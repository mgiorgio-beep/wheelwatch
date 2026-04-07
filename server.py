import os
import sqlite3
import logging
import smtplib
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
            return render_auth_page('login', error='Enter username and password')
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session.permanent = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect('/')
        return render_auth_page('login', error='Invalid username or password')
    return render_auth_page('login')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        if not username or not password:
            return render_auth_page('signup', error='All fields required')
        if len(username) < 2 or len(username) > 20:
            return render_auth_page('signup', error='Username must be 2-20 characters')
        if not username.isalnum():
            return render_auth_page('signup', error='Username: letters and numbers only')
        if len(password) < 4:
            return render_auth_page('signup', error='Password must be at least 4 characters')
        if password != confirm:
            return render_auth_page('signup', error='Passwords don\'t match')
        db = get_db()
        existing = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if existing:
            return render_auth_page('signup', error='Username already taken')
        pw_hash = generate_password_hash(password)
        db.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (username, pw_hash))
        db.commit()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        session.permanent = True
        session['user_id'] = user['id']
        session['username'] = user['username']
        logger.info('New user registered: {}'.format(username))
        send_notification(
            '⚓ Wheelhouse — New User: {}'.format(username),
            'New account created on Wheelhouse.\n\nUsername: {}\nTime: {}\n\nhttps://wheelhouse.rednun.com'.format(
                username, datetime.now().strftime('%B %d, %Y %I:%M %p'))
        )
        return redirect('/')
    return render_auth_page('signup')

def render_auth_page(mode, error=None):
    err_html = '<div class="err">{}</div>'.format(error) if error else ''
    if mode == 'signup':
        form = '''
        <form method="POST">
        <input type="text" name="username" placeholder="Choose a username" autocomplete="username" autofocus>
        <input type="password" name="password" placeholder="Password" autocomplete="new-password">
        <input type="password" name="confirm" placeholder="Confirm password" autocomplete="new-password">
        <button type="submit">CREATE ACCOUNT</button>
        </form>
        <div class="link">Already have an account? <a href="/login">Log in</a></div>
        '''
        subtitle = 'Create your account'
    else:
        form = '''
        <form method="POST">
        <input type="text" name="username" placeholder="Username" autocomplete="username" autofocus>
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
    user = db.execute('SELECT username, hide_welcome FROM users WHERE id = ?',
                      (session['user_id'],)).fetchone()
    if not user:
        return jsonify({'error': 'User not found'}), 404
    return jsonify({
        'username': user['username'],
        'hide_welcome': bool(user['hide_welcome']),
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

# Register routes
from fishing_intel import register_routes as register_fishing_routes
from captain_advisor import register_advisor_routes

register_fishing_routes(app, login_required)
register_advisor_routes(app, login_required)

logger.info('Wheelhouse server initialized')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8090, debug=True)
