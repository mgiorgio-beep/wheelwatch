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

# ==================== LEGAL PAGES (Twilio A2P compliance) ====================

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
        account by default. If you join a Friend Group and enable catch sharing, spot
        names (not GPS coordinates) are visible to group members. GPS coordinates are
        never shared with other users under any circumstance.</p>

        <h3>Aggregate Analysis</h3>
        <p>Anonymized catch data — species, technique, and spot names with all
        identifying information removed — may be used in aggregate to improve fishing
        pattern predictions for all users. No individual catch data is attributable
        to you in this analysis.</p>

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


# Register routes
from fishing_intel import register_routes as register_fishing_routes
from captain_advisor import register_advisor_routes

register_fishing_routes(app, login_required)
register_advisor_routes(app, login_required)

logger.info('Wheelhouse server initialized')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8090, debug=True)
