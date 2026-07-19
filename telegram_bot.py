#!/usr/bin/env python3
"""
Wheelhouse Telegram Bot — long-polling, no external deps beyond requests.

What it does:
  /start <CODE>   Link a Telegram chat to a Wheelhouse account. The code comes
                  from the app (Settings -> Link Telegram); one-time use.
  /unlink         Unlink this chat.
  any other text  Goes to the Captain's Advisor; the reply comes back in chat.
  a photo         Sent to the advisor as an image (fishfinder screen, catch,
                  bird pile...). Add a caption to ask a specific question;
                  no caption asks "what am I looking at?"
  a location pin  Sets your position for the rest of the conversation — the
                  advisor gets it with every question, same as the app's GPS
                  tag. Live-location updates keep it current. /reset clears it.

Once linked, the chat also receives crew catch alerts and the 5AM briefing
verdict (sent by push_notify.telegram_to_user from the web app / cron).

Setup (one time):
  1. Telegram: talk to @BotFather -> /newbot -> pick a name -> copy the token.
  2. On the server, add to /opt/wheelhouse/.env:
       TELEGRAM_BOT_TOKEN=123456:ABC-...
       TELEGRAM_BOT_NAME=YourBotUsername     (without @, used for app links)
  3. Install the systemd service (run as root):
       cat > /etc/systemd/system/wheelhouse-telegram.service << 'EOF'
       [Unit]
       Description=Wheelhouse Telegram Bot
       After=network-online.target

       [Service]
       User=rednun
       WorkingDirectory=/opt/wheelhouse
       ExecStart=/opt/wheelhouse/venv/bin/python /opt/wheelhouse/telegram_bot.py
       Restart=always
       RestartSec=10

       [Install]
       WantedBy=multi-user.target
       EOF
       systemctl daemon-reload && systemctl enable --now wheelhouse-telegram

The bot talks to the advisor through the local web app's /api/bot/advisor
endpoint (X-Bot-Key auth), so it needs BOT_SECRET_KEY set in .env as well.
"""

import os
import re as _re
import sys
import time
import json
import base64
import sqlite3
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
load_dotenv('/opt/rednun/.env', override=False)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('wh-telegram')

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'wheelhouse.db')
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
BOT_KEY = os.environ.get('BOT_SECRET_KEY', '')
ADVISOR_URL = os.environ.get('WH_LOCAL_URL', 'http://127.0.0.1:8090') + '/api/bot/advisor'
API = f'https://api.telegram.org/bot{TOKEN}'

# Per-chat rolling advisor history (in memory; resets on restart)
_HISTORY = {}
# Per-chat last known position from a shared location pin: chat_id -> (lat, lon)
_GPS = {}


def tg(method, **params):
    r = requests.post(f'{API}/{method}', json=params, timeout=35)
    r.raise_for_status()
    return r.json()


def md_to_telegram_html(text):
    """Convert the advisor's markdown to Telegram's small HTML subset.

    Telegram HTML supports <b>/<i>/<code>/<pre> and requires &<> escaped
    everywhere else. Headers become bold lines, bullets become •.
    """
    s = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    s = _re.sub(r'`([^`\n]+)`', r'<code>\1</code>', s)
    s = _re.sub(r'^#{1,4} +(.+)$', r'<b>\1</b>', s, flags=_re.M)
    s = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s, flags=_re.S)
    s = _re.sub(r'(?<![\w*])\*([^*\n]+)\*(?![\w*])', r'<i>\1</i>', s)
    s = _re.sub(r'^[\-\*] +', '• ', s, flags=_re.M)
    s = _re.sub(r'^-{3,}$', '———', s, flags=_re.M)
    return s


