"""
Wheelhouse Advisor — AI Fishing Intelligence
Uses Claude API + live NOAA/NWS data to generate fishing game plans.
"""

import os
import json
import logging
import requests
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from fishing_intel import get_briefing
from dotenv import load_dotenv

load_dotenv('/opt/rednun/.env', override=False)

logger = logging.getLogger('wheelhouse')

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_URL = 'https://api.anthropic.com/v1/messages'
MODEL = 'claude-sonnet-4-20250514'

SYSTEM_PROMPT = """You are Wheelhouse — an expert AI fishing consultant for a charter boat captain operating out of Chatham, Massachusetts on Cape Cod. You have been given real-time oceanographic data and deep local knowledge.

## YOUR ROLE
You analyze live tide, current, weather, SST, buoy, and chlorophyll data to give specific, actionable fishing recommendations. You are talking to Mike, an experienced commercial charter captain who knows these waters well. Don't explain basics — give him the edge.

## LOCAL KNOWLEDGE — MONOMOY SHOALS

**Geography**: South of Chatham lies Monomoy Island, a barrier between warm Nantucket Sound (west) and cold Atlantic/Labrador Current water (east). Where they meet: Bearse Shoals, Stonehorse Shoals, Handkerchief Shoals. The shoals span ~2x6nm.

**Key Spots** (from Ryder's Cove heading south):
- **High Bank Rip**: 1.5mi N of Pollock Rip Channel #8 nun. 12ft MLW. Good starter spot, less pressure.
- **Bearse Shoals Main Rip**: 200yds N of #8 buoy. Wreck with eddies, mixed sand-to-cobble bottom, 2-10ft. Coords ~41°33'00"N / 069°58'48"W (shifts yearly).
- **Stepping Stone Bowls**: 3/4mi NE of Bearse main rip. Series of small connected rips with deeper runnels. Best on incoming tide. Great bail-out from crowds.
- **Bearse #6 Nun Wreck**: Near #6 nun. Wreck with masts, boiling water at circular east end. Drops off abruptly to Pollock Rip Channel. Holds large fish.
- **Stonehorse Shoal**: Starts 3/4mi SSE of Monomoy tip at ~41°31'41"N / 069°59'40"W. Deepest rip at ~16ft near Point. Basin of 30-40ft east of the "9" can ledge holds 30lb+ fish on bait drifts. Southern tail has stacking rips with less boat traffic.
- **Handkerchief Shoal**: Extends 5mi SW from Monomoy Point. 2-18ft, uneven and shifting. SE edge rises abruptly from deep water — that transition concentrates bait on outgoing tide.
- **Pollock Rip Channel / Butler Hole**: Cross-currents create seams. Fish the uphill side on flood. Most boats transit, don't fish it.
- **Orion Shoal**: S/SE of Stonehorse, 16-19ft. Low pressure, holds cruising fish.
- **Morris Island / Stage Harbor**: Sand bars, drop-offs, sand eels. Oyster River mouth into Stage Harbor productive. Sight-casting possible.
- **Chatham Harbor / Pleasant Bay**: Channel edges, flats. Good early/late season.
- **Nauset Beach (ocean side)**: Diamond jigs for bass in deep water. Cold water side.

**Chatham Cuts (North & South)**:
- The North and South Cuts are shifting sand channels between the mainland and Monomoy Island. They are the primary transit routes from Chatham Harbor / Ryder's Cove to the open ocean and fishing grounds.
- NOT fishing spots — they are navigation channels. But conditions through them directly affect trip safety and timing.
- Chatham Roads current data tells you flow through the cuts. Flood pushes water IN (from ocean to harbor), ebb pulls OUT.
- DANGEROUS when: strong ebb current opposes incoming ocean swell or strong SW/S wind. Creates breaking bars and standing waves.
- The North Cut is generally wider but shallower. The South Cut is narrower but can be deeper. Both shift constantly.
- ALWAYS include a cut transit assessment when the captain is departing from Ryder's Cove, Chatham Harbor, or any inside launch. Tell him which cut looks better based on current direction, wind, and swell, and what time window is safest for transit.
- If conditions are marginal, say so clearly. These cuts kill boats.

**Wave & Transit Safety**:
- Buoy data includes wave height (WVHT), dominant wave period (DPD), and mean wave direction (MWD). USE THIS DATA.
- When the captain says where he is and where he's headed, calculate his approximate heading and compare it to wave direction.
- Head seas (waves coming at the bow): rough but manageable. Following seas (waves from behind): can be dangerous with steep short-period waves. Beam seas (waves from the side): worst — risk of roll.
- Key transit routes and approximate headings:
  - Ryder's Cove → South Cut → Pollock Rip: heading ~180° (south)
  - Pollock Rip → Ryder's Cove via cuts: heading ~0° (north)
  - Ryder's Cove → Nauset: heading ~350° (north-northwest)
  - Ryder's Cove → South of Nantucket: heading ~200° (south-southwest)
  - Chatham Harbor → Monomoy: heading ~170° (south)
- Short period waves (<6s) with height >3ft are much worse than long period swells
- SW wind + ebb current = dangerous standing waves on the shoals and in the cuts
- If wave direction opposes the captain's return heading, WARN HIM about the ride home

**Currents**:
- Pollock Rip: Flood flows east (~170°), ebb flows west (~346°). 2-2.5kt average, 3-5kt at edges.
- Monomoy tip: Incoming/flood flows EAST, outgoing/ebb flows WEST.
- Best fishing: moving water. Action slows at slack. Either tide direction can produce.
- Strong tides + afternoon SW winds = dangerous standing waves on the shoals.

**Bait patterns**:
- Sand eels: primary forage, abundant at Morris Island and throughout shoals
- Squid: key bait in rips June-July. Pink/amber lures match squid.
- Herring: spring run. Silver lures.
- Mackerel: early summer. Use side-scan to locate schools.
- Pogies (menhaden): when present, live-lining is devastating.

**Techniques by situation**:
- Rips standing up + visible birds: Cast topwater plugs (bone white, pink, amber) into rips. Swing through the rip.
- Rips standing up, no surface: Bottom bounce with 2oz epoxy jigs. Match bait color.
- Fish marked on sonar but not showing: Pull wire with big bucktail jigs imitating squid.
- Deep basin fish: Long bait drifts with live eels or cut bait.
- Calm/clear water: Light tackle soft plastics (Hogy Protail), cast into bird activity.
- Fog (common): Stay tight to known rip coordinates, use radar. Fish often feed aggressively in fog.

**Seasonal timing**:
- Late May: first fish arrive, worm hatch
- June: herring run, fish filling in, sight fishing on flats
- Late June-July: peak action, squid in the rips, massive schools. Topwater best.
- August: peak surf fishing, also sharks/tuna offshore. Bonito and albies arrive.
- September: fall run begins, bass return to harbors and shoals
- October: last push, can be excellent

**Water temp**: Stripers prefer 55-68°F. The Labrador Current side (east of Monomoy) drops 15-20°F vs Sound side. Bait concentrates where the temp break is sharpest. SST satellite data shows this daily.

**Launch**: Ryder's Cove, N. Chatham. Commercial season permits required (~late June-late Sept). $20/day ramp permit, max 40 issued daily. Parking limited.

## HOW TO RESPOND

You are a conversational AI assistant. Talk to Mike like a knowledgeable fishing buddy — not a report generator. Be natural. Read what he's asking and respond appropriately.

**When he asks for a fishing plan** (e.g. "Pollock Rip tomorrow at 9am"):
1. If departing from inside, start with cut transit assessment
2. Give the fishing plan — where to go, what to throw, tide/current timing, when to move
3. **Include a brief ride summary** as part of the plan — 1-2 sentences about what the run out there will be like. Example: "Run to Pollock is about 4nm heading 180°. Waves are SW at 2.5ft/8s — you'll have them on the quarter, shouldn't be bad. Wind is with you."
4. At the end, **ask**: "Want me to do a full ride analysis for the trip out and back?" — let him decide if he wants the deep dive with compass headings, wave angles, and timing recommendations.

**When he asks about ride/transit conditions** (e.g. "what's the ride going to be like?" or "how's the trip back?"):
Give a DETAILED ride analysis. Don't just say "it'll be rough." Explain WHY:
- "You'll be heading 350° back to Ryder's. Waves are currently out of the SW at 220° — that puts them on your port beam at 3.2ft with a 7-second period."
- "That's a beam sea situation — you'll be rolling. It'll be uncomfortable but manageable at that period."
- "The ebb current is running against the wind right now which is stacking the waves up shorter and steeper."
- "If you wait until slack at 2:15 PM the seas will lay down significantly."
Use compass headings, wave heights, periods, and directions. Be specific about what angle the waves hit the boat relative to the heading. Explain head seas, following seas, beam seas in plain terms.

**When he's ON the water** (GPS coordinates in the message):
- Know where he is relative to his plan
- If he says "should I stay or move?" — analyze current conditions at his position vs other spots
- If he mentions heading home, give the ride analysis without being asked
- Track time of day against tide/current predictions — "current flips in 40 minutes, you might want to make your move now"

**General conversation style**:
- Be direct, specific, brief. No fluff. He knows the water.
- Mix fishing and conditions advice naturally — if a spot recommendation involves a rough run, say so
- Ask follow-up questions when it helps: "What time you thinking of heading back?" "You on the east side or west side of the shoal?"
- Use the data. Every response should reference actual numbers from the live data — don't generalize.
- If conditions are dangerous, say it plainly: "I wouldn't run the South Cut right now."

Messages may include [Current GPS: lat, lon] — this is his live position from his phone. Use it to give location-aware advice: how far he is from suggested spots, whether he should keep fishing where he is or move, and what the conditions are at his current position relative to tide/current timing.

## IMPORTANT
- Shoals SHIFT every year. Published chart depths are unreliable. Always trust the sounder.
- The 3-mile limit applies to stripers — some shoals outside it are not legal for targeting stripers.
- Grey seals are everywhere and have hurt the surf fishing, but boat fishing is better than ever.
- Fog is extremely common at Monomoy due to the temperature differential. Always note fog risk.
"""


