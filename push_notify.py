"""
Wheelhouse notifications — Web Push + Telegram delivery.

One entry point for every alert the app generates:

    notify_user(username, title, body, url='/')

delivers to (a) every Web Push subscription the user has registered (iPhone
home-screen PWA, desktop browser) and (b) their linked Telegram chat, if any.
Both channels fail independently and silently — notifications are best-effort.

Web Push uses VAPID. Keys are generated once and persisted to vapid.json
next to this file (gitignored); no manual key setup needed.

Requires: pywebpush  (server: /opt/wheelhouse/venv/bin/pip install pywebpush)
Telegram requires TELEGRAM_BOT_TOKEN in .env (see telegram_bot.py).
"""

import os
import json
import sqlite3
import logging

logger = logging.getLogger('wh-notify')

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'wheelhouse.db')
VAPID_PATH = os.path.join(BASE, 'vapid.json')
VAPID_PEM_PATH = os.path.join(BASE, 'vapid_private.pem')
VAPID_CLAIM_EMAIL = 'mailto:mgiorgio@rednun.com'


def ensure_tables():
    with sqlite3.connect(DB_PATH, timeout=15) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            endpoint TEXT UNIQUE NOT NULL,
            subscription TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS telegram_links (
            username TEXT PRIMARY KEY,
            chat_id INTEGER,
            link_code TEXT,
            linked_at TIMESTAMP
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS notify_prefs (
            username TEXT PRIMARY KEY,
            channels TEXT NOT NULL DEFAULT 'both'
        )''')
        db.commit()


# ==================== VAPID KEYS ====================

def _ensure_pem_file(keys):
    """pywebpush wants the private key as a FILE PATH (a PEM string gets
    misparsed as a raw base64 key -> 'ASN.1 parsing error'). Keep the PEM
    materialized next to vapid.json."""
    if not os.path.exists(VAPID_PEM_PATH):
        with open(VAPID_PEM_PATH, 'w') as f:
            f.write(keys['private_pem'])
        os.chmod(VAPID_PEM_PATH, 0o600)
    return VAPID_PEM_PATH


def _load_or_create_vapid():
    """Persisted VAPID keypair; generated on first use."""
    if os.path.exists(VAPID_PATH):
        with open(VAPID_PATH) as f:
            keys = json.load(f)
        _ensure_pem_file(keys)
        return keys
    from py_vapid import Vapid02, b64urlencode
    from cryptography.hazmat.primitives import serialization
    v = Vapid02()
    v.generate_keys()
    priv = v.private_pem().decode()
    raw_pub = v.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    keys = {'private_pem': priv, 'public_key_b64': b64urlencode(raw_pub)}
    with open(VAPID_PATH, 'w') as f:
        json.dump(keys, f)
    os.chmod(VAPID_PATH, 0o600)
    _ensure_pem_file(keys)
    logger.info('Generated new VAPID keypair')
    return keys


def vapid_public_key():
    try:
        return _load_or_create_vapid()['public_key_b64']
    except Exception as e:
        logger.error(f'VAPID key unavailable: {e}')
        return None


# ==================== SUBSCRIPTIONS ====================

def save_subscription(username, subscription):
    """subscription: the browser's PushSubscription JSON (dict)."""
    ensure_tables()
    endpoint = (subscription or {}).get('endpoint', '')
    if not endpoint:
        return False
    with sqlite3.connect(DB_PATH, timeout=15) as db:
        db.execute('''INSERT INTO push_subscriptions (username, endpoint, subscription)
                      VALUES (?, ?, ?)
                      ON CONFLICT(endpoint) DO UPDATE SET
                        username = excluded.username,
                        subscription = excluded.subscription''',
                   (username, endpoint, json.dumps(subscription)))
        db.commit()
    return True


def remove_subscription(endpoint):
    ensure_tables()
    with sqlite3.connect(DB_PATH, timeout=15) as db:
        db.execute('DELETE FROM push_subscriptions WHERE endpoint = ?', (endpoint,))
        db.commit()


# ==================== SENDING ====================

def _send_webpush(subscription, payload):
    from pywebpush import webpush, WebPushException
    keys = _load_or_create_vapid()
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=_ensure_pem_file(keys),
            vapid_claims={'sub': VAPID_CLAIM_EMAIL},
            timeout=10,
        )
        return True
    except WebPushException as e:
        code = getattr(getattr(e, 'response', None), 'status_code', None)
        if code in (404, 410):  # subscription expired/revoked — prune it
            remove_subscription(subscription.get('endpoint', ''))
            logger.info('Pruned dead push subscription')
        else:
            logger.warning(f'webpush failed: {e}')
        return False


def push_to_user(username, title, body, url='/'):
    """Send to every push subscription this user has. Returns count sent."""
    ensure_tables()
    sent = 0
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT subscription FROM push_subscriptions WHERE username = ?',
                              (username,)).fetchall()
        payload = {'title': title, 'body': body, 'url': url}
        for r in rows:
            try:
                if _send_webpush(json.loads(r['subscription']), payload):
                    sent += 1
            except Exception as e:
                logger.warning(f'push send failed: {e}')
    except Exception as e:
        logger.error(f'push_to_user failed: {e}')
    return sent


def telegram_to_user(username, text):
    """Send a Telegram message to the user's linked chat. Best-effort."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    if not token:
        return False
    try:
        import requests
        ensure_tables()
        with sqlite3.connect(DB_PATH, timeout=15) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT chat_id FROM telegram_links WHERE username = ? AND chat_id IS NOT NULL',
                             (username,)).fetchone()
        if not row:
            return False
        r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                          json={'chat_id': row['chat_id'], 'text': text}, timeout=10)
        return r.ok
    except Exception as e:
        logger.warning(f'telegram send failed: {e}')
        return False


VALID_CHANNELS = ('both', 'push', 'telegram')


def get_notify_pref(username):
    """'both' (default) | 'push' | 'telegram'."""
    try:
        ensure_tables()
        with sqlite3.connect(DB_PATH, timeout=15) as db:
            row = db.execute('SELECT channels FROM notify_prefs WHERE username = ?',
                             (username,)).fetchone()
            if row and row[0] in VALID_CHANNELS:
                return row[0]
    except Exception as e:
        logger.warning(f'notify pref read failed: {e}')
    return 'both'


def set_notify_pref(username, channels):
    if channels not in VALID_CHANNELS:
        return False
    ensure_tables()
    with sqlite3.connect(DB_PATH, timeout=15) as db:
        db.execute('''INSERT INTO notify_prefs (username, channels) VALUES (?, ?)
                      ON CONFLICT(username) DO UPDATE SET channels = excluded.channels''',
                   (username, channels))
        db.commit()
    return True


def notify_user(username, title, body, url='/'):
    """Deliver an alert over the channels the user chose (default: both).
    Never raises. Note: this governs ALERTS only — two-way advisor chat on
    Telegram works regardless of this preference."""
    pref = get_notify_pref(username)
    if pref in ('both', 'push'):
        try:
            push_to_user(username, title, body, url=url)
        except Exception as e:
            logger.error(f'notify push failed: {e}')
    if pref in ('both', 'telegram'):
        try:
            telegram_to_user(username, f'{title}\n{body}')
        except Exception as e:
            logger.error(f'notify telegram failed: {e}')


def all_push_usernames():
    """Distinct users with at least one channel — for broadcast (morning briefing)."""
    ensure_tables()
    users = set()
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as db:
            for (u,) in db.execute('SELECT DISTINCT username FROM push_subscriptions'):
                users.add(u)
            for (u,) in db.execute('SELECT username FROM telegram_links WHERE chat_id IS NOT NULL'):
                users.add(u)
    except Exception as e:
        logger.error(f'all_push_usernames failed: {e}')
    return users