def _chunks(text, limit=3900):
    """Split on newlines to stay under Telegram's 4096-char message cap."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ''
    for line in text.split('\n'):
        while len(line) > limit:  # single pathological line: hard-split
            if cur:
                out.append(cur)
                cur = ''
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = cur + '\n' + line if cur else line
    if cur:
        out.append(cur)
    return out


def send(chat_id, text, fmt=False):
    """Send a message. fmt=True renders advisor markdown as Telegram HTML,
    falling back to plain text if Telegram rejects the markup."""
    for part in _chunks(text):
        try:
            if fmt:
                try:
                    tg('sendMessage', chat_id=chat_id,
                       text=md_to_telegram_html(part), parse_mode='HTML')
                    continue
                except Exception as e:
                    logger.warning(f'HTML send failed, falling back to plain: {e}')
            tg('sendMessage', chat_id=chat_id, text=part)
        except Exception as e:
            logger.warning(f'send failed: {e}')


def username_for_chat(chat_id):
    with sqlite3.connect(DB_PATH, timeout=15) as db:
        row = db.execute('SELECT username FROM telegram_links WHERE chat_id = ?',
                         (chat_id,)).fetchone()
        return row[0] if row else None


def try_link(chat_id, code):
    """Bind chat_id to whichever account generated this one-time code."""
    code = (code or '').strip().upper()
    if not code:
        return None
    with sqlite3.connect(DB_PATH, timeout=15) as db:
        row = db.execute('SELECT username FROM telegram_links WHERE link_code = ?',
                         (code,)).fetchone()
        if not row:
            return None
        db.execute('UPDATE telegram_links SET chat_id = ?, link_code = NULL, linked_at = ? '
                   'WHERE username = ?', (chat_id, datetime.now().isoformat(), row[0]))
        db.commit()
        return row[0]


def unlink(chat_id):
    with sqlite3.connect(DB_PATH, timeout=15) as db:
        db.execute('UPDATE telegram_links SET chat_id = NULL WHERE chat_id = ?', (chat_id,))
        db.commit()


def ask_advisor(chat_id, text, image_b64=None, image_media_type='image/jpeg'):
    history = _HISTORY.get(chat_id, [])
    # Same GPS tag the app appends, sourced from the chat's last location pin.
    # The tag rides on the outgoing message only — history stays clean.
    gps = _GPS.get(chat_id)
    out_text = text + (f'\n[Current GPS: {gps[0]:.4f}, {gps[1]:.4f}]' if gps else '')
    payload = {'message': out_text, 'messages': history}
    if image_b64:
        payload['image_b64'] = image_b64
        payload['image_media_type'] = image_media_type
    try:
        r = requests.post(ADVISOR_URL,
                          headers={'X-Bot-Key': BOT_KEY},
                          json=payload,
                          timeout=120 if image_b64 else 90)
        r.raise_for_status()
        reply = r.json().get('reply', '') or 'No answer — try again.'
    except Exception as e:
        logger.error(f'advisor call failed: {e}')
        return 'Advisor is unavailable right now — try again in a minute.'
    # History stays text-only; a placeholder marks photo turns so follow-up
    # questions still make sense to the model.
    history.append({'role': 'user', 'content': text + (' [photo attached]' if image_b64 else '')})
    history.append({'role': 'assistant', 'content': reply})
    _HISTORY[chat_id] = history[-16:]
    return reply


def fetch_photo_b64(msg):
    """Download the largest rendition of a Telegram photo, return base64 str.

    Telegram pre-compresses photos to JPEG (~1280px longest side for the
    largest rendition), which is exactly the size we want for vision — no
    resizing needed on our end.
    """
    photos = msg.get('photo') or []
    if not photos:
        return None
    file_id = photos[-1]['file_id']  # sizes are ordered small -> large
    info = tg('getFile', file_id=file_id)
    path = info['result']['file_path']
    r = requests.get(f'https://api.telegram.org/file/bot{TOKEN}/{path}', timeout=30)
    r.raise_for_status()
    return base64.b64encode(r.content).decode('ascii')


def handle(msg):
    chat_id = msg['chat']['id']
    text = (msg.get('text') or '').strip()
    has_photo = bool(msg.get('photo'))
    loc = msg.get('location')
    has_loc = bool(loc and loc.get('latitude') is not None)
    if not text and not has_photo and not has_loc:
        return

    if text.startswith('/start'):
        parts = text.split(None, 1)
        code = parts[1] if len(parts) > 1 else ''
        user = try_link(chat_id, code)
        if user:
            send(chat_id, f'Linked to {user}. You will get crew catch alerts and the '
                          f'morning briefing here. Ask me anything about conditions, '
                          f'tides, or where to fish.')
        elif username_for_chat(chat_id):
            send(chat_id, 'Already linked. Ask me a fishing question, or /unlink.')
        else:
            send(chat_id, 'To link your Wheelhouse account: open the app -> Settings -> '
                          'Link Telegram, then send me /start YOURCODE')
        return

    if text == '/unlink':
        unlink(chat_id)
        send(chat_id, 'Unlinked. /start <code> to link again.')
        return

    user = username_for_chat(chat_id)
    if not user:
        send(chat_id, 'Not linked yet. Open the Wheelhouse app -> Settings -> Link Telegram '
                      'and send me /start YOURCODE')
        return

    if text.lower() in ('/reset', 'reset'):
        _HISTORY.pop(chat_id, None)
        had_gps = _GPS.pop(chat_id, None)
        send(chat_id, 'Conversation cleared.' + (' Position cleared too.' if had_gps else ''))
        return

    if has_loc:
        _GPS[chat_id] = (loc['latitude'], loc['longitude'])
        send(chat_id, f"Got your position ({loc['latitude']:.4f}, {loc['longitude']:.4f}) — "
                      f"I'll pin every question to it until you send a new one or /reset. "
                      f"Share a live location and I'll track you as you move.")
        return

    if has_photo:
        try:
            img_b64 = fetch_photo_b64(msg)
        except Exception as e:
            logger.error(f'photo fetch failed: {e}')
            send(chat_id, "Couldn't pull that photo down — send it again.")
            return
        question = (msg.get('caption') or '').strip() or \
            "What am I looking at here, and what's the move?"
        send(chat_id, ask_advisor(chat_id, question, image_b64=img_b64), fmt=True)
        return

    send(chat_id, ask_advisor(chat_id, text), fmt=True)


def main():
    if not TOKEN:
        logger.error('TELEGRAM_BOT_TOKEN not set — see docstring. Exiting.')
        sys.exit(1)
    logger.info('Wheelhouse Telegram bot polling...')
    offset = 0
    while True:
        try:
            resp = tg('getUpdates', offset=offset, timeout=30)
            for upd in resp.get('result', []):
                offset = upd['update_id'] + 1
                if 'message' in upd:
                    try:
                        handle(upd['message'])
                    except Exception as e:
                        logger.error(f'handle failed: {e}')
                elif 'edited_message' in upd:
                    # Live-location shares arrive as silent edits — keep the
                    # chat's pinned position current, no reply.
                    em = upd['edited_message']
                    eloc = em.get('location')
                    if eloc and eloc.get('latitude') is not None:
                        _GPS[em['chat']['id']] = (eloc['latitude'], eloc['longitude'])
        except Exception as e:
            logger.warning(f'poll error: {e}')
            time.sleep(10)


if __name__ == '__main__':
    main()
