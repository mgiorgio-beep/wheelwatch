import os
import json
import sqlite3
import logging
import smtplib
import random
import string
import glob as globmod
import time as time_module
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from flask import Flask, send_from_directory, session, request, redirect, jsonify, g
from functools import wraps
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

# Load both .env files — wheelhouse for app config, rednun for email creds
load_dotenv()
load_dotenv('/opt/rednun/.env', override=False)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('wheelhouse')

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24).hex())
app.permanent_session_lifetime = timedelta(days=30)

DB_PATH = os.path.join(os.path.dirname(__file__), 'wheelhouse.db')

NOTIFY_EMAIL = 'mgiorgio@rednun.com'
GMAIL_USER = os.environ.get('GMAIL_ADDRESS', '')
GMAIL_PASS = os.environ.get('GMAIL_APP_PASSWORD', '')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')


# ==================== EMAIL NOTIFICATIONS ====================

def send_notification(subject, body):
    """Send email notification. Fails silently."""
    if not GMAIL_USER or not GMAIL_PASS:
        logger.warning('Email not configured — skipping notification')
        return
    try:
        msg = MIMEText(body, 'plain')
        msg['Subject'] = subject
        msg['From'] = GMAIL_USER
        msg['To'] = NOTIFY_EMAIL
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASS)
            smtp.send_message(msg)
        logger.info('Notification sent: {}'.format(subject))
    except Exception as e:
        logger.error('Email notification failed: {}'.format(e))


