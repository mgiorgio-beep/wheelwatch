# WHEELHOUSE — Claude Code Deployment Brief
# Standalone app on Beelink server at wheelhouse.rednun.com

## OBJECTIVE
Deploy "Wheelhouse" as a standalone Flask app on the Beelink server at the Chatham restaurant. Expose it publicly at **wheelhouse.rednun.com** via an existing Cloudflare tunnel. This is a completely separate application from the Red Nun Dashboard (which runs on DigitalOcean).

## WHAT WHEELHOUSE DOES
A fishing intelligence dashboard + AI advisor for a charter boat captain out of Chatham, MA. Pulls live NOAA tide/current data, NWS weather, NDBC buoy data, NASA satellite imagery, then lets the captain chat with an AI advisor that analyzes all the live data and gives specific fishing game plans for Monomoy Shoals.

## SERVER — BEELINK SER5
- **SSH**: `ssh -p 2222 rednun@ssh.rednun.com`
- **Local IP**: `10.1.10.83`
- **OS**: Ubuntu Linux
- **Location**: Chatham restaurant, on local network
- **External access**: Cloudflare tunnel (already configured for other services)
- **Existing services**: There may be other apps running on this box — check before using port 8080. Use port **8090** for Wheelhouse to avoid conflicts.

## FILES PROVIDED
You have 3 application files in the current working directory:

1. **`fishing_intel.py`** — Data feeds module
   - NOAA tides/currents API proxy (stations: 8447435 Chatham, 8447270 Stage Harbor, ACT1616 Pollock Rip, ACT1611 Chatham Roads)
   - NWS weather/forecast for Chatham coordinates (41.6723, -69.9597)
   - NDBC buoy 44018 (SE Cape Cod) real-time observations
   - NASA GIBS WMS satellite image proxy (true color VIIRS/MODIS, chlorophyll, SST)
   - SST, chlorophyll, visual satellite source link collections
   - All data cached in memory with TTL (15min-1hr)
   - Has `register_routes(app, login_required)` to register all API endpoints
   - **NOTE**: Has a `/fishing` route that serves fishing.html — this is fine, the server.py `/` route will be the primary entry point

2. **`captain_advisor.py`** — AI advisor module
   - Claude API integration using claude-sonnet-4-20250514
   - Massive system prompt with deep Monomoy Shoals local knowledge
   - `get_live_data_context()` gathers all NOAA/NWS/NDBC data and formats as text
   - `ask_advisor(messages, user_message)` sends conversation + live data to Claude API
   - Requires `ANTHROPIC_API_KEY` environment variable
   - Has `register_advisor_routes(app, login_required)` to register POST `/api/fishing/advisor`

3. **`fishing.html`** — Frontend (single-page dashboard)
   - Dark theme, monospace font, mobile-first
   - Wheelhouse Advisor chat box at top
   - Live data cards: buoy, tides (2 stations), currents (2 stations), hourly weather, 48hr tide curve chart, extended forecast
   - NASA GIBS satellite image viewer with layer/zoom switchers
   - SST, visual satellite, chlorophyll source links
   - Auto-refreshes every 15 minutes
   - **IMPORTANT**: Remove the line `<script src="/static/sidebar.js"></script>` — that's from the Red Nun dashboard and doesn't exist here

## DEPLOYMENT STEPS

### Step 1: Create app directory and venv
```bash
sudo mkdir -p /opt/wheelhouse/static
sudo chown -R rednun:rednun /opt/wheelhouse
cd /opt/wheelhouse
python3 -m venv venv
source venv/bin/activate
pip install flask gunicorn requests python-dotenv
```

### Step 2: Copy application files
```bash
cp fishing_intel.py /opt/wheelhouse/
cp captain_advisor.py /opt/wheelhouse/
cp fishing.html /opt/wheelhouse/static/
```

### Step 3: Remove sidebar.js reference from fishing.html
```bash
sed -i '/<script src="\/static\/sidebar.js"><\/script>/d' /opt/wheelhouse/static/fishing.html
```

