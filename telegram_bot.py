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


def tg(method, **params):
    r = requests.post(f'{API}/{method}', json=params, timeout=35)
    r.raise_for_status()
    return r.json()


def send(chat_id, text):
    try:
        tg('sendMessage', chat_id=chat_id, text=text)
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
    payload = {'message': text, 'messages': history}
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
    if not text and not has_photo:
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
        send(chat_id, 'Conversation cleared.')
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
        send(chat_id, ask_advisor(chat_id, question, image_b64=img_b64))
        return

    send(chat_id, ask_advisor(chat_id, text))


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
        except Exception as e:
            logger.warning(f'poll error: {e}')
            time.sleep(10)


if __name__ == '__main__':
    main()