# ==================== DATABASE ====================

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        hide_welcome INTEGER DEFAULT 0
    )''')

    # Additional columns (safe to re-run)
    for col_sql in [
        "ALTER TABLE users ADD COLUMN phone_number TEXT DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN phone_verified INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN phone_verify_code TEXT DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN phone_verify_expires REAL DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN first_name TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN last_name TEXT DEFAULT ''",
    ]:
        try:
            db.execute(col_sql)
        except Exception:
            pass

    db.execute('''CREATE TABLE IF NOT EXISTS friend_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        code TEXT UNIQUE NOT NULL,
        created_by TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    db.execute('''CREATE TABLE IF NOT EXISTS group_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        role TEXT DEFAULT 'member',
        share_my_catches INTEGER DEFAULT 1,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, username),
        FOREIGN KEY(group_id) REFERENCES friend_groups(id)
    )''')

    db.execute('''CREATE TABLE IF NOT EXISTS group_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        group_name TEXT NOT NULL,
        from_user TEXT NOT NULL,
        to_user TEXT NOT NULL,
        spot TEXT,
        species TEXT,
        message TEXT,
        read INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    db.execute('''CREATE TABLE IF NOT EXISTS sms_conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone_number TEXT NOT NULL,
        direction TEXT NOT NULL,
        body TEXT NOT NULL,
        twilio_sid TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    db.execute('''CREATE TABLE IF NOT EXISTS sms_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone_number TEXT NOT NULL UNIQUE,
        history TEXT DEFAULT '[]',
        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        message_count INTEGER DEFAULT 0
    )''')

    db.execute('''CREATE TABLE IF NOT EXISTS location_updates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        accuracy REAL,
        sharing INTEGER DEFAULT 1,
        sharing_group_id INTEGER,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(username)
    )''')

    db.commit()
    db.close()
    logger.info('Database initialized')

init_db()


# ==================== AUTH ====================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Not authenticated'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


# ==================== AUTH PAGES ====================

PAGE_STYLE = '''
body { background:#f0f4f8; color:#1a2a3a; font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;
       display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }
.box { background:#ffffff; border:1px solid #c8d6e0; border-radius:10px; padding:32px; width:320px;
       text-align:center; box-shadow:0 2px 8px rgba(0,0,0,0.06); }
h1 { font-size:22px; letter-spacing:3px; margin-bottom:6px; color:#1a2a3a; }
h1 span { color:#0077aa; }
.subtitle { font-size:12px; color:#7a8a9a; margin-bottom:20px; }
input { width:100%; padding:12px; background:#e8edf2; border:1px solid #c8d6e0; border-radius:6px;
         color:#1a2a3a; font-family:inherit; font-size:16px; margin-bottom:10px; box-sizing:border-box;
         outline:none; -webkit-appearance:none; }
input:focus { border-color:#0077aa; }
button { width:100%; padding:13px; background:#0077aa; border:none; color:#fff;
          border-radius:6px; cursor:pointer; font-family:inherit; font-size:14px; font-weight:700;
          letter-spacing:1px; margin-top:4px; }
button:hover { background:#006699; }
.link { margin-top:16px; font-size:13px; color:#7a8a9a; }
.link a { color:#0077aa; text-decoration:none; }
.link a:hover { text-decoration:underline; }
.err { color:#cc2222; font-size:13px; margin-bottom:12px; background:#cc222210; padding:8px;
       border-radius:4px; }
'''

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        if not username or not password:
            return render_auth_page('login', error='Enter email and password')
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session.permanent = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect('/')
        return render_auth_page('login', error='Invalid email or password')
    return render_auth_page('login')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    import re
    if request.method == 'POST':
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        phone = request.form.get('phone', '').strip()
        if not first_name or not last_name:
            return render_auth_page('signup', error='First and last name required')
        if not username or not password:
            return render_auth_page('signup', error='Email and password required')
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', username):
            return render_auth_page('signup', error='Enter a valid email address')
        if len(password) < 4:
            return render_auth_page('signup', error='Password must be at least 4 characters')
        if password != confirm:
            return render_auth_page('signup', error='Passwords don\'t match')
        db = get_db()
        existing = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if existing:
            return render_auth_page('signup', error='Email already registered')
        pw_hash = generate_password_hash(password)
        db.execute('INSERT INTO users (username, password_hash, first_name, last_name) VALUES (?, ?, ?, ?)',
                   (username, pw_hash, first_name, last_name))
        db.commit()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        session.permanent = True
        session['user_id'] = user['id']
        session['username'] = user['username']
        # Save phone if provided (unverified — they can verify from Settings)
        if phone:
            phone_clean = ''.join(c for c in phone if c.isdigit() or c == '+')
            if not phone_clean.startswith('+'):
                phone_clean = '+1' + phone_clean
            if len(phone_clean) >= 10:
                db.execute('UPDATE users SET phone_number = ? WHERE id = ?',
                           (phone_clean, user['id']))
                db.commit()
        logger.info('New user registered: {}'.format(username))
        send_notification(
            'Wheelhouse — New User: {}'.format(username),
            'New account created on Wheelhouse.\n\nEmail: {}\nPhone: {}\nTime: {}\n\nhttps://wheelhouse.rednun.com'.format(
                username, phone or 'not provided', datetime.now().strftime('%B %d, %Y %I:%M %p'))
        )
        return redirect('/')
    return render_auth_page('signup')

def render_auth_page(mode, error=None):
    err_html = '<div class="err">{}</div>'.format(error) if error else ''
    if mode == 'signup':
        form = '''
        <form method="POST">
        <div style="display:flex;gap:8px;">
        <input type="text" name="first_name" placeholder="First name" autocomplete="given-name" autofocus style="flex:1">
        <input type="text" name="last_name" placeholder="Last name" autocomplete="family-name" style="flex:1">
        </div>
        <input type="email" name="username" placeholder="Email address" autocomplete="email">
        <input type="password" name="password" placeholder="Password" autocomplete="new-password">
        <input type="password" name="confirm" placeholder="Confirm password" autocomplete="new-password">
        <div style="margin:12px 0 4px;text-align:left">
          <div style="font-size:12px;font-weight:600;color:#1a2a3a;margin-bottom:6px">Phone number <span style="color:#7a8a9a;font-weight:400">(optional)</span></div>
          <input type="tel" name="phone" placeholder="(617) 555-1234" autocomplete="tel" style="margin-bottom:6px">
          <div style="font-size:11px;color:#0077aa;line-height:1.4;margin-bottom:4px">
            With phone: SMS catch alerts from your crew, weather/conditions texts, and text-based advisor access.
          </div>
          <div style="font-size:11px;color:#7a8a9a;line-height:1.4">
            Without phone: Full app access &mdash; no text notifications. You can always add your phone later in Settings.
          </div>
        </div>
        <button type="submit">CREATE ACCOUNT</button>
        </form>
        <div class="link">Already have an account? <a href="/login">Log in</a></div>
        '''
        subtitle = 'Create your account'
    else:
        form = '''
        <form method="POST">
        <input type="email" name="username" placeholder="Email" autocomplete="email" autofocus>
        <input type="password" name="password" placeholder="Password" autocomplete="current-password">
        <button type="submit">LOG IN</button>
        </form>
        <div class="link">New here? <a href="/signup">Create an account</a></div>
        '''
        subtitle = 'Log in to continue'

    html = '''<!DOCTYPE html>
<html><head><title>Wheelhouse</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#f0f4f8">
<style>{style}</style></head><body>
<div class="box">
<img src="/static/nco_logo.jpg" alt="North Chatham Outfitters" style="width:120px;margin-bottom:12px;opacity:0.9;">
<h1>&#9875; WHEEL<span>HOUSE</span></h1>
<div class="subtitle">{subtitle}</div>
{err}
{form}
</div></body></html>'''.format(style=PAGE_STYLE, subtitle=subtitle, err=err_html, form=form)
    return html

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ==================== USER API ====================

@app.route('/api/user/profile')
@login_required
def api_user_profile():
    db = get_db()
    user = db.execute('SELECT username, hide_welcome, phone_number, phone_verified, is_admin FROM users WHERE id = ?',
                      (session['user_id'],)).fetchone()
    if not user:
        return jsonify({'error': 'User not found'}), 404
    return jsonify({
        'username': user['username'],
        'hide_welcome': bool(user['hide_welcome']),
        'phone_number': user['phone_number'],
        'phone_verified': bool(user['phone_verified']),
        'is_admin': bool(user['is_admin']),
    })

@app.route('/api/user/hide-welcome', methods=['POST'])
@login_required
def api_user_hide_welcome():
    db = get_db()
    db.execute('UPDATE users SET hide_welcome = 1 WHERE id = ?', (session['user_id'],))
    db.commit()
    return jsonify({'ok': True})


# ==================== MAIN ROUTES ====================

@app.route('/')
@login_required
def index():
    resp = send_from_directory('static', 'fishing.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@app.route('/api/bot/advisor', methods=['POST'])
def api_bot_advisor():
    """Bot-only advisor endpoint — auths via X-Bot-Key header instead of session.

    Reuses the same captain_advisor.ask_advisor logic as /api/fishing/advisor,
    but is callable from the Telegram bot process running on the same host.
    """
    expected_key = os.environ.get('BOT_SECRET_KEY', '')
    if not expected_key:
        logger.warning('Bot advisor called but BOT_SECRET_KEY is not configured')
        return jsonify({'error': 'Bot endpoint not configured'}), 503

    provided_key = request.headers.get('X-Bot-Key', '')
    if provided_key != expected_key:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    user_message = data.get('message', '')
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    messages = data.get('messages', [])

    from captain_advisor import ask_advisor
    reply = ask_advisor(messages, user_message)
    return jsonify({'reply': reply})


@app.route('/api/suggestion', methods=['POST'])
@login_required
def api_suggestion():
    import threading
    data = request.get_json()
    text = data.get('text', '').strip() if data else ''
    if not text:
        return jsonify({'error': 'No suggestion provided'}), 400
    username = session.get('username', 'unknown')
    # Send email in background so response is instant
    threading.Thread(target=send_notification, args=(
        f'💬 Wheelhouse Suggestion from {username}',
        f'Suggestion from {username}:\n\n{text}\n\nhttps://wheelhouse.rednun.com'
    )).start()
    logger.info(f'Suggestion received from {username}: {text[:100]}')
    return jsonify({'sent': True})

# ==================== TWILIO CONFIG ====================

TWILIO_SID   = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_FROM  = os.environ.get('TWILIO_PHONE_NUMBER', '')

def get_twilio_client():
    from twilio.rest import Client as TwilioClient
    return TwilioClient(TWILIO_SID, TWILIO_TOKEN)

def _send_sms(to_number, body):
    """Send an SMS via Twilio. Returns True on success."""
    if not TWILIO_SID or not TWILIO_TOKEN:
        logger.error('Twilio not configured')
        return False
    try:
        client = get_twilio_client()
        client.messages.create(to=to_number, from_=TWILIO_FROM, body=body)
        return True
    except Exception as e:
        logger.error(f'SMS send failed: {e}')
        return False

def sms_reply(to_number, body):
    """Send an SMS reply via Twilio. Splits long messages automatically."""
    if not TWILIO_SID or not TWILIO_TOKEN:
        logger.error('Twilio credentials not configured')
        return False
    try:
        client = get_twilio_client()
        max_len = 1550
        if len(body) <= max_len:
            msg = client.messages.create(to=to_number, from_=TWILIO_FROM, body=body)
            logger.info(f'SMS sent to {to_number[-4:]}****: {msg.sid}')
        else:
            chunks = []
            current = ''
            for sentence in body.replace('. ', '.|').split('|'):
                if len(current) + len(sentence) < max_len:
                    current += sentence + ' '
                else:
                    if current: chunks.append(current.strip())
                    current = sentence + ' '
            if current: chunks.append(current.strip())
            for i, chunk in enumerate(chunks):
                prefix = f'({i+1}/{len(chunks)}) ' if len(chunks) > 1 else ''
                client.messages.create(to=to_number, from_=TWILIO_FROM,
                                       body=prefix + chunk)
        return True
    except Exception as e:
        logger.error(f'Twilio send failed: {e}')
        return False


# ==================== PHONE REGISTRATION ====================

@app.route('/api/user/phone/register', methods=['POST'])
@login_required
def api_phone_register():
    data = request.get_json()
    phone = data.get('phone', '').strip() if data else ''
    phone = ''.join(c for c in phone if c.isdigit() or c == '+')
    if not phone.startswith('+'):
        phone = '+1' + phone
    if len(phone) < 10:
        return jsonify({'error': 'Invalid phone number'}), 400
    db = get_db()
    existing = db.execute(
        'SELECT id, username FROM users WHERE phone_number = ? AND phone_verified = 1',
        (phone,)).fetchone()
    if existing and existing['username'] != session['username']:
        return jsonify({'error': 'Phone number already registered'}), 409
    code = str(random.randint(100000, 999999))
    expires = time_module.time() + 600
    db.execute('''UPDATE users SET phone_number = ?, phone_verify_code = ?,
                  phone_verify_expires = ?, phone_verified = 0
                  WHERE id = ?''',
               (phone, code, expires, session['user_id']))
    db.commit()
    sent = _send_sms(phone,
        f"Wheelhouse verification code: {code}\n"
        f"Expires in 10 minutes. Do not share this code.")
    if not sent:
        return jsonify({'error': 'Could not send SMS. Check the number and try again.'}), 500
    logger.info(f'Verification code sent to {phone[-4:]}**** for {session["username"]}')
    return jsonify({'sent': True, 'phone': phone})

@app.route('/api/user/phone/verify', methods=['POST'])
@login_required
def api_phone_verify():
    data = request.get_json()
    code = data.get('code', '').strip() if data else ''
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if not user['phone_verify_code']:
        return jsonify({'error': 'No verification pending'}), 400
    if time_module.time() > (user['phone_verify_expires'] or 0):
        return jsonify({'error': 'Code expired. Request a new one.'}), 400
    if code != user['phone_verify_code']:
        return jsonify({'error': 'Incorrect code'}), 400
    db.execute('''UPDATE users SET phone_verified = 1,
                  phone_verify_code = NULL, phone_verify_expires = NULL
                  WHERE id = ?''', (session['user_id'],))
    db.commit()
    logger.info(f'Phone verified for {session["username"]}: {user["phone_number"][-4:]}****')
    _send_sms(user['phone_number'],
        f"Wheelhouse ready. Text this number anytime for conditions, "
        f"tides, and fishing intel.\n\n"
        f"To log a catch just text it naturally:\n"
        f"\"28lb striper Stonehorse white bucktail flood\"\n\n"
        f"Text HELP for commands.")
    return jsonify({'verified': True})

@app.route('/api/user/phone/remove', methods=['POST'])
@login_required
def api_phone_remove():
    db = get_db()
    db.execute('''UPDATE users SET phone_number = NULL, phone_verified = 0,
                  phone_verify_code = NULL, phone_verify_expires = NULL
                  WHERE id = ?''', (session['user_id'],))
    db.commit()
    return jsonify({'removed': True})


# ==================== FRIEND GROUPS ====================

def _generate_invite_code():
    words = ['MONOMOY','POLLOCK','CHATHAM','SHOALS','STRIPER',
             'BLUEFISH','STAGHAR','STONHRS','BEARSE','REDNUN']
    return random.choice(words) + ''.join(random.choices(string.digits, k=2))

@app.route('/api/groups', methods=['GET'])
@login_required
def api_groups_list():
    db = get_db()
    rows = db.execute('''
        SELECT g.id, g.name, g.code, g.created_by, g.created_at,
               gm.role, gm.share_my_catches,
               (SELECT COUNT(*) FROM group_members WHERE group_id = g.id) as member_count
        FROM friend_groups g
        JOIN group_members gm ON g.id = gm.group_id
        WHERE gm.username = ?
        ORDER BY g.created_at DESC
    ''', (session['username'],)).fetchall()
    return jsonify({'groups': [dict(r) for r in rows]})

@app.route('/api/groups', methods=['POST'])
@login_required
def api_groups_create():
    data = request.get_json()
    name = data.get('name', '').strip() if data else ''
    if not name or len(name) < 2:
        return jsonify({'error': 'Group name required'}), 400
    db = get_db()
    code = _generate_invite_code()
    while db.execute('SELECT id FROM friend_groups WHERE code = ?', (code,)).fetchone():
        code = _generate_invite_code()
    db.execute('INSERT INTO friend_groups (name, code, created_by) VALUES (?, ?, ?)',
               (name, code, session['username']))
    group_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    db.execute('INSERT INTO group_members (group_id, username, role) VALUES (?, ?, ?)',
               (group_id, session['username'], 'captain'))
    db.commit()
    logger.info(f'Friend Group created: "{name}" ({code}) by {session["username"]}')
    return jsonify({'group_id': group_id, 'code': code, 'name': name})

@app.route('/api/groups/join', methods=['POST'])
@login_required
def api_groups_join():
    data = request.get_json()
    code = data.get('code', '').strip().upper() if data else ''
    if not code:
        return jsonify({'error': 'Invite code required'}), 400
    db = get_db()
    group = db.execute('SELECT * FROM friend_groups WHERE code = ?', (code,)).fetchone()
    if not group:
        return jsonify({'error': 'Invalid code'}), 404
    if db.execute('SELECT id FROM group_members WHERE group_id = ? AND username = ?',
                  (group['id'], session['username'])).fetchone():
        return jsonify({'error': 'Already a member'}), 409
    db.execute('INSERT INTO group_members (group_id, username, role) VALUES (?, ?, ?)',
               (group['id'], session['username'], 'member'))
    db.commit()
    return jsonify({'group_id': group['id'], 'name': group['name']})

@app.route('/api/groups/<int:group_id>/members', methods=['GET'])
@login_required
def api_group_members(group_id):
    db = get_db()
    if not db.execute('SELECT id FROM group_members WHERE group_id = ? AND username = ?',
                      (group_id, session['username'])).fetchone():
        return jsonify({'error': 'Not a member'}), 403
    members = db.execute('''
        SELECT username, role, share_my_catches, joined_at
        FROM group_members WHERE group_id = ?
        ORDER BY role DESC, joined_at ASC
    ''', (group_id,)).fetchall()
    return jsonify({'members': [dict(m) for m in members]})

@app.route('/api/groups/<int:group_id>/sharing', methods=['POST'])
@login_required
def api_group_sharing_toggle(group_id):
    data = request.get_json()
    share = 1 if data.get('share') else 0
    db = get_db()
    db.execute('UPDATE group_members SET share_my_catches = ? WHERE group_id = ? AND username = ?',
               (share, group_id, session['username']))
    db.commit()
    return jsonify({'sharing': bool(share)})

@app.route('/api/groups/<int:group_id>/catches', methods=['GET'])
@login_required
def api_group_catches(group_id):
    """Shared catches from group members who have opted in. GPS included for crew."""
    db = get_db()
    if not db.execute('SELECT id FROM group_members WHERE group_id = ? AND username = ?',
                      (group_id, session['username'])).fetchone():
        return jsonify({'error': 'Not a member'}), 403
    sharing = db.execute(
        'SELECT username FROM group_members WHERE group_id = ? AND share_my_catches = 1',
        (group_id,)).fetchall()
    sharing_users = set(r['username'] for r in sharing)
    from captain_advisor import LOGS_DIR
    files = sorted(globmod.glob(os.path.join(LOGS_DIR, 'catch_*.json')), reverse=True)
    catches = []
    for fp in files[:500]:
        try:
            with open(fp) as f:
                entry = json.load(f)
            if entry.get('logged_by') not in sharing_users:
                continue
            dt = datetime.fromisoformat(entry['timestamp'])
            catches.append({
                'date': dt.strftime('%b %d %I:%M %p'),
                'captain': entry.get('logged_by', ''),
                'spot': entry.get('spot', ''),
                'species': entry.get('species', ''),
                'technique': entry.get('technique', ''),
                'lure': entry.get('lure', ''),
                'notes': entry.get('notes', ''),
                'gps': entry.get('gps'),
                'conditions': entry.get('conditions', {}),
            })
        except Exception:
            pass
    return jsonify({'catches': catches[:100]})

@app.route('/api/groups/<int:group_id>/leave', methods=['POST'])
@login_required
def api_group_leave(group_id):
    db = get_db()
    member = db.execute('SELECT * FROM group_members WHERE group_id = ? AND username = ?',
                        (group_id, session['username'])).fetchone()
    if not member:
        return jsonify({'error': 'Not a member'}), 403
    if member['role'] == 'captain':
        count = db.execute('SELECT COUNT(*) as c FROM group_members WHERE group_id = ?',
                           (group_id,)).fetchone()['c']
        if count > 1:
            return jsonify({'error': 'Assign a new captain before leaving'}), 400
        db.execute('DELETE FROM friend_groups WHERE id = ?', (group_id,))
    db.execute('DELETE FROM group_members WHERE group_id = ? AND username = ?',
               (group_id, session['username']))
    db.commit()
    return jsonify({'left': True})


@app.route('/api/groups/<int:group_id>/transfer', methods=['POST'])
@login_required
def api_group_transfer_captain(group_id):
    """Transfer captain role to another member."""
    db = get_db()

    # Verify requester is the captain
    member = db.execute(
        'SELECT * FROM group_members WHERE group_id = ? AND username = ?',
        (group_id, session['username'])).fetchone()
    if not member:
        return jsonify({'error': 'Not a member'}), 403
    if member['role'] != 'captain':
        return jsonify({'error': 'Only the captain can transfer ownership'}), 403

    data = request.get_json()
    new_captain = data.get('username', '').strip() if data else ''
    if not new_captain:
        return jsonify({'error': 'Username required'}), 400

    # Verify new captain is a member
    target = db.execute(
        'SELECT * FROM group_members WHERE group_id = ? AND username = ?',
        (group_id, new_captain)).fetchone()
    if not target:
        return jsonify({'error': f'{new_captain} is not in this crew'}), 404

    # Transfer
    db.execute(
        'UPDATE group_members SET role = ? WHERE group_id = ? AND username = ?',
        ('member', group_id, session['username']))
    db.execute(
        'UPDATE group_members SET role = ? WHERE group_id = ? AND username = ?',
        ('captain', group_id, new_captain))
    db.commit()

    logger.info(f'Captain transfer: {session["username"]} -> {new_captain} in group {group_id}')
    return jsonify({'ok': True, 'new_captain': new_captain})


# ==================== NOTIFICATIONS ====================

@app.route('/api/notifications', methods=['GET'])
@login_required
def api_notifications():
    db = get_db()
    rows = db.execute('''
        SELECT * FROM group_notifications
        WHERE to_user = ? AND read = 0
        ORDER BY created_at DESC LIMIT 20
    ''', (session['username'],)).fetchall()
    return jsonify({'notifications': [dict(r) for r in rows]})

@app.route('/api/notifications/read', methods=['POST'])
@login_required
def api_notifications_mark_read():
    db = get_db()
    db.execute('UPDATE group_notifications SET read = 1 WHERE to_user = ?',
               (session['username'],))
    db.commit()
    return jsonify({'ok': True})


# ==================== FRIEND FINDER ====================

@app.route('/api/location/update', methods=['POST'])
@login_required
def api_location_update():
    """Captain posts their current position. Called every 60s."""
    data = request.get_json()
    if not data or 'lat' not in data or 'lon' not in data:
        return jsonify({'error': 'lat/lon required'}), 400

    lat = float(data['lat'])
    lon = float(data['lon'])
    accuracy = float(data.get('accuracy', 0))
    sharing = 1 if data.get('sharing', True) else 0
    group_id = data.get('group_id')

    db = get_db()
    db.execute('''
        INSERT INTO location_updates
            (username, lat, lon, accuracy, sharing, sharing_group_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(username) DO UPDATE SET
            lat = excluded.lat,
            lon = excluded.lon,
            accuracy = excluded.accuracy,
            sharing = excluded.sharing,
            sharing_group_id = excluded.sharing_group_id,
            updated_at = CURRENT_TIMESTAMP
    ''', (session['username'], lat, lon, accuracy, sharing, group_id))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/location/sharing', methods=['POST'])
@login_required
def api_location_toggle():
    """Toggle sharing on/off or change which crew sees you."""
    data = request.get_json()
    sharing = 1 if data.get('sharing') else 0
    group_id = data.get('group_id')
    db = get_db()
    db.execute('''
        INSERT INTO location_updates
            (username, lat, lon, sharing, sharing_group_id, updated_at)
        VALUES (?, 0, 0, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(username) DO UPDATE SET
            sharing = excluded.sharing,
            sharing_group_id = excluded.sharing_group_id,
            updated_at = CURRENT_TIMESTAMP
    ''', (session['username'], sharing, group_id))
    db.commit()
    return jsonify({'sharing': bool(sharing), 'group_id': group_id})


@app.route('/api/location/crew', methods=['GET'])
@login_required
def api_location_crew():
    """
    Get live positions of crew members visible to the requesting captain,
    plus their catches logged today.
    """
    db = get_db()
    username = session['username']

    my_groups = db.execute(
        'SELECT group_id FROM group_members WHERE username = ?',
        (username,)).fetchall()
    my_group_ids = [r['group_id'] for r in my_groups]

    if not my_group_ids:
        return jsonify({'members': [], 'catches': []})

    placeholders = ','.join(['?' for _ in my_group_ids])
    cutoff = (datetime.now() - timedelta(hours=2)).isoformat()

    members = db.execute(f'''
        SELECT lu.username, lu.lat, lu.lon, lu.accuracy, lu.updated_at,
               lu.sharing_group_id
        FROM location_updates lu
        WHERE lu.username != ?
          AND lu.sharing = 1
          AND lu.sharing_group_id IN ({placeholders})
          AND lu.updated_at > ?
          AND lu.lat != 0 AND lu.lon != 0
    ''', [username] + my_group_ids + [cutoff]).fetchall()

    from captain_advisor import LOGS_DIR
    today = datetime.now().strftime('%Y-%m-%d')
    sharing_usernames = set(r['username'] for r in members)

    catches = []
    for fp in globmod.glob(os.path.join(LOGS_DIR, 'catch_*.json')):
        try:
            with open(fp) as f:
                entry = json.load(f)
            if entry.get('logged_by') not in sharing_usernames:
                continue
            if not entry.get('timestamp', '').startswith(today):
                continue
            catches.append({
                'username': entry.get('logged_by', ''),
                'species': entry.get('species', ''),
                'technique': entry.get('technique', ''),
                'lure': entry.get('lure', ''),
                'gps': entry.get('gps'),
                'time': entry.get('timestamp', '')[11:16],
            })
        except Exception:
            pass

    return jsonify({
        'members': [dict(m) for m in members],
        'catches': catches,
    })


@app.route('/api/location/status', methods=['GET'])
@login_required
def api_location_status():
    """Get current sharing status for this captain."""
    db = get_db()
    row = db.execute(
        'SELECT sharing, sharing_group_id FROM location_updates WHERE username = ?',
        (session['username'],)).fetchone()
    return jsonify({
        'sharing': bool(row['sharing']) if row else True,
        'group_id': row['sharing_group_id'] if row else None,
    })


# ==================== SMS WEBHOOK ====================

def _looks_like_catch(text):
    catch_keywords = [
        'caught', 'landed', 'got a', 'nice', 'striper', 'bluefish', 'bass',
        'albie', 'false albacore', 'bonito', 'fluke', 'tuna', 'lb', 'pound',
        'inch', 'bucktail', 'eel', 'plug', 'jig', 'popper', 'slug-go',
        'stonehorse', 'bearse', 'pollock rip', 'monomoy', 'stage harbor',
        'nauset', 'chatham', 'high bank'
    ]
    question_keywords = [
        '?', 'what', 'how', 'when', 'where', 'should', 'will', 'can',
        'conditions', 'tide', 'weather', 'wind', 'ride', 'leaving',
        'heading', 'going', 'plan', 'best', 'recommend'
    ]
    text_lower = text.lower()
    catch_score = sum(1 for k in catch_keywords if k in text_lower)
    question_score = sum(1 for k in question_keywords if k in text_lower)
    return catch_score > question_score and catch_score >= 2

def get_sms_history(phone_number, max_exchanges=8):
    db = get_db()
    row = db.execute('SELECT history FROM sms_sessions WHERE phone_number = ?',
                     (phone_number,)).fetchone()
    if not row:
        return []
    try:
        history = json.loads(row['history'])
        if len(history) > max_exchanges * 2:
            history = history[-(max_exchanges * 2):]
        return history
    except Exception:
        return []

def save_sms_history(phone_number, history):
    db = get_db()
    db.execute('''
        INSERT INTO sms_sessions (phone_number, history, last_active, message_count)
        VALUES (?, ?, CURRENT_TIMESTAMP, 1)
        ON CONFLICT(phone_number) DO UPDATE SET
            history = excluded.history,
            last_active = CURRENT_TIMESTAMP,
            message_count = message_count + 1
    ''', (phone_number, json.dumps(history)))
    db.commit()

def log_sms(phone_number, direction, body, twilio_sid=None):
    db = get_db()
    db.execute('''
        INSERT INTO sms_conversations (phone_number, direction, body, twilio_sid)
        VALUES (?, ?, ?, ?)
    ''', (phone_number, direction, body, twilio_sid))
    db.commit()

@app.route('/api/sms/inbound', methods=['POST'])
def sms_inbound():
    """Twilio webhook — receives inbound SMS."""
    if TWILIO_TOKEN:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(TWILIO_TOKEN)
        signature = request.headers.get('X-Twilio-Signature', '')
        if not validator.validate(request.url, request.form.to_dict(), signature):
            logger.warning('Invalid Twilio signature — rejected')
            return ('Forbidden', 403)

    from_number = request.form.get('From', '')
    body = request.form.get('Body', '').strip()
    twilio_sid = request.form.get('MessageSid', '')

    if not from_number or not body:
        return ('', 204)

    logger.info(f'SMS inbound from {from_number[-4:]}****: {body[:50]}')
    log_sms(from_number, 'inbound', body, twilio_sid)

    db = get_db()
    user = db.execute(
        'SELECT * FROM users WHERE phone_number = ? AND phone_verified = 1',
        (from_number,)).fetchone()
    username = user['username'] if user else None

    body_lower = body.lower().strip()

    if body_lower in ('stop', 'unsubscribe'):
        return ('', 204)

    if body_lower in ('reset', 'restart', 'clear'):
        save_sms_history(from_number, [])
        name = f" {username}" if username else ""
        sms_reply(from_number,
            f"Wheelhouse ready{name}. Ask me about conditions, tides, "
            f"currents, or where to fish. Or just text a catch to log it.")
        return ('', 204)

    if body_lower in ('help', '?'):
        sms_reply(from_number,
            "Wheelhouse commands:\n"
            "- Ask any fishing question\n"
            "- Log a catch: \"28lb striper Stonehorse white bucktail\"\n"
            "- RESET - clear conversation history\n"
            "- STATUS - check your account\n\n"
            + ("" if username else
               "Register at wheelhouse.rednun.com to link your account."))
        return ('', 204)

    if body_lower == 'status':
        if username:
            sms_reply(from_number,
                f"Logged in as {username}.\n"
                f"Your catches and conversations are synced to your account.")
        else:
            sms_reply(from_number,
                "Not linked to an account. Register at wheelhouse.rednun.com "
                "and add your phone number in settings to sync your data.")
        return ('', 204)

    # Catch logging via SMS
    if username and _looks_like_catch(body):
        try:
            from captain_advisor import ask_advisor, ANTHROPIC_API_KEY, ANTHROPIC_URL, MODEL
            import requests as req

            parse_prompt = (
                f'Parse this fishing catch report into structured fields. '
                f'Respond ONLY with valid JSON, no markdown:\n'
                f'{{"spot":"","species":"","technique":"","lure":"","notes":""}}\n\n'
                f'Transcript: "{body}"'
            )
            r = req.post(ANTHROPIC_URL,
                headers={'Content-Type':'application/json',
                         'x-api-key': ANTHROPIC_API_KEY,
                         'anthropic-version': '2023-06-01'},
                json={'model': MODEL, 'max_tokens': 300,
                      'messages': [{'role':'user','content': parse_prompt}]},
                timeout=15)
            r.raise_for_status()
            text = ''.join(b['text'] for b in r.json().get('content',[]) if b.get('type')=='text')
            parsed = json.loads(text.strip())

            from captain_advisor import _snapshot_conditions, LOGS_DIR
            conditions = _snapshot_conditions()
            entry = {
                'timestamp': datetime.now().isoformat(),
                'logged_by': username,
                'spot': parsed.get('spot',''),
                'gps': None,
                'species': parsed.get('species',''),
                'technique': parsed.get('technique',''),
                'lure': parsed.get('lure',''),
                'notes': parsed.get('notes', body),
                'conditions': conditions,
                'source': 'sms',
            }
            ts = datetime.now().strftime('%Y-%m-%d_%H%M%S')
            filepath = os.path.join(LOGS_DIR, f'catch_{ts}.json')
            with open(filepath, 'w') as f:
                json.dump(entry, f, indent=2)

            logger.info(f'SMS catch logged for {username}: {parsed.get("spot","")} {parsed.get("species","")}')

            # Notify Friend Groups via SMS
            try:
                groups = db.execute('''
                    SELECT g.id, g.name FROM friend_groups g
                    JOIN group_members gm ON g.id = gm.group_id
                    WHERE gm.username = ? AND gm.share_my_catches = 1
                ''', (username,)).fetchall()
                for group in groups:
                    members = db.execute('''
                        SELECT u.phone_number, gm.username
                        FROM group_members gm
                        JOIN users u ON u.username = gm.username
                        WHERE gm.group_id = ? AND gm.username != ?
                        AND gm.share_my_catches = 1 AND u.phone_verified = 1
                    ''', (group['id'], username)).fetchall()
                    for member in members:
                        species = entry.get('species','a fish')
                        spot = entry.get('spot','')
                        msg = f"[{group['name']}] {username} just logged {species}"
                        if spot: msg += f" at {spot}"
                        sms_reply(member['phone_number'], msg)
            except Exception as e:
                logger.error(f'Group SMS notification failed: {e}')

            confirm = f"Logged: {parsed.get('species','catch')}"
            if parsed.get('spot'): confirm += f" at {parsed['spot']}"
            if parsed.get('technique'): confirm += f", {parsed['technique']}"
            confirm += ". Check wheelhouse.rednun.com to see it."
            sms_reply(from_number, confirm)
            log_sms(from_number, 'outbound', confirm)
            return ('', 204)

        except Exception as e:
            logger.error(f'SMS catch parse failed: {e}')

    # Default — send to advisor
    history = get_sms_history(from_number)
    try:
        from captain_advisor import ask_advisor
        response = ask_advisor(history, body)
    except Exception as e:
        logger.error(f'Advisor failed for SMS: {e}')
        response = "Sorry, the advisor is temporarily unavailable. Try again in a moment."

    history.append({'role': 'user', 'content': body})
    history.append({'role': 'assistant', 'content': response})
    if len(history) > 16:
        history = history[-16:]
    save_sms_history(from_number, history)

    log_sms(from_number, 'outbound', response)
    sms_reply(from_number, response)
    return ('', 204)


# ==================== SMS ADMIN API ====================

# Admin username — determined at startup from oldest account
_ADMIN_USERNAME = None
def _get_admin_username():
    global _ADMIN_USERNAME
    if _ADMIN_USERNAME is None:
        try:
            db = sqlite3.connect(DB_PATH)
            db.row_factory = sqlite3.Row
            user = db.execute('SELECT username FROM users WHERE is_admin = 1 LIMIT 1').fetchone()
            _ADMIN_USERNAME = user['username'] if user else ''
            db.close()
        except Exception:
            _ADMIN_USERNAME = ''
    return _ADMIN_USERNAME

@app.route('/api/sms/conversations')
@login_required
def api_sms_conversations():
    if session.get('username') != _get_admin_username():
        return jsonify({'error': 'Admin only'}), 403
    db = get_db()
    sessions_list = db.execute('''
        SELECT phone_number, last_active, message_count
        FROM sms_sessions
        ORDER BY last_active DESC
        LIMIT 50
    ''').fetchall()
    result = []
    for s in sessions_list:
        last = db.execute('''
            SELECT body, direction, created_at FROM sms_conversations
            WHERE phone_number = ?
            ORDER BY created_at DESC LIMIT 1
        ''', (s['phone_number'],)).fetchone()
        result.append({
            'phone': s['phone_number'][-4:] + '****',
            'phone_full': s['phone_number'],
            'last_active': s['last_active'],
            'message_count': s['message_count'],
            'last_message': dict(last) if last else None,
        })
    return jsonify({'conversations': result})

@app.route('/api/sms/conversation/<path:phone>')
@login_required
def api_sms_conversation_detail(phone):
    if session.get('username') != _get_admin_username():
        return jsonify({'error': 'Admin only'}), 403
    db = get_db()
    messages = db.execute('''
        SELECT direction, body, created_at, twilio_sid
        FROM sms_conversations
        WHERE phone_number = ?
        ORDER BY created_at ASC
    ''', (phone,)).fetchall()
    return jsonify({'messages': [dict(m) for m in messages]})

@app.route('/api/sms/stats')
@login_required
def api_sms_stats():
    if session.get('username') != _get_admin_username():
        return jsonify({'error': 'Admin only'}), 403
    db = get_db()
    total_convos = db.execute('SELECT COUNT(*) as c FROM sms_sessions').fetchone()['c']
    total_msgs = db.execute('SELECT COUNT(*) as c FROM sms_conversations').fetchone()['c']
    inbound = db.execute("SELECT COUNT(*) as c FROM sms_conversations WHERE direction='inbound'").fetchone()['c']
    outbound = db.execute("SELECT COUNT(*) as c FROM sms_conversations WHERE direction='outbound'").fetchone()['c']
    today = db.execute("""
        SELECT COUNT(*) as c FROM sms_conversations
        WHERE DATE(created_at) = DATE('now')
    """).fetchone()['c']
    return jsonify({
        'total_conversations': total_convos,
        'total_messages': total_msgs,
        'inbound': inbound,
        'outbound': outbound,
        'messages_today': today,
    })


# ==================== LEGAL PAGES (Twilio A2P compliance) ====================

@app.route('/update')
def update_page():
    return send_from_directory('static', 'update.html')

@app.route('/privacy')
def privacy():
    return render_legal_page('privacy')

@app.route('/terms')
def terms():
    return render_legal_page('terms')

def render_legal_page(page_type):
    if page_type == 'privacy':
        title = 'Privacy Policy'
        content = '''
        <h2>Privacy Policy</h2>
        <p class="updated">Last updated: April 2026</p>

        <h3>What We Collect</h3>
        <p>Wheelhouse collects the information you provide when creating an account
        (username, password), your phone number if you choose to register it, catch
        logs you submit including location names and optional GPS coordinates, and
        conversation history with the Wheelhouse advisor.</p>

        <h3>How We Use It</h3>
        <p>Your data is used solely to operate the Wheelhouse service — to authenticate
        you, provide fishing intelligence, save your catch logs, and send SMS messages
        you have requested. We do not sell, share, or license your data to third parties.</p>

        <h3>SMS Messaging</h3>
        <p>By registering your phone number you consent to receive SMS messages from
        Wheelhouse including verification codes, advisor responses to your questions,
        and Friend Group catch notifications from captains you have chosen to connect
        with. Message and data rates may apply. Text STOP at any time to unsubscribe.
        Text HELP for support.</p>

        <h3>Catch Log Data</h3>
        <p>Your catch logs including spot names and GPS coordinates are private to your
        account by default.</p>

        <p>When you join a Friend Group and enable catch sharing, the following
        information becomes visible to other members of that group: spot name,
        species, technique, lure, notes, GPS coordinates, and conditions data.
        GPS is shared within your crew because you have explicitly opted in to
        share with those specific captains. You control this per crew at any
        time from the crew settings.</p>

        <p>GPS coordinates shared within a Friend Group are never used in
        aggregate analysis and are never visible to anyone outside that specific
        group — including other crews you belong to.</p>

        <h3>Friend Finder</h3>
        <p>Wheelhouse includes an optional live location feature called Friend Finder.
        When enabled, your GPS coordinates are shared in real time with members of
        a specific crew you choose at the start of each session. You control which
        crew can see you, and you can turn sharing off at any time from the map screen.</p>

        <p>Location sharing is separate from catch log sharing. Enabling Friend Finder
        does not automatically share your catch logs, and sharing your catches with a
        crew does not automatically enable Friend Finder. Each is controlled
        independently.</p>

        <p>When Friend Finder is active, crew members can see your live position and
        your catches logged today including GPS coordinates. When you disable sharing
        or close the app, your position is removed from the map within 2 hours.</p>

        <p>Your location data is never used in aggregate analysis, never shared outside
        your chosen crew, and is not stored beyond 2 hours of inactivity.</p>

        <h3>Aggregate Analysis</h3>
        <p>Anonymized catch data — species and technique only — may be used in
        aggregate to improve fishing pattern predictions for all users. No GPS
        coordinates, spot names, lure selections, notes, or usernames are included
        in aggregate analysis. No individual catch data is attributable to you.</p>

        <h3>Data Storage</h3>
        <p>Your data is stored on a private server located in Chatham, Massachusetts.
        We do not use third-party cloud storage for your personal data.</p>

        <h3>Third-Party Services</h3>
        <p>Wheelhouse uses Twilio to deliver SMS messages and Anthropic\'s Claude API
        to power the fishing advisor. Your messages may pass through these services
        to fulfill your requests.
        <a href="https://www.twilio.com/legal/privacy" target="_blank">Twilio Privacy Policy</a> &middot;
        <a href="https://www.anthropic.com/privacy" target="_blank">Anthropic Privacy Policy</a></p>

        <h3>Data Retention</h3>
        <p>Your data is retained as long as your account is active. You may request
        deletion of your account and all associated data by contacting
        <a href="mailto:mgiorgio@rednun.com">mgiorgio@rednun.com</a>.</p>

        <h3>Contact</h3>
        <p>For privacy questions or data deletion requests:<br>
        <a href="mailto:mgiorgio@rednun.com">mgiorgio@rednun.com</a></p>
        '''
    else:
        title = 'Terms of Service'
        content = '''
        <h2>Terms of Service</h2>
        <p class="updated">Last updated: April 2026</p>

        <h3>Acceptance</h3>
        <p>By creating an account or using Wheelhouse, you agree to these terms.
        If you do not agree, do not use the service.</p>

        <h3>The Service</h3>
        <p>Wheelhouse provides AI-assisted fishing intelligence including tidal data,
        weather conditions, oceanographic analysis, and fishing recommendations for
        the Monomoy / Chatham, Massachusetts area. The service is intended for use
        by licensed charter boat captains and recreational anglers.</p>

        <h3>No Warranty</h3>
        <p>Wheelhouse is provided as-is. Fishing recommendations, conditions
        forecasts, and advisor responses are informational only and do not
        constitute professional maritime or safety advice. Always exercise your
        own judgment on the water. Weather and ocean conditions can change rapidly.
        The operator of Wheelhouse is not responsible for decisions made based on
        information provided by this service.</p>

        <h3>Safety</h3>
        <p>Nothing in this service overrides your responsibility to follow safe
        boating practices, applicable maritime regulations, and your own judgment
        regarding sea conditions. Always file a float plan. Always carry appropriate
        safety equipment.</p>

        <h3>SMS Messaging</h3>
        <p>By registering your phone number you agree to receive SMS messages from
        Wheelhouse as described in our Privacy Policy. Message and data rates may
        apply. You can opt out at any time by texting STOP. For help text HELP or
        contact mgiorgio@rednun.com.</p>

        <h3>Accounts</h3>
        <p>You are responsible for maintaining the confidentiality of your account
        credentials. You may not share your account or use the service to provide
        access to unauthorized users.</p>

        <h3>Catch Data</h3>
        <p>You retain ownership of the catch data you log. By submitting catch data
        you grant Wheelhouse a limited license to use that data in anonymized
        aggregate form to improve predictions for all users, as described in the
        Privacy Policy.</p>

        <h3>Prohibited Use</h3>
        <p>You may not use Wheelhouse to violate any applicable fishing regulations,
        licensing requirements, or maritime law. You may not attempt to reverse
        engineer, scrape, or abuse the service.</p>

        <h3>Changes</h3>
        <p>These terms may be updated at any time. Continued use of the service
        after changes constitutes acceptance of the updated terms.</p>

        <h3>Contact</h3>
        <p><a href="mailto:mgiorgio@rednun.com">mgiorgio@rednun.com</a></p>
        '''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Wheelhouse — {title}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #f0f4f8;
      color: #1a2a3a;
      font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
      font-size: 16px;
      line-height: 1.7;
      padding: 0;
    }}
    header {{
      background: #ffffff;
      border-bottom: 1px solid #c8d6e0;
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .logo {{
      font-size: 18px;
      font-weight: 700;
      letter-spacing: 2px;
      color: #1a2a3a;
      text-decoration: none;
    }}
    .logo span {{ color: #0077aa; }}
    .back-link {{
      font-size: 13px;
      color: #0077aa;
      text-decoration: none;
    }}
    .back-link:hover {{ text-decoration: underline; }}
    main {{
      max-width: 720px;
      margin: 0 auto;
      padding: 40px 24px 80px;
    }}
    h2 {{
      font-size: 26px;
      font-weight: 700;
      color: #1a2a3a;
      margin-bottom: 6px;
    }}
    h3 {{
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 1.5px;
      margin-top: 28px;
      margin-bottom: 8px;
      text-transform: uppercase;
      color: #0077aa;
    }}
    p {{
      color: #2a3a4a;
      margin-bottom: 12px;
      font-size: 15px;
    }}
    .updated {{
      font-size: 13px;
      color: #7a8a9a;
      margin-bottom: 32px;
    }}
    a {{
      color: #0077aa;
      text-decoration: none;
    }}
    a:hover {{ text-decoration: underline; }}
    footer {{
      text-align: center;
      padding: 24px;
      font-size: 12px;
      color: #9aa8b8;
      border-top: 1px solid #c8d6e0;
      background: #fff;
    }}
  </style>
</head>
<body>
  <header>
    <a class="logo" href="/">&#9875; WHEEL<span>HOUSE</span></a>
    <a class="back-link" href="/">&#8592; Back to app</a>
  </header>
  <main>
    {content}
  </main>
  <footer>
    &copy; 2026 Wheelhouse &middot; Chatham, MA &middot;
    <a href="/privacy">Privacy</a> &middot;
    <a href="/terms">Terms</a>
  </footer>
</body>
</html>'''

    return html


# ==================== ADMIN DASHBOARD ====================

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.cookies.get('wh_admin')
        if not auth or auth != ADMIN_PASSWORD or not ADMIN_PASSWORD:
            return redirect('/admin/login')
        return f(*args, **kwargs)
    return decorated


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        pw = request.form.get('password', '')
        if pw == ADMIN_PASSWORD and ADMIN_PASSWORD:
            resp = redirect('/admin')
            resp.set_cookie('wh_admin', pw, httponly=True, samesite='Strict', max_age=86400*30)
            return resp
        error = 'Wrong password'
    return f'''<!DOCTYPE html>
<html><head><title>Wheelhouse Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{background:#111214;color:#fff;font-family:-apple-system,sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
  .box{{background:#1C1C1E;border-radius:16px;padding:32px;width:320px;border:1px solid #2C2C2E}}
  h2{{margin:0 0 24px;font-size:20px;letter-spacing:-0.5px}}
  input{{width:100%;background:#2C2C2E;border:none;border-radius:10px;
         padding:12px 14px;color:#fff;font-size:15px;box-sizing:border-box;margin-bottom:12px}}
  button{{width:100%;background:#0A84FF;border:none;border-radius:10px;
          padding:12px;color:#fff;font-size:15px;font-weight:600;cursor:pointer}}
  .err{{color:#FF453A;font-size:13px;margin-bottom:12px}}
</style></head><body>
<div class="box">
  <h2>Wheelhouse Admin</h2>
  {"<div class='err'>" + error + "</div>" if error else ""}
  <form method="POST">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Sign In</button>
  </form>
</div></body></html>'''


@app.route('/admin/logout')
def admin_logout():
    resp = redirect('/admin/login')
    resp.delete_cookie('wh_admin')
    return resp


@app.route('/admin')
@admin_required
def admin_dashboard():
    db = get_db()

    # Users
    users = db.execute('''
        SELECT username, created_at, is_admin,
               phone_number, phone_verified,
               (SELECT COUNT(*) FROM group_members gm WHERE gm.username = u.username) as crew_count
        FROM users u
        ORDER BY created_at DESC
    ''').fetchall()

    # Crews
    groups = db.execute('''
        SELECT g.id, g.name, g.code, g.created_at,
               COUNT(gm.username) as member_count,
               (SELECT username FROM group_members WHERE group_id = g.id AND role = 'captain' LIMIT 1) as captain
        FROM friend_groups g
        LEFT JOIN group_members gm ON gm.group_id = g.id
        GROUP BY g.id
        ORDER BY g.created_at DESC
    ''').fetchall()

    # Crew members per group
    group_members_map = {}
    for grp in groups:
        members = db.execute('''
            SELECT username, role, share_my_catches as sharing_enabled,
                   joined_at
            FROM group_members
            WHERE group_id = ?
            ORDER BY role DESC, joined_at ASC
        ''', (grp['id'],)).fetchall()
        group_members_map[grp['id']] = [dict(m) for m in members]

    # SMS stats (safe queries)
    try:
        sms_total = db.execute('SELECT COUNT(*) FROM sms_conversations').fetchone()[0]
        sms_inbound = db.execute(
            "SELECT COUNT(*) FROM sms_conversations WHERE direction='inbound'"
        ).fetchone()[0]
        sms_unique = db.execute(
            'SELECT COUNT(DISTINCT phone_number) FROM sms_conversations'
        ).fetchone()[0]
    except Exception:
        sms_total = sms_inbound = sms_unique = 0

    # SMS trial users (table may not exist)
    trial_users = []
    try:
        trial_users = db.execute(
            'SELECT phone_number, message_count, first_seen FROM sms_trial ORDER BY message_count DESC'
        ).fetchall()
    except Exception:
        pass

    # Catch logs
    catch_files = globmod.glob('/opt/wheelhouse/logs/catch_*.json')
    catch_count = len(catch_files)
    catches_by_user = {}
    for fp in catch_files:
        try:
            with open(fp) as f:
                e = json.load(f)
            u = e.get('logged_by', 'unknown')
            catches_by_user[u] = catches_by_user.get(u, 0) + 1
        except Exception:
            pass

    # Conditions log
    try:
        conditions_count = db.execute('SELECT COUNT(*) FROM conditions_log').fetchone()[0]
        latest_conditions = db.execute(
            'SELECT date, snapshot_hour, sst_gradient_f, tide_direction, solunar_rating '
            'FROM conditions_log ORDER BY logged_at DESC LIMIT 1'
        ).fetchone()
    except Exception:
        conditions_count = 0
        latest_conditions = None

    # Location sharing — who is currently active
    cutoff = (datetime.now() - timedelta(hours=2)).isoformat()
    active_locations = db.execute('''
        SELECT username, sharing, sharing_group_id, updated_at
        FROM location_updates
        WHERE updated_at > ? AND lat != 0
        ORDER BY updated_at DESC
    ''', (cutoff,)).fetchall()

    # Pattern engine status
    try:
        from pattern_intel import get_pattern_prediction
        pattern_status = get_pattern_prediction()
    except Exception as e:
        pattern_status = {'status': 'error', 'message': str(e)}

    def row(label, value, color='#fff'):
        return (f'<tr><td style="color:#8E8E93;padding:8px 0;font-size:13px;width:180px">{label}</td>'
                f'<td style="color:{color};font-size:13px;font-weight:500">{value}</td></tr>')

    def section(title):
        return (f'<h2 style="font-size:16px;font-weight:600;color:#fff;margin:32px 0 12px;'
                f'padding-bottom:8px;border-bottom:1px solid #2C2C2E">{title}</h2>')

    def card(content):
        return (f'<div style="background:#1C1C1E;border-radius:14px;padding:16px 20px;'
                f'margin-bottom:12px;border:1px solid #2C2C2E">{content}</div>')

    # Latest conditions string
    if latest_conditions:
        lc_str = (f"{latest_conditions['date']} {latest_conditions['snapshot_hour']}:00 "
                  f"&mdash; {latest_conditions['tide_direction'] or 'n/a'}, "
                  f"gradient {latest_conditions['sst_gradient_f'] or 'n/a'}&deg;F")
    else:
        lc_str = 'None'

    # Pattern summary (truncate if long)
    p_summary = pattern_status.get('summary', '&mdash;')
    if len(p_summary) > 120:
        p_summary = p_summary[:120] + '...'
    p_color = '#34C759' if pattern_status.get('status') == 'ok' else '#FF9F0A'

    html = f'''<!DOCTYPE html>
<html><head><title>Wheelhouse Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#111214;color:#fff;font-family:-apple-system,sans-serif;padding:24px;max-width:900px;margin:0 auto}}
  table{{width:100%;border-collapse:collapse}}
  .pill{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}}
  .green{{background:#34C75918;color:#34C759;border:1px solid #34C75930}}
  .gray{{background:#3A3A3C;color:#8E8E93}}
  .blue{{background:#0A84FF18;color:#0A84FF;border:1px solid #0A84FF30}}
  .red{{background:#FF453A18;color:#FF453A;border:1px solid #FF453A30}}
  a{{color:#0A84FF;text-decoration:none}}
</style></head><body>

<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
  <div>
    <div style="font-size:28px;font-weight:700;letter-spacing:-0.5px">Admin</div>
    <div style="color:#8E8E93;font-size:13px;margin-top:2px">Wheelhouse &middot; {datetime.now().strftime("%b %d %Y %H:%M")}</div>
  </div>
  <a href="/admin/logout" style="color:#8E8E93;font-size:13px">Sign out</a>
</div>

{section("Overview")}
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:8px">
  <div style="background:#1C1C1E;border-radius:12px;padding:14px;border:1px solid #2C2C2E">
    <div style="color:#8E8E93;font-size:11px;text-transform:uppercase;letter-spacing:0.4px">Captains</div>
    <div style="color:#fff;font-size:26px;font-weight:700;margin-top:4px">{len(users)}</div>
  </div>
  <div style="background:#1C1C1E;border-radius:12px;padding:14px;border:1px solid #2C2C2E">
    <div style="color:#8E8E93;font-size:11px;text-transform:uppercase;letter-spacing:0.4px">Crews</div>
    <div style="color:#fff;font-size:26px;font-weight:700;margin-top:4px">{len(groups)}</div>
  </div>
  <div style="background:#1C1C1E;border-radius:12px;padding:14px;border:1px solid #2C2C2E">
    <div style="color:#8E8E93;font-size:11px;text-transform:uppercase;letter-spacing:0.4px">Catches logged</div>
    <div style="color:#fff;font-size:26px;font-weight:700;margin-top:4px">{catch_count}</div>
  </div>
  <div style="background:#1C1C1E;border-radius:12px;padding:14px;border:1px solid #2C2C2E">
    <div style="color:#8E8E93;font-size:11px;text-transform:uppercase;letter-spacing:0.4px">SMS messages</div>
    <div style="color:#fff;font-size:26px;font-weight:700;margin-top:4px">{sms_total}</div>
  </div>
</div>

{section("Pattern Engine")}
{card(f"""<table>
  {row("Status", pattern_status.get('status','unknown'), p_color)}
  {row("Conditions logged", str(conditions_count))}
  {row("Latest snapshot", lc_str)}
  {row("Pattern summary", p_summary)}
</table>""")}

{section("Data Feeds")}
<div id="feed-status" style="background:#1C1C1E;border-radius:14px;padding:16px 20px;margin-bottom:12px;border:1px solid #2C2C2E">
  <div style="color:#48484A;font-size:13px">Loading feed status...</div>
</div>
<script>
fetch('/api/fishing/feed-status')
  .then(function(r){{ return r.json(); }})
  .then(function(data){{
    var html = '<table style="width:100%;border-collapse:collapse">';
    data.feeds.forEach(function(f){{
      var color = f.status === 'ok' ? '#34C759' : f.status === 'stale' ? '#FF9F0A' : f.status === 'no data' ? '#8E8E93' : '#FF453A';
      var pill = '<span class="pill" style="background:' + color + '18;color:' + color + ';border:1px solid ' + color + '30">' + f.status + '</span>';
      var age = f.age ? '<span style="color:#8E8E93;font-size:12px"> &middot; ' + f.age + '</span>' : '';
      html += '<tr><td style="color:#8E8E93;padding:6px 0;font-size:13px;width:220px">' + f.name + '</td>';
      html += '<td style="font-size:13px">' + pill + age + '</td></tr>';
    }});
    html += '</table>';
    document.getElementById('feed-status').innerHTML = html;
  }})
  .catch(function(){{
    document.getElementById('feed-status').innerHTML = '<div style="color:#FF453A;font-size:13px">Failed to load feed status</div>';
  }});
</script>

{section(f"Captains ({len(users)})")}
'''

    for u in users:
        phone_status = ('<span class="pill green">Verified</span>' if u['phone_verified'] else
                       f'<span class="pill gray">{u["phone_number"] or "No phone"}</span>')
        catch_ct = catches_by_user.get(u['username'], 0)
        admin_pill = '<span class="pill blue">admin</span> ' if u['is_admin'] else ''
        html += card(f'''
<div style="display:flex;justify-content:space-between;align-items:flex-start">
  <div>
    <div style="font-size:15px;font-weight:600">{u["username"]}</div>
    <div style="color:#8E8E93;font-size:12px;margin-top:3px">Joined {str(u["created_at"])[:10]}</div>
  </div>
  <div style="text-align:right;display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end">
    {admin_pill}{phone_status}
    <span class="pill blue">{u["crew_count"]} crew{"s" if u["crew_count"] != 1 else ""}</span>
    <span class="pill {"green" if catch_ct > 0 else "gray"}">{catch_ct} catch{"es" if catch_ct != 1 else ""}</span>
  </div>
</div>''')

    # CREWS
    html += section(f"Crews ({len(groups)})")
    if groups:
        for grp in groups:
            members = group_members_map.get(grp['id'], [])
            member_rows = ''.join([
                f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:0.5px solid #2C2C2E">'
                f'<span style="color:#fff;font-size:13px">{m["username"]}</span>'
                f'<div style="display:flex;gap:6px">'
                f'<span class="pill {"blue" if m["role"]=="captain" else "gray"}">{m["role"]}</span>'
                f'<span class="pill {"green" if m["sharing_enabled"] else "gray"}">{"sharing" if m["sharing_enabled"] else "dark"}</span>'
                f'</div></div>'
                for m in members
            ])
            html += card(f'''
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
  <div>
    <div style="font-size:15px;font-weight:600">{grp["name"]}</div>
    <div style="color:#8E8E93;font-size:12px;margin-top:2px">Code: {grp["code"]} &middot; {grp["member_count"]} members &middot; created {str(grp["created_at"])[:10]}</div>
  </div>
</div>
{member_rows if member_rows else '<div style="color:#48484A;font-size:13px">No members</div>'}''')
    else:
        html += card('<div style="color:#48484A;font-size:13px">No crews created yet</div>')

    # SMS
    html += section("SMS")
    html += card(f'''<table>
  {row("Total messages", str(sms_total))}
  {row("Inbound messages", str(sms_inbound))}
  {row("Unique phone numbers", str(sms_unique))}
  {row("Trial users", str(len(trial_users)))}
</table>''')

    if trial_users:
        trial_rows = ''.join([
            f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:0.5px solid #2C2C2E">'
            f'<span style="color:#8E8E93;font-size:13px">{t["phone_number"]}</span>'
            f'<span class="pill {"red" if t["message_count"]>=4 else "gray"}">{t["message_count"]}/5 messages</span>'
            f'</div>'
            for t in trial_users
        ])
        html += card(f'<div style="font-size:13px;color:#8E8E93;margin-bottom:8px;text-transform:uppercase;'
                     f'letter-spacing:0.4px;font-size:11px">Trial numbers</div>{trial_rows}')

    # FRIEND FINDER
    html += section("Friend Finder &mdash; Currently Active")
    if active_locations:
        loc_rows = ''.join([
            f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:0.5px solid #2C2C2E">'
            f'<span style="color:#fff;font-size:13px">{loc["username"]}</span>'
            f'<div style="display:flex;gap:6px">'
            f'<span class="pill {"green" if loc["sharing"] else "gray"}">{"sharing" if loc["sharing"] else "dark"}</span>'
            f'<span style="color:#8E8E93;font-size:12px">{str(loc["updated_at"])[11:16]}</span>'
            f'</div></div>'
            for loc in active_locations
        ])
        html += card(loc_rows)
    else:
        html += card('<div style="color:#48484A;font-size:13px">No captains active on the water right now</div>')

    # CATCHES BY CAPTAIN
    if catches_by_user:
        html += section("Catch Log Activity")
        catch_rows = ''.join([
            f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:0.5px solid #2C2C2E">'
            f'<span style="color:#fff;font-size:13px">{u}</span>'
            f'<span class="pill green">{c} catch{"es" if c!=1 else ""}</span>'
            f'</div>'
            for u, c in sorted(catches_by_user.items(), key=lambda x: x[1], reverse=True)
        ])
        html += card(catch_rows)

    html += '</body></html>'
    return html


@app.route('/api/fishing/feed-status')
@admin_required
def api_feed_status():
    """Quick health check of all data feeds — just status, not values."""
    import time as _time
    from fishing_intel import _cache

    feeds = []
    now = _time.time()

    def check_feed(name, cache_key, ttl=1800):
        """Check if feed is cached and fresh. Cache-only — no live fetching."""
        if cache_key in _cache:
            val, ts = _cache[cache_key]
            age_sec = int(now - ts)
            status = 'ok' if age_sec < ttl else 'stale'
            mins = age_sec // 60
            age_str = f'{mins}m ago' if mins > 0 else 'just now'
            has_data = val is not None
            feeds.append({'name': name, 'status': status if has_data else 'empty', 'age': age_str})
        else:
            feeds.append({'name': name, 'status': 'no data', 'age': 'not yet fetched'})

    check_feed('NDBC Buoy 44018 (SE Cape Cod)', 'buoy_44018', 900)
    check_feed('NDBC Buoy 44020 (Nantucket Sound)', 'buoy_44020', 900)
    check_feed('WHOI Spotter (Chatham)', 'spot_buoy', 900)
    check_feed('NDBC Buoy 44090 (Cape Cod Bay)', 'buoy_44090', 900)
    check_feed('NWS Weather (Chatham)', 'weather', 1800)
    check_feed('NOAA Tides (Chatham)', 'tides_chatham', 3600)
    check_feed('NOAA Currents (Pollock Rip)', 'currents_pollock_rip', 3600)
    check_feed('NOAA Currents (Chatham Roads)', 'currents_chatham_roads', 3600)
    check_feed('ERDDAP SST/Chlorophyll', 'erddap_conditions', 3600)
    check_feed('AIS Vessel Tracking', 'ais_vessels', 43200)

    return jsonify({'feeds': feeds, 'checked': datetime.now().isoformat()})


# Register routes
from fishing_intel import register_routes as register_fishing_routes
from captain_advisor import register_advisor_routes

register_fishing_routes(app, login_required)
register_advisor_routes(app, login_required)

logger.info('Wheelhouse server initialized')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8090, debug=True)