### Step 4: Create server.py
Create `/opt/wheelhouse/server.py` — a standalone Flask app with simple password auth:

```python
import os
import logging
from datetime import timedelta
from flask import Flask, send_from_directory, session, request, redirect, jsonify
from functools import wraps
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('wheelhouse')

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24).hex())
app.permanent_session_lifetime = timedelta(days=30)

WHEELHOUSE_PASSWORD = os.environ.get('WHEELHOUSE_PASSWORD', 'wheelhouse')

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Not authenticated'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == WHEELHOUSE_PASSWORD:
            session.permanent = True
            session['authenticated'] = True
            return redirect('/')
        return send_login_page(error='Wrong password')
    return send_login_page()

def send_login_page(error=None):
    err_html = f'<p style="color:#ff4444;margin-bottom:12px;">{error}</p>' if error else ''
    html = '''<!DOCTYPE html>
<html><head><title>Wheelhouse</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body { background:#0a0f1a; color:#e0e8f0; font-family:'SF Mono',Consolas,monospace;
       display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }
.box { background:#0d1520; border:1px solid #1a2535; border-radius:8px; padding:30px; width:300px; text-align:center; }
h1 { font-size:22px; letter-spacing:3px; margin-bottom:20px; }
h1 span { color:#00d4ff; }
input { width:100%%; padding:12px; background:#080d16; border:1px solid #1a2535; border-radius:4px;
         color:#e0e8f0; font-family:inherit; font-size:14px; margin-bottom:12px; box-sizing:border-box; outline:none; }
input:focus { border-color:#00d4ff40; }
button { width:100%%; padding:12px; background:#00d4ff15; border:1px solid #00d4ff40; color:#00d4ff;
          border-radius:4px; cursor:pointer; font-family:inherit; font-size:13px; font-weight:700;
          letter-spacing:1px; }
button:hover { background:#00d4ff25; }
</style></head><body>
<div class="box">
<h1>⚓ WHEEL<span>HOUSE</span></h1>
''' + err_html + '''
<form method="POST">
<input type="password" name="password" placeholder="Password" autofocus>
<button type="submit">ENTER</button>
</form>
</div></body></html>'''
    return html

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/')
@login_required
def index():
    return send_from_directory('static', 'fishing.html')

# Register routes
from fishing_intel import register_routes as register_fishing_routes
from captain_advisor import register_advisor_routes

register_fishing_routes(app, login_required)
register_advisor_routes(app, login_required)

logger.info('Wheelhouse server initialized')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8090, debug=True)
```

### Step 5: Create .env
```bash
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
cat > /opt/wheelhouse/.env << EOF
ANTHROPIC_API_KEY=USERS_KEY_HERE
WHEELHOUSE_PASSWORD=wheelhouse
SECRET_KEY=$SECRET_KEY
EOF
```
**Ask the user for their Anthropic API key** and preferred login password.

### Step 6: Test locally before setting up the service
```bash
cd /opt/wheelhouse
source venv/bin/activate
python -c "import fishing_intel; print('fishing_intel OK')"
python -c "import captain_advisor; print('captain_advisor OK')"
python -c "from server import app; print('server OK')"

# Quick manual test
python server.py &
sleep 2
curl -s http://127.0.0.1:8090/login | head -5
kill %1
```

### Step 7: Create systemd service
```bash
sudo tee /etc/systemd/system/wheelhouse.service << 'EOF'
[Unit]
Description=Wheelhouse Fishing Intel
After=network.target

[Service]
User=rednun
WorkingDirectory=/opt/wheelhouse
Environment=PATH=/opt/wheelhouse/venv/bin:/usr/bin
EnvironmentFile=/opt/wheelhouse/.env
ExecStart=/opt/wheelhouse/venv/bin/gunicorn server:app -b 127.0.0.1:8090 -w 2 --timeout 60
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable wheelhouse
sudo systemctl start wheelhouse
sleep 3
sudo systemctl status wheelhouse --no-pager | head -15
sudo journalctl -u wheelhouse --no-pager -n 20
```