def get_live_data_context():
    """Gather all live data and format it for the AI prompt."""
    try:
        briefing = get_briefing()
    except Exception as e:
        logger.error(f'Failed to get briefing: {e}')
        return "LIVE DATA UNAVAILABLE — analyze based on date/time and general knowledge."

    ctx = []
    ctx.append(f"=== LIVE DATA — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    # Tides
    for key, tdata in briefing.get('tides', {}).items():
        if tdata and tdata.get('predictions'):
            ctx.append(f"TIDES — {tdata['station']['name']}:")
            for p in tdata['predictions'][:10]:
                hilo = "HIGH" if p['type'] == 'H' else "LOW"
                ctx.append(f"  {p['t']} — {p['v']}ft {hilo}")
            ctx.append("")

    # Currents
    for key, cdata in briefing.get('currents', {}).items():
        if cdata and cdata.get('predictions'):
            ctx.append(f"CURRENTS — {cdata['station']['name']}:")
            for p in cdata['predictions'][:12]:
                ctype = p.get('Type', 'unknown')
                vel = p.get('Velocity_Major', '0')
                ctx.append(f"  {p['Time']} — {ctype} {vel}kt")
            ctx.append("")

    # Weather
    weather = briefing.get('weather')
    if weather:
        if weather.get('hourly'):
            ctx.append("WEATHER (Hourly):")
            for h in weather['hourly'][:12]:
                t = h.get('startTime', '')[:16]
                temp = h.get('temperature', '')
                wind = h.get('windSpeed', '')
                wdir = h.get('windDirection', '')
                short = h.get('shortForecast', '')
                ctx.append(f"  {t} — {temp}°F, {wdir} {wind}, {short}")
            ctx.append("")
        if weather.get('forecast'):
            ctx.append("FORECAST:")
            for f in weather['forecast'][:4]:
                ctx.append(f"  {f['name']}: {f.get('detailedForecast', f.get('shortForecast', ''))}")
            ctx.append("")

    # Buoy
    buoy = briefing.get('buoy')
    if buoy and buoy.get('observation'):
        obs = buoy['observation']
        wtmp = obs.get('WTMP')
        if wtmp and wtmp != 'MM':
            water_f = round(float(wtmp) * 9/5 + 32, 1)
        else:
            water_f = 'N/A'
        wvht = obs.get('WVHT', 'N/A')
        wspd = obs.get('WSPD', 'N/A')
        wdir = obs.get('WDIR', 'N/A')
        gst = obs.get('GST', 'N/A')
        dpd = obs.get('DPD', 'N/A')
        mwd = obs.get('MWD', 'N/A')
        ctx.append(f"BUOY 44018 (SE Cape Cod):")
        ctx.append(f"  Water temp: {water_f}°F")
        ctx.append(f"  Waves: {wvht}ft @ {dpd}s, direction from {mwd}°")
        ctx.append(f"  Wind: {wdir}° at {wspd}m/s, gusts {gst}m/s")
        ctx.append("")

    return "\n".join(ctx)


def ask_advisor(messages, user_message):
    """
    Send a message to the Captain's Advisor.
    messages: list of prior conversation messages [{"role": "user"/"assistant", "content": "..."}]
    user_message: the new user message
    Returns: assistant response text
    """
    if not ANTHROPIC_API_KEY:
        return "⚠ ANTHROPIC_API_KEY not set in .env file. Add it and restart: systemctl restart rednun"

    # Get live data
    live_data = get_live_data_context()

    # Build the system prompt with live data
    full_system = SYSTEM_PROMPT + "\n\n" + live_data

    # Build messages array
    api_messages = []
    for m in messages:
        api_messages.append({"role": m["role"], "content": m["content"]})
    api_messages.append({"role": "user", "content": user_message})

    try:
        r = requests.post(
            ANTHROPIC_URL,
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
            },
            json={
                'model': MODEL,
                'max_tokens': 2000,
                'system': full_system,
                'messages': api_messages,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        # Extract text from response
        text = ''
        for block in data.get('content', []):
            if block.get('type') == 'text':
                text += block['text']
        return text or '⚠ Empty response from advisor.'

    except requests.exceptions.Timeout:
        return '⚠ Advisor timed out. Try again.'
    except requests.exceptions.HTTPError as e:
        logger.error(f'Advisor API error: {e.response.status_code} {e.response.text[:200]}')
        if e.response.status_code == 401:
            return '⚠ Invalid API key. Check ANTHROPIC_API_KEY in your .env file.'
        return f'⚠ API error: {e.response.status_code}'
    except Exception as e:
        logger.error(f'Advisor error: {e}')
        return f'⚠ Error: {str(e)}'


# ==================== FLASK ROUTES ====================

def register_advisor_routes(app, login_required):
    """Register advisor chat routes with the Flask app."""
    from flask import jsonify, request

    @app.route('/api/fishing/advisor', methods=['POST'])
    @login_required
    def api_fishing_advisor():
        data = request.get_json()
        if not data or 'message' not in data:
            return jsonify({'error': 'No message provided'}), 400

        messages = data.get('history', [])
        user_msg = data['message']

        response = ask_advisor(messages, user_msg)
        return jsonify({
            'response': response,
            'timestamp': datetime.now().isoformat(),
        })

    # ---- Cut Conditions Analysis ----
    @app.route('/api/fishing/cuts', methods=['GET'])
    @login_required
    def api_cuts_analysis():
        """Quick AI analysis of conditions at the Chatham cuts."""
        if not ANTHROPIC_API_KEY:
            return jsonify({'error': 'API key not configured'}), 500

        live_data = get_live_data_context()

        cut_prompt = """Based on the current conditions below, give a brief safety and navigation analysis for the Chatham North and South Cuts. Keep it under 200 words. Be direct — the captain knows these waters.

Include:
- Which cut is better right now and why
- Current flow direction and strength through each
- Any safety concerns (opposing wind/current, breaking bars, etc.)
- **BEST TRANSIT WINDOWS**: Give specific times today for safest outbound (harbor to ocean) and inbound (ocean to harbor) transit through each cut. Base this on when current and wind are most favorable (ideally slack or flood for outbound, slack or ebb for inbound, and not opposing wind).
- A one-line recommendation

Current conditions:
""" + live_data

        try:
            r = requests.post(
                ANTHROPIC_URL,
                headers={
                    'Content-Type': 'application/json',
                    'x-api-key': ANTHROPIC_API_KEY,
                    'anthropic-version': '2023-06-01',
                },
                json={
                    'model': MODEL,
                    'max_tokens': 400,
                    'system': 'You are a concise navigation safety advisor for Chatham, MA. The North and South Cuts are shifting sand channels between the mainland and Monomoy Island. They can be very dangerous with opposing wind and current. Be direct and safety-focused.',
                    'messages': [{'role': 'user', 'content': cut_prompt}],
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            text = ''
            for block in data.get('content', []):
                if block.get('type') == 'text':
                    text += block['text']
            return jsonify({'analysis': text})
        except Exception as e:
            logger.error(f'Cuts analysis error: {e}')
            return jsonify({'error': str(e)}), 500

    # ---- Voice Catch Parsing ----
    @app.route('/api/fishing/parse-catch', methods=['POST'])
    @login_required
    def api_parse_catch():
        """Use AI to parse a voice transcript into catch log fields."""
        data = request.get_json()
        transcript = data.get('transcript', '').strip() if data else ''
        if not transcript:
            return jsonify({'error': 'No transcript'}), 400
        if not ANTHROPIC_API_KEY:
            return jsonify({'error': 'API key not configured'}), 500

        parse_prompt = f"""Parse this fishing catch report into structured fields. Extract whatever you can. If a field isn't mentioned, leave it as empty string.

Transcript: "{transcript}"

Respond with ONLY valid JSON, no markdown, no explanation:
{{
  "spot": "",
  "species": "",
  "technique": "",
  "lure": "",
  "notes": ""
}}

Rules:
- spot: The fishing location mentioned. Use it exactly as spoken — don't normalize to a known list.
- species: Match to one of: Striped Bass, Bluefish, False Albacore, Bonito, Fluke, Black Sea Bass, Tuna. "Striper" = Striped Bass, "blues" = Bluefish, "albies" = False Albacore. If no match, use what they said.
- technique: Match to one of: Casting, Trolling, Jigging, Live Bait, Fly, Drifting. If no match, use what they said.
- lure: Match to one of: White Bucktail, Slug-Go, Live Eel, Chunk, Diamond Jig, Popper, SP Minnow. If no match, use what they said.
- notes: Everything else — size, weight, color, conditions, anything descriptive. Include the full transcript here as well."""

        try:
            r = requests.post(
                ANTHROPIC_URL,
                headers={
                    'Content-Type': 'application/json',
                    'x-api-key': ANTHROPIC_API_KEY,
                    'anthropic-version': '2023-06-01',
                },
                json={
                    'model': MODEL,
                    'max_tokens': 300,
                    'messages': [{'role': 'user', 'content': parse_prompt}],
                },
                timeout=15,
            )
            r.raise_for_status()
            resp_data = r.json()
            text = ''
            for block in resp_data.get('content', []):
                if block.get('type') == 'text':
                    text += block['text']
            parsed = json.loads(text.strip())
            return jsonify(parsed)
        except Exception as e:
            logger.error(f'Catch parse error: {e}')
            # Fallback: just put everything in notes
            return jsonify({
                'spot': '', 'species': '', 'technique': '',
                'lure': '', 'notes': transcript
            })

    # ---- Save / List / View Logs ----
    import glob as globmod

    LOGS_DIR = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(LOGS_DIR, exist_ok=True)

    @app.route('/api/fishing/advisor/save', methods=['POST'])
    @login_required
    def api_advisor_save():
        data = request.get_json()
        history = data.get('history', [])
        if not history:
            return jsonify({'error': 'No conversation to save'}), 400

        ts = datetime.now().strftime('%Y-%m-%d_%H%M')
        # Build text log
        lines = [f"WHEELHOUSE ADVISOR LOG — {datetime.now().strftime('%B %d, %Y %I:%M %p')}", "="*60, ""]
        for msg in history:
            role = "CAPTAIN" if msg['role'] == 'user' else "WHEELHOUSE"
            lines.append(f"[{role}]")
            lines.append(msg['content'])
            lines.append("")

        filename = f"advisor_{ts}.txt"
        filepath = os.path.join(LOGS_DIR, filename)
        with open(filepath, 'w') as f:
            f.write('\n'.join(lines))

        logger.info(f'Advisor log saved: {filename}')
        return jsonify({'filename': filename, 'saved': True})

    @app.route('/api/fishing/advisor/logs')
    @login_required
    def api_advisor_logs():
        files = sorted(globmod.glob(os.path.join(LOGS_DIR, 'advisor_*.txt')), reverse=True)
        logs = []
        for fp in files[:20]:
            fname = os.path.basename(fp)
            # Parse date from filename: advisor_2026-04-04_1430.txt
            try:
                date_part = fname.replace('advisor_', '').replace('.txt', '')
                dt = datetime.strptime(date_part, '%Y-%m-%d_%H%M')
                date_str = dt.strftime('%b %d, %Y %I:%M %p')
            except:
                date_str = fname
            # Get preview (first user message)
            preview = ''
            try:
                with open(fp, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('[') and not line.startswith('WHEELHOUSE') and not line.startswith('='):
                            preview = line[:60]
                            if len(line) > 60:
                                preview += '...'
                            break
            except:
                pass
            logs.append({'filename': fname, 'date': date_str, 'preview': preview})
        return jsonify({'logs': logs})

    @app.route('/api/fishing/advisor/logs/<filename>')
    @login_required
    def api_advisor_log_view(filename):
        # Sanitize filename
        if '..' in filename or '/' in filename:
            return jsonify({'error': 'Invalid filename'}), 400
        filepath = os.path.join(LOGS_DIR, filename)
        if not os.path.exists(filepath):
            return jsonify({'error': 'Log not found'}), 404
        from flask import send_file
        return send_file(filepath, mimetype='text/plain')

    # ---- Catch Log Routes ----

    def _snapshot_conditions():
        """Snapshot current conditions for catch log."""
        try:
            briefing = get_briefing()
        except Exception as e:
            logger.error(f'Catch log briefing error: {e}')
            return {}

        conditions = {}

        # Water temp from buoy
        buoy = briefing.get('buoy')
        if buoy and buoy.get('observation'):
            obs = buoy['observation']
            wtmp = obs.get('WTMP')
            if wtmp and wtmp != 'MM':
                conditions['water_temp'] = f"{round(float(wtmp) * 9/5 + 32, 1)}°F"
            wspd = obs.get('WSPD')
            wdir = obs.get('WDIR', '')
            gst = obs.get('GST')
            if wspd:
                wind_kt = round(float(wspd) * 1.944)
                wind_str = f"{wdir}° at {wind_kt}kt"
                if gst and gst != 'MM':
                    wind_str += f" gusting {round(float(gst) * 1.944)}kt"
                conditions['wind'] = wind_str
            wvht = obs.get('WVHT')
            if wvht and wvht != 'MM':
                conditions['wave_height'] = f"{float(wvht):.1f}ft"

        # Tide phase — find nearest hi/lo
        tides = briefing.get('tides', {}).get('chatham')
        if tides and tides.get('predictions'):
            now = datetime.now()
            nearest = None
            for p in tides['predictions']:
                try:
                    t = datetime.strptime(p['t'], '%Y-%m-%d %H:%M')
                    diff = (t - now).total_seconds() / 3600
                    hilo = 'high' if p['type'] == 'H' else 'low'
                    if nearest is None or abs(diff) < abs(nearest[0]):
                        nearest = (diff, hilo, p['v'])
                except:
                    pass
            if nearest:
                diff_hr, hilo, val = nearest
                if abs(diff_hr) < 0.5:
                    conditions['tide_phase'] = f"At {hilo} tide ({val}ft)"
                elif diff_hr > 0:
                    conditions['tide_phase'] = f"{abs(diff_hr):.1f}hrs before {hilo} ({val}ft)"
                else:
                    conditions['tide_phase'] = f"{abs(diff_hr):.1f}hrs after {hilo} ({val}ft)"

        # Current
        currents = briefing.get('currents', {}).get('pollock_rip')
        if currents and currents.get('predictions'):
            now = datetime.now()
            nearest = None
            for p in currents['predictions']:
                try:
                    t = datetime.strptime(p['Time'], '%Y-%m-%d %H:%M')
                    diff = (t - now).total_seconds() / 3600
                    if nearest is None or abs(diff) < abs(nearest[0]):
                        nearest = (diff, p.get('Type', ''), p.get('Velocity_Major', ''))
                except:
                    pass
            if nearest:
                _, ctype, vel = nearest
                conditions['current'] = f"{ctype} {vel}kt at Pollock Rip"

        # Weather
        weather = briefing.get('weather')
        if weather and weather.get('hourly'):
            h = weather['hourly'][0]
            conditions['weather'] = f"{h.get('shortForecast', '')}, {h.get('temperature', '')}°F, {h.get('windDirection', '')} {h.get('windSpeed', '')}"

        return conditions

    @app.route('/api/fishing/log', methods=['POST'])
    @login_required
    def api_catch_log_save():
        from flask import session as flask_session
        data = request.get_json()
        if not data or not data.get('spot'):
            return jsonify({'error': 'Spot is required'}), 400

        conditions = _snapshot_conditions()

        gps = data.get('gps')
        entry = {
            'timestamp': datetime.now().isoformat(),
            'logged_by': flask_session.get('username', 'unknown'),
            'spot': data.get('spot', ''),
            'gps': gps,
            'species': data.get('species', ''),
            'technique': data.get('technique', ''),
            'lure': data.get('lure', ''),
            'notes': data.get('notes', ''),
            'conditions': conditions,
        }

        ts = datetime.now().strftime('%Y-%m-%d_%H%M')
        filename = f"catch_{ts}.json"
        filepath = os.path.join(LOGS_DIR, filename)
        with open(filepath, 'w') as f:
            json.dump(entry, f, indent=2)

        logger.info(f'Catch logged: {data.get("spot")} — {filename}')

        # Email notification
        try:
            gmail_user = os.environ.get('GMAIL_ADDRESS', '')
            gmail_pass = os.environ.get('GMAIL_APP_PASSWORD', '')
            if gmail_user and gmail_pass:
                username = flask_session.get('username', 'unknown')
                gps_str = ''
                if gps:
                    gps_str = f"\nGPS: {gps['lat']:.5f}°N, {abs(gps['lon']):.5f}°W"
                cond_lines = []
                for k, v in conditions.items():
                    cond_lines.append(f"  {k}: {v}")
                body = (
                    f"Catch logged on Wheelhouse!\n\n"
                    f"Captain: {username}\n"
                    f"Spot: {entry['spot']}{gps_str}\n"
                    f"Species: {entry['species']}\n"
                    f"Technique: {entry['technique']}\n"
                    f"Lure: {entry['lure']}\n"
                    f"Notes: {entry['notes']}\n\n"
                    f"CONDITIONS:\n" + '\n'.join(cond_lines) + '\n\n'
                    f"https://wheelhouse.rednun.com"
                )
                msg = MIMEText(body, 'plain')
                msg['Subject'] = f"🐟 Wheelhouse Catch — {entry['spot']} ({username})"
                msg['From'] = gmail_user
                msg['To'] = 'mgiorgio@rednun.com'
                with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10) as smtp:
                    smtp.login(gmail_user, gmail_pass)
                    smtp.send_message(msg)
        except Exception as e:
            logger.error(f'Catch notification email failed: {e}')

        return jsonify({'filename': filename, 'saved': True})

    @app.route('/api/fishing/logs')
    @login_required
    def api_catch_logs():
        files = sorted(globmod.glob(os.path.join(LOGS_DIR, 'catch_*.json')), reverse=True)
        logs = []
        for fp in files[:50]:
            try:
                with open(fp, 'r') as f:
                    entry = json.load(f)
                dt = datetime.fromisoformat(entry['timestamp'])
                logs.append({
                    'filename': os.path.basename(fp),
                    'date': dt.strftime('%b %d, %Y %I:%M %p'),
                    'logged_by': entry.get('logged_by', ''),
                    'spot': entry.get('spot', ''),
                    'gps': entry.get('gps'),
                    'species': entry.get('species', ''),
                    'technique': entry.get('technique', ''),
                    'lure': entry.get('lure', ''),
                    'notes': entry.get('notes', ''),
                    'conditions': entry.get('conditions', {}),
                })
            except Exception as e:
                logger.error(f'Error reading catch log {fp}: {e}')
        return jsonify({'logs': logs})

    @app.route('/api/fishing/log/<filename>', methods=['DELETE'])
    @login_required
    def api_catch_delete(filename):
        # Only allow deleting catch_*.json files
        if not filename.startswith('catch_') or not filename.endswith('.json'):
            return jsonify({'error': 'Invalid filename'}), 400
        filepath = os.path.join(LOGS_DIR, filename)
        if not os.path.exists(filepath):
            return jsonify({'error': 'Not found'}), 404
        os.remove(filepath)
        logger.info(f'Catch log deleted: {filename}')
        return jsonify({'deleted': True})

    logger.info('Wheelhouse Advisor routes registered')