### Step 8: Cloudflare tunnel — ADD HOSTNAME
The user knows how to do this. Remind them:

**In Cloudflare Zero Trust dashboard → Access → Tunnels → their tunnel → Public Hostnames → Add:**
- **Subdomain**: `wheelhouse`
- **Domain**: `rednun.com`
- **Service type**: `HTTP`
- **URL**: `localhost:8090`

That's it — Cloudflare handles SSL, no nginx needed on the Beelink. The tunnel proxies `wheelhouse.rednun.com` directly to `localhost:8090`.

**NOTE**: If there IS nginx running on the Beelink in front of other apps, you can either:
- Point the tunnel directly to gunicorn on 8090 (simpler, recommended)
- Or add an nginx server block and point the tunnel to nginx

### Step 9: Verify
```bash
# Local test
curl -s http://127.0.0.1:8090/login | head -5

# Through tunnel (after Cloudflare hostname is added)
curl -s https://wheelhouse.rednun.com/login | head -5
```

Tell the user to visit: **https://wheelhouse.rednun.com**
- Log in with their password
- Type: "Leaving Ryder's Cove tomorrow at 12 PM"

## ARCHITECTURE
```
wheelhouse.rednun.com
        │
   Cloudflare Tunnel
        │
   Beelink SER5 (10.1.10.83)
   gunicorn :8090
        │
   /opt/wheelhouse
   ├── server.py          ← Flask app + login
   ├── fishing_intel.py   ← NOAA/NWS/NDBC/NASA data
   ├── captain_advisor.py ← Claude AI advisor
   ├── static/fishing.html ← Frontend
   └── .env               ← API keys

   (Completely separate from)

dashboard.rednun.com
        │
   DigitalOcean (159.65.180.102)
   /opt/rednun ← existing dashboard, untouched
```

## API ROUTES

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Wheelhouse dashboard |
| `/login` | GET/POST | Password login |
| `/logout` | GET | Clear session |
| `/api/fishing/briefing` | GET | Full data briefing |
| `/api/fishing/tides` | GET | Tide predictions |
| `/api/fishing/tides/hourly` | GET | 6-min tide heights for charting |
| `/api/fishing/currents` | GET | Current predictions (flood/ebb/slack) |
| `/api/fishing/weather` | GET | NWS hourly + extended forecast |
| `/api/fishing/buoy` | GET | NDBC buoy 44018 observations |
| `/api/fishing/sst` | GET | SST source links |
| `/api/fishing/chlorophyll` | GET | Chlorophyll source links |
| `/api/fishing/visual` | GET | Visual satellite source links |
| `/api/fishing/satellite` | GET | NASA GIBS satellite image (base64) |
| `/api/fishing/satellite/layers` | GET | Available satellite layers |
| `/api/fishing/advisor` | POST | Wheelhouse AI advisor chat |

## TROUBLESHOOTING
- **502 from Cloudflare**: gunicorn not running. `sudo systemctl status wheelhouse`
- **"fishing_intel not loaded"**: File must be in `/opt/wheelhouse/` not `static/`
- **"ANTHROPIC_API_KEY not set"**: Check `/opt/wheelhouse/.env`
- **Advisor returns 401 from Claude**: API key invalid. Verify at console.anthropic.com
- **Satellite images blank**: Today's pass not processed yet (~3hr delay). Auto-falls back to yesterday.
- **Can't reach wheelhouse.rednun.com**: Check Cloudflare tunnel hostname config
- **Permission errors**: Make sure `/opt/wheelhouse` is owned by `rednun` user

## COST
- NOAA/NWS/NDBC/NASA data: **FREE**
- Claude API (advisor): **~$0.01-0.03 per query**
- Server: Beelink already running, no additional cost
- Domain/tunnel: Already configured
