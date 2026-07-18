"""
Wheelhouse Photo Catch Log + Social Feed

Routes:
  POST /parse-catch-photo     — Claude Vision identifies species + estimates size
  POST /log-catch-photo       — Save resized JPEG + catch entry JSON
  GET  /api/crew-feed         — Last 7 days of catches across crews (legacy, kept for back-compat)
  GET  /api/feed              — Unified social feed: catches + text/photo posts, merged
  POST /api/post              — Create a text (+optional photo) post with visibility=friends|public
  DELETE /api/post/<id>       — Delete your own post
  GET  /post-photos/<fname>   — Serve post photos (visibility-aware)
  GET  /catch-photos/<fname>  — Serve catch photos (crew-shared)

Catches are stored in the same JSON format as captain_advisor (one file per catch in
LOGS_DIR, filename `catch_<timestamp>.json`). Posts live in the `posts` SQLite table.
The feed never emits raw GPS — only `area_name` derived from coords.
"""

import os
import io
import json
import base64
import logging
import threading
import glob as globmod
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger('wheelhouse')

BASE_DIR = os.path.dirname(__file__)
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
PHOTOS_DIR = os.path.join(BASE_DIR, 'catch_photos')
POST_PHOTOS_DIR = os.path.join(BASE_DIR, 'post_photos')
# Private store for Garmin/instrument photos. These are read for value extraction
# ONLY and must NEVER be served to the feed or inserted into the posts table.
INSTRUMENT_DIR = os.path.join(LOGS_DIR, 'instrument')
DB_PATH = os.path.join(BASE_DIR, 'wheelhouse.db')

os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs(POST_PHOTOS_DIR, exist_ok=True)
os.makedirs(INSTRUMENT_DIR, exist_ok=True)


def _ensure_posts_table():
    """Create the posts table if it doesn't exist. Idempotent — safe to call on every request."""
    with sqlite3.connect(DB_PATH, timeout=15) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS photo_owners (
            filename TEXT PRIMARY KEY,
            username TEXT NOT NULL
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            photo_filename TEXT,
            visibility TEXT NOT NULL DEFAULT 'friends',
            lat REAL,
            lon REAL,
            area_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_posts_username ON posts(username)')
        db.commit()

# Named fishing areas around Monomoy / Chatham. Privacy-preserving: raw GPS is
# never shown in the feed — only the matching area name. Order matters (first match wins).
AREAS = [
    ((41.645, 41.720), (-69.980, -69.900), "Pollock Rip"),
    ((41.520, 41.580), (-70.010, -69.940), "Stonehorse Shoal"),
    ((41.525, 41.575), (-69.940, -69.860), "Bearse Shoals"),
    ((41.450, 41.525), (-70.050, -69.900), "Handkerchief Shoal"),
    ((41.550, 41.650), (-70.000, -69.940), "Monomoy Shoals"),
    ((41.665, 41.705), (-70.025, -69.960), "Stage Harbor"),
    ((41.680, 41.720), (-69.960, -69.900), "Chatham Bars"),
    ((41.700, 41.800), (-70.050, -69.940), "Pleasant Bay"),
    ((41.770, 41.880), (-70.020, -69.900), "Nauset"),
    ((41.400, 41.525), (-70.100, -69.850), "South of Monomoy"),
]


def coords_to_area_name(lat, lon):
    """Map a coordinate to a named area. Falls back to a generic regional label."""
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    for (lat_lo, lat_hi), (lon_lo, lon_hi), name in AREAS:
        if lat_lo <= lat <= lat_hi and lon_lo <= lon <= lon_hi:
            return name
    # Broad Cape Cod fallback
    if 41.3 <= lat <= 42.2 and -70.5 <= lon <= -69.7:
        return "Chatham Area"
    return "Offshore"


def time_ago(iso_ts):
    """Format an ISO timestamp as '5m ago' / '2h ago' / '3d ago' / 'Apr 15'."""
    try:
        dt = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return ''
    delta = datetime.now() - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return 'just now'
    if secs < 3600:
        return f'{secs // 60}m ago'
    if secs < 86400:
        return f'{secs // 3600}h ago'
    if secs < 86400 * 7:
        return f'{secs // 86400}d ago'
    return dt.strftime('%b %d')


# ==================== CLAUDE VISION ====================

VISION_PROMPT = """You are helping a charter captain log a catch from a photo taken on the water near Chatham, MA.

Identify the fish and estimate its length in inches. Common species here: Striped Bass, Bluefish, False Albacore, Bonito, Fluke, Black Sea Bass, Scup, Tautog, Tuna. If you can't see a fish clearly, say so.

Size estimation — use whatever reference is in the frame: the angler's hand, forearm, a rod, a boat deck plank. Be realistic, not generous. A striper "looks big in the hand" is not a 40-incher.

If a lure or bait is visible in the fish's mouth or the frame, identify it: type and color (e.g. "white bucktail", "chartreuse soft plastic", "live eel", "metal jig"). If no lure/bait is visible, return null — do not guess.

Respond with ONLY valid JSON — no markdown, no prose:
{
  "species": "<e.g. 'Striped Bass' or 'Unknown'>",
  "size_inches": <number or null if not estimable>,
  "species_confidence": "<low|medium|high>",
  "size_confidence": "<low|medium|high>",
  "lure": "<lure/bait type + color, e.g. 'white bucktail'; null if not visible>",
  "lure_confidence": "<low|medium|high>",
  "notes": "<one short sentence on what you see — reference used, condition of fish, anything notable>"
}"""


INSTRUMENT_VISION_PROMPT = """You are reading a marine electronics screen (Garmin/Lowrance/Humminbird fishfinder/chartplotter) photographed on a fishing boat near Chatham, MA.

The screen may be a full sonar page, or a split page showing sonar AND a position/chart panel. Extract ONLY values that are clearly legible as on-screen numbers — do NOT infer or estimate. If a value is not visible or you cannot read it confidently, return null for that field.

Read units off the screen. Temperature may be °F or °C. Depth may be feet (ft) or meters (m). Report the raw number and the unit you saw in "units_seen"; do NOT convert — the caller converts.

- water_temp_f: the main water temperature reading (often a large 'TEMP' value). Report the NUMBER as shown.
- surface_temp_f: a separate surface temperature reading if the screen distinguishes one; else null.
- depth_ft: the depth reading as shown (often a large 'DEPTH' number).
- lat, lon: GPS position if a position/chart panel is shown (decimal degrees; west longitude negative). Null if no position shown.
- speed_kt: speed over ground if shown.
- units_seen: object noting the units printed on screen, e.g. {"temp":"F","depth":"ft"} or {"temp":"C","depth":"m"}.

Respond with ONLY valid JSON — no markdown, no prose:
{
  "water_temp_f": <number as shown or null>,
  "surface_temp_f": <number as shown or null>,
  "depth_ft": <number as shown or null>,
  "lat": <decimal degrees or null>,
  "lon": <decimal degrees or null>,
  "speed_kt": <number or null>,
  "units_seen": {"temp": "<F|C|null>", "depth": "<ft|m|null>"}
}"""


def _extract_text(response):
    """Pull the text blocks out of an Anthropic response (skipping thinking blocks)."""
    pieces = []
    for block in response.content:
        btype = getattr(block, 'type', None)
        if btype == 'text':
            pieces.append(block.text)
    return ''.join(pieces).strip()


def _parse_vision_json(text):
    """Claude may return JSON with leading/trailing whitespace or stray prose. Extract the JSON object."""
    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`')
        if text.lower().startswith('json'):
            text = text[4:].strip()
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1:
        raise ValueError(f'No JSON object in response: {text[:200]}')
    return json.loads(text[start:end + 1])


def _parse_photo_with_claude(image_bytes, media_type='image/jpeg'):
    """Send image to Claude Vision for species/size identification. Returns dict."""
    import anthropic

    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise RuntimeError('ANTHROPIC_API_KEY not configured')

    # Hard client timeout: this runs inside the /parse-catch-photo request, which
    # must finish inside gunicorn's 60s worker budget. One attempt, no retries —
    # a retried 45s call would blow the budget and get the worker killed.
    client = anthropic.Anthropic(api_key=api_key, timeout=45.0, max_retries=0)
    img_b64 = base64.standard_b64encode(image_bytes).decode('ascii')

    response = client.messages.create(
        model='claude-opus-4-7',
        max_tokens=1024,
        thinking={'type': 'adaptive'},
        messages=[{
            'role': 'user',
            'content': [
                {
                    'type': 'image',
                    'source': {
                        'type': 'base64',
                        'media_type': media_type,
                        'data': img_b64,
                    },
                },
                {'type': 'text', 'text': VISION_PROMPT},
            ],
        }],
    )
    text = _extract_text(response)
    return _parse_vision_json(text)


def _c_to_f(c):
    return c * 9 / 5 + 32


def _m_to_ft(m):
    return m * 3.28084


def _normalize_instrument(raw):
    """Convert a raw Garmin extraction to canonical F / feet. Returns a dict with
    water_temp_f, depth_ft, lat, lon, speed_kt — each None if illegible/absent."""
    units = raw.get('units_seen') or {}
    temp_unit = str(units.get('temp') or '').strip().upper()
    depth_unit = str(units.get('depth') or '').strip().lower()

    def num(v):
        try:
            if v is None:
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    # Temperature: prefer a dedicated water temp, fall back to surface temp.
    temp = num(raw.get('water_temp_f'))
    if temp is None:
        temp = num(raw.get('surface_temp_f'))
    if temp is not None and temp_unit == 'C':
        temp = _c_to_f(temp)

    depth = num(raw.get('depth_ft'))
    if depth is not None and depth_unit in ('m', 'meter', 'meters'):
        depth = _m_to_ft(depth)

    lat = num(raw.get('lat'))
    lon = num(raw.get('lon'))
    speed = num(raw.get('speed_kt'))

    return {
        'water_temp_f': round(temp, 1) if temp is not None else None,
        'depth_ft': round(depth, 1) if depth is not None else None,
        'lat': lat,
        'lon': lon,
        'speed_kt': round(speed, 1) if speed is not None else None,
    }


def _parse_instrument_with_claude(image_bytes, media_type='image/jpeg'):
    """Read a Garmin/fishfinder screen via Opus Vision. Returns canonical (F/feet) dict.
    The image is used ONLY for extraction here — the caller must never persist it to
    the posts table or any feed photo directory."""
    import anthropic

    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise RuntimeError('ANTHROPIC_API_KEY not configured')

    # Runs in the post-save background thread, so no gunicorn deadline — but cap
    # it anyway so a hung API call can't pin a thread until the next deploy.
    client = anthropic.Anthropic(api_key=api_key, timeout=60.0, max_retries=1)
    img_b64 = base64.standard_b64encode(image_bytes).decode('ascii')

    response = client.messages.create(
        model='claude-opus-4-8',
        max_tokens=1024,
        thinking={'type': 'adaptive'},
        messages=[{
            'role': 'user',
            'content': [
                {
                    'type': 'image',
                    'source': {
                        'type': 'base64',
                        'media_type': media_type,
                        'data': img_b64,
                    },
                },
                {'type': 'text', 'text': INSTRUMENT_VISION_PROMPT},
            ],
        }],
    )
    raw = _parse_vision_json(_extract_text(response))
    return _normalize_instrument(raw)


# ==================== IMAGE HANDLING ====================

def _resize_and_save(image_bytes, out_path, max_width=1200, quality=85):
    """Resize to max_width (preserving aspect) and save as JPEG. Strips EXIF."""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)  # honor phone rotation metadata, then drop it
    if img.mode != 'RGB':
        img = img.convert('RGB')
    w, h = img.size
    if w > max_width:
        new_h = int(h * (max_width / w))
        resample = getattr(getattr(Image, 'Resampling', None), 'LANCZOS', None) or Image.LANCZOS
        img = img.resize((max_width, new_h), resample)
    img.save(out_path, 'JPEG', quality=quality, optimize=True)


def _extract_exif_gps(image_bytes):
    """Pull (lat, lon) from an image's EXIF GPS tags. Returns (None, None) if absent/unreadable.
    Works on the raw uploaded bytes — must be called before we strip EXIF during resize."""
    try:
        from PIL import Image, ExifTags
    except ImportError:
        return (None, None)
    try:
        img = Image.open(io.BytesIO(image_bytes))
        exif = img.getexif()
        if not exif:
            return (None, None)
        # GPSInfo tag id is 34853; its value is an IFD dict keyed by GPS tag ids.
        gps_ifd = exif.get_ifd(34853) if hasattr(exif, 'get_ifd') else None
        if not gps_ifd:
            return (None, None)
        gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
        lat_ref = gps.get('GPSLatitudeRef')
        lat_dms = gps.get('GPSLatitude')
        lon_ref = gps.get('GPSLongitudeRef')
        lon_dms = gps.get('GPSLongitude')
        if not (lat_dms and lon_dms and lat_ref and lon_ref):
            return (None, None)

        def dms_to_deg(dms):
            # Pillow returns each component as a Fraction/IFDRational or a (num, den) tuple
            def _num(x):
                if isinstance(x, tuple) and len(x) == 2 and x[1]:
                    return x[0] / x[1]
                return float(x)
            d, m, s = dms
            return _num(d) + _num(m) / 60.0 + _num(s) / 3600.0

        lat = dms_to_deg(lat_dms)
        lon = dms_to_deg(lon_dms)
        if str(lat_ref).upper().startswith('S'):
            lat = -lat
        if str(lon_ref).upper().startswith('W'):
            lon = -lon
        return (lat, lon)
    except Exception as e:
        logger.debug(f'EXIF GPS extract failed: {e}')
        return (None, None)


EXIF_MAX_AGE_DAYS = 14


def _validate_capture_time(dt):
    """Clamp a claimed capture time: no future, no older than EXIF_MAX_AGE_DAYS.
    Returns the datetime or None."""
    now = datetime.now()
    if dt > now + timedelta(minutes=10):
        return None
    if dt < now - timedelta(days=EXIF_MAX_AGE_DAYS):
        return None
    return dt


def _client_capture_time(raw):
    """Parse a client-supplied ISO capture time (photo EXIF read client-side,
    since large photos are re-encoded — EXIF-stripped — before upload)."""
    if not raw:
        return None
    try:
        return _validate_capture_time(datetime.fromisoformat(str(raw).strip()[:19]))
    except (TypeError, ValueError):
        return None


def _extract_exif_datetime(image_bytes):
    """Pull the capture time from EXIF (DateTimeOriginal, falling back to
    DateTimeDigitized, then DateTime). Returns a naive local datetime or None.
    Must be called on the raw uploaded bytes — resize strips EXIF.
    Rejects implausible values (future, or more than EXIF_MAX_AGE_DAYS old —
    protects the pattern engine from doctored/wrong camera dates)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        exif = img.getexif()
        if not exif:
            return None
        raw = None
        # Exif sub-IFD (34665): 36867 DateTimeOriginal, 36868 DateTimeDigitized
        try:
            exif_ifd = exif.get_ifd(34665) if hasattr(exif, 'get_ifd') else {}
            raw = exif_ifd.get(36867) or exif_ifd.get(36868)
        except Exception:
            pass
        # IFD0 fallback: 306 DateTime
        if not raw:
            raw = exif.get(306)
        if not raw:
            return None
        dt = datetime.strptime(str(raw).strip(), '%Y:%m:%d %H:%M:%S')
        return _validate_capture_time(dt)
    except Exception as e:
        logger.debug(f'EXIF datetime extract failed: {e}')
        return None


def _record_photo_owner(filename, username):
    """Authoritative owner record — photo auth checks use this, not the lossy
    sanitized token embedded in the filename."""
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as db:
            db.execute('INSERT OR REPLACE INTO photo_owners (filename, username) VALUES (?, ?)',
                       (filename, username))
            db.commit()
    except Exception as e:
        logger.warning(f'photo owner record failed: {e}')


def _find_catch_by_client_id(client_id, max_files=400):
    """Scan recent catch JSONs for a matching client_id (idempotency token sent
    by the app, stable across retries of the same catch). Bounded and cheap —
    catch files are ~1KB. Returns the entry dict with '_path' set, or None."""
    if not client_id:
        return None
    try:
        paths = globmod.glob(os.path.join(LOGS_DIR, 'catch_*.json'))
        paths.sort(key=os.path.getmtime, reverse=True)
        for p in paths[:max_files]:
            try:
                with open(p) as f:
                    entry = json.load(f)
            except Exception:
                continue
            if entry.get('client_id') == client_id:
                entry['_path'] = p
                return entry
    except Exception as e:
        logger.warning(f'client_id scan failed: {e}')
    return None


def _merge_into_catch_file(log_path, updates):
    """Atomically merge keys into an already-saved catch JSON. The catch may have
    been edited or deleted while the background work ran — re-read it fresh and
    touch only the keys we own. Returns True on success."""
    try:
        with open(log_path) as f:
            entry = json.load(f)
    except FileNotFoundError:
        logger.info(f'catch enrich skipped — {os.path.basename(log_path)} was deleted')
        return False
    except Exception as e:
        logger.warning(f'catch enrich read failed for {log_path}: {e}')
        return False
    entry.update(updates)
    tmp_path = log_path + '.tmp'
    try:
        with open(tmp_path, 'w') as f:
            json.dump(entry, f, indent=2)
        os.replace(tmp_path, log_path)  # atomic — feed readers never see a torn file
        return True
    except Exception as e:
        logger.warning(f'catch enrich write failed for {log_path}: {e}')
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False


def _enrich_catch_async(log_path, instr_bytes, instr_media_type,
                        lat, lon, catch_dt, username, user_spot):
    """Post-save enrichment, run in a daemon thread AFTER /log-catch-photo has
    responded. Does the two slow jobs that used to run inside the request and
    could blow gunicorn's 60s worker timeout when a sonar photo was attached
    (killing the worker and losing the catch):
      1. Claude Vision read of the Garmin/instrument photo (20-40s)
      2. conditions snapshot (live ERDDAP/buoy/tide fetches on a fresh catch —
         the ERDDAP request timeout alone is 60s)
    Results are merged into the already-saved catch file. Any failure here just
    leaves the catch without those fields — it can never lose the catch."""
    instrument = None
    if instr_bytes:
        try:
            instrument = _parse_instrument_with_claude(
                instr_bytes, media_type=instr_media_type)
            logger.info(f'Instrument extract for {username}: {instrument}')
        except Exception as e:
            logger.warning(f'Instrument photo parse skipped: {e}')

    updates = {}
    garmin_temp = instrument.get('water_temp_f') if instrument else None
    garmin_depth = instrument.get('depth_ft') if instrument else None
    if instrument and instrument.get('lat') is not None and instrument.get('lon') is not None:
        # GPS read off the chartplotter wins over browser/EXIF fixes.
        lat, lon = instrument['lat'], instrument['lon']
        updates['gps'] = {'lat': lat, 'lon': lon}
        updates['gps_source'] = 'instrument'
        area = coords_to_area_name(lat, lon)
        if area:
            updates['area_name'] = area
            if not user_spot:
                # Spot was auto-derived from GPS at save time — keep it in step
                # with the better fix. A captain-typed spot is never touched.
                updates['spot'] = area
    if instrument:
        updates['instrument'] = instrument

    try:
        from conditions import build_conditions_snapshot
        conditions = build_conditions_snapshot(
            lat=lat, lon=lon, depth_ft=garmin_depth, water_temp_f=garmin_temp,
            at=catch_dt)
        if conditions:
            updates['conditions'] = conditions
    except Exception as e:
        logger.warning(f'Conditions snapshot skipped: {e}')

    if updates:
        _merge_into_catch_file(log_path, updates)


def _photo_owner(filename):
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT username FROM photo_owners WHERE filename = ?',
                             (filename,)).fetchone()
            return row['username'] if row else None
    except Exception as e:
        logger.warning(f'photo owner lookup failed: {e}')
        return None


# ==================== ROUTES ====================

def register_photo_catch_routes(app, login_required):
    """Register photo catch + feed routes with the Flask app."""
    from flask import request, jsonify, session, send_from_directory

    _ensure_posts_table()

    @app.route('/parse-catch-photo', methods=['POST'])
    @login_required
    def parse_catch_photo():
        """Identify species + estimate size from an uploaded photo via Claude Vision."""
        photo = request.files.get('photo')
        if not photo:
            return jsonify({'error': 'No photo uploaded'}), 400
        try:
            image_bytes = photo.read()
            if not image_bytes:
                return jsonify({'error': 'Empty photo'}), 400
            if len(image_bytes) > 15 * 1024 * 1024:
                return jsonify({'error': 'Photo too large (max 15MB)'}), 413
            media_type = photo.mimetype if photo.mimetype in (
                'image/jpeg', 'image/png', 'image/gif', 'image/webp') else 'image/jpeg'
            parsed = _parse_photo_with_claude(image_bytes, media_type=media_type)
            exif_lat, exif_lon = _extract_exif_gps(image_bytes)
            exif_area = coords_to_area_name(exif_lat, exif_lon) if exif_lat is not None else None
            exif_dt = _extract_exif_datetime(image_bytes)
            logger.info(f'Vision parse for {session.get("username","?")}: '
                        f'{parsed.get("species","?")} {parsed.get("size_inches","?")}in '
                        f'exif_gps={"yes" if exif_lat is not None else "no"}')
            return jsonify({
                'species': parsed.get('species', ''),
                'size_inches': parsed.get('size_inches'),
                'species_confidence': parsed.get('species_confidence', 'low'),
                'size_confidence': parsed.get('size_confidence', 'low'),
                'lure': parsed.get('lure') or '',
                'lure_confidence': parsed.get('lure_confidence', 'low'),
                'notes': parsed.get('notes', ''),
                'exif_lat': exif_lat,
                'exif_lon': exif_lon,
                'exif_area_name': exif_area,
                'exif_time': exif_dt.isoformat() if exif_dt else None,
            })
        except Exception as e:
            logger.error(f'Vision parse failed: {e}')
            return jsonify({'error': 'Could not identify the fish. Try another photo.'}), 500

    @app.route('/log-catch-photo', methods=['POST'])
    @login_required
    def log_catch_photo():
        """Save a catch with a photo. Multipart form: photo + JSON fields."""
        # Idempotency: retries (manual re-tap or the phone's offline/failure queue)
        # carry the same client_id as the original attempt. If that catch already
        # made it to disk, acknowledge it instead of logging the fish twice.
        client_id = (request.form.get('client_id') or '').strip()[:64]
        if client_id:
            existing = _find_catch_by_client_id(client_id)
            if existing is not None:
                logger.info(f'Duplicate save ignored (client_id={client_id}): '
                            f'{os.path.basename(existing["_path"])}')
                return jsonify({
                    'saved': True,
                    'duplicate': True,
                    'filename': os.path.basename(existing['_path']),
                    'photo_filename': existing.get('photo_filename'),
                    'area_name': existing.get('area_name'),
                })

        photo = request.files.get('photo')
        if not photo:
            return jsonify({'error': 'Photo required'}), 400

        image_bytes = photo.read()
        if not image_bytes:
            return jsonify({'error': 'Empty photo'}), 400
        if len(image_bytes) > 15 * 1024 * 1024:
            return jsonify({'error': 'Photo too large (max 15MB)'}), 413

        # Form fields
        species = (request.form.get('species') or '').strip()
        size_inches_raw = (request.form.get('size_inches') or '').strip()
        try:
            size_inches = float(size_inches_raw) if size_inches_raw else None
        except ValueError:
            size_inches = None
        spot = (request.form.get('spot') or '').strip()
        technique = (request.form.get('technique') or '').strip()
        lure = (request.form.get('lure') or '').strip()
        notes = (request.form.get('notes') or '').strip()
        species_confidence = (request.form.get('species_confidence') or '').strip()
        size_confidence = (request.form.get('size_confidence') or '').strip()

        try:
            lat = float(request.form['lat']) if request.form.get('lat') else None
            lon = float(request.form['lon']) if request.form.get('lon') else None
        except ValueError:
            lat = lon = None
        # GPS provenance: the client tracks whether coords came from photo EXIF
        # (read client-side before re-encoding strips it) or a live device fix.
        client_gps_source = (request.form.get('gps_source') or '').strip().lower()
        if client_gps_source not in ('exif', 'browser', 'manual'):
            client_gps_source = 'browser'
        gps_source = client_gps_source if (lat is not None and lon is not None) else None
        if lat is None or lon is None:
            exif_lat, exif_lon = _extract_exif_gps(image_bytes)
            if exif_lat is not None and exif_lon is not None:
                lat, lon = exif_lat, exif_lon
                gps_source = 'exif'
        gps = {'lat': lat, 'lon': lon} if lat is not None and lon is not None else None

        # Catch time: prefer the photo's EXIF capture time over upload time.
        # Server-side extraction wins; client-read EXIF time (sent as exif_time)
        # covers large photos that were re-encoded before upload.
        taken_dt = _extract_exif_datetime(image_bytes) or \
            _client_capture_time(request.form.get('exif_time'))
        time_source = 'exif' if taken_dt else 'upload'
        if taken_dt is None:
            # Offline queue: the catch was saved on the boat and synced later.
            # queued_at is when the captain hit Save — far closer to the truth
            # than the upload time. Same validation window as EXIF times.
            queued = _client_capture_time(request.form.get('queued_at'))
            if queued is not None:
                taken_dt = queued
                time_source = 'queued'
        catch_dt = taken_dt or datetime.now()

        # Save photo
        username = session.get('username', 'unknown')
        safe_user = _safe_user(username)
        ts = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        photo_filename = f'catch_{safe_user}_{ts}.jpg'
        photo_path = os.path.join(PHOTOS_DIR, photo_filename)
        try:
            _resize_and_save(image_bytes, photo_path)
        except Exception as e:
            logger.error(f'Photo resize/save failed: {e}')
            return jsonify({'error': 'Could not save photo'}), 500
        _record_photo_owner(photo_filename, username)

        # Optional Garmin/instrument photo: store it in the private instrument dir
        # now (fast, resized, EXIF-stripped — never served to the feed). The Claude
        # read of it is SLOW (20-40s) and runs after the response, in
        # _enrich_catch_async — doing it inline used to blow gunicorn's 60s worker
        # timeout and lose the whole catch.
        instr_bytes = None
        instr_media_type = 'image/jpeg'
        instr_file = request.files.get('instrument_photo')
        if instr_file:
            try:
                raw_instr = instr_file.read()
                if raw_instr and len(raw_instr) <= 15 * 1024 * 1024:
                    instr_bytes = raw_instr
                    if instr_file.mimetype in (
                            'image/jpeg', 'image/png', 'image/gif', 'image/webp'):
                        instr_media_type = instr_file.mimetype
                    try:
                        instr_name = f'instrument_{safe_user}_{ts}.jpg'
                        _resize_and_save(instr_bytes, os.path.join(INSTRUMENT_DIR, instr_name))
                        _record_photo_owner(instr_name, username)
                    except Exception as e:
                        logger.warning(f'Instrument photo store skipped: {e}')
            except Exception as e:
                logger.warning(f'Instrument photo read skipped: {e}')

        # Conditions + instrument values are filled in by _enrich_catch_async after
        # the response goes out; the entry is saved immediately with what we have.
        conditions = {}

        area_name = coords_to_area_name(lat, lon)

        entry = {
            'timestamp': catch_dt.isoformat(),
            'time_source': time_source,
            'logged_by': username,
            'spot': spot or (area_name or ''),
            'gps': gps,
            'area_name': area_name,
            'species': species,
            'technique': technique,
            'lure': lure,
            'notes': notes,
            'conditions': conditions,
            'photo_filename': photo_filename,
            'size_inches': size_inches,
            'species_confidence': species_confidence,
            'size_confidence': size_confidence,
            'source': 'photo',
            'gps_source': gps_source,
            'client_id': client_id or None,
        }

        log_filename = f'catch_{ts}.json'
        log_path = os.path.join(LOGS_DIR, log_filename)
        try:
            with open(log_path, 'w') as f:
                json.dump(entry, f, indent=2)
        except Exception as e:
            logger.error(f'Catch log write failed: {e}')
            return jsonify({'error': 'Could not save catch'}), 500
        logger.info(f'Photo catch logged by {username}: {species or "?"} '
                    f'{size_inches or "?"}in @ {area_name or "no-gps"}')

        # Kick off the slow enrichment (sonar-photo Claude read + live conditions
        # fetches) in the background — the catch is already safe on disk.
        threading.Thread(
            target=_enrich_catch_async,
            args=(log_path, instr_bytes, instr_media_type, lat, lon, catch_dt,
                  username, spot),
            daemon=True,
        ).start()

        # Crew notifications (DB insert — mirrors captain_advisor flow)
        try:
            with sqlite3.connect(DB_PATH, timeout=15) as ndb:
                ndb.row_factory = sqlite3.Row
                groups = ndb.execute('''
                    SELECT g.id, g.name FROM friend_groups g
                    JOIN group_members gm ON g.id = gm.group_id
                    WHERE gm.username = ? AND gm.share_my_catches = 1
                ''', (username,)).fetchall()
                for group in groups:
                    members = ndb.execute('''
                        SELECT username FROM group_members
                        WHERE group_id = ? AND username != ? AND share_my_catches = 1
                    ''', (group['id'], username)).fetchall()
                    for member in members:
                        label = species or 'a fish'
                        if size_inches:
                            label = f'{int(size_inches)}" {label}'
                        msg = f'{username} just logged {label}'
                        if area_name:
                            msg += f' at {area_name}'
                        ndb.execute('''
                            INSERT INTO group_notifications
                            (group_id, group_name, from_user, to_user, spot, species, message)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (group['id'], group['name'], username, member['username'],
                              area_name or '', species, msg))
                        try:
                            from push_notify import notify_user
                            notify_user(member['username'], f"\U0001F41F {group['name']}", msg)
                        except Exception as e:
                            logger.warning(f'push notify failed: {e}')
                ndb.commit()
        except Exception as e:
            logger.error(f'Photo catch notification failed: {e}')

        return jsonify({
            'saved': True,
            'filename': log_filename,
            'photo_filename': photo_filename,
            'area_name': area_name,
        })

    def _safe_user(u):
        return ''.join(c for c in (u or '') if c.isalnum() or c in ('_', '-', '.'))[:40] or 'user'

    @app.route('/catch-photos/<filename>')
    @login_required
    def serve_catch_photo(filename):
        """Serve a saved catch photo. Only members of the photo owner's crews may view."""
        if '..' in filename or '/' in filename or '\\' in filename:
            return jsonify({'error': 'Invalid filename'}), 400
        photo_path = os.path.join(PHOTOS_DIR, filename)
        if not os.path.exists(photo_path):
            return jsonify({'error': 'Not found'}), 404
        # Authorization: photo is visible if viewer is the owner or shares a crew with owner.
        # Photo filename format: catch_<safe_user>_<timestamp>.jpg — parse owner.
        # Note: safe_user strips '@' etc., so compare against the same normalization of real usernames.
        viewer = session.get('username', '')
        owner = _photo_owner(filename)  # authoritative
        if owner is not None:
            if owner != viewer:
                try:
                    with sqlite3.connect(DB_PATH, timeout=15) as ndb:
                        ndb.row_factory = sqlite3.Row
                        rows = ndb.execute('''
                            SELECT DISTINCT m2.username FROM group_members m1
                            JOIN group_members m2 ON m1.group_id = m2.group_id
                            WHERE m1.username = ?
                        ''', (viewer,)).fetchall()
                        if not any(r['username'] == owner for r in rows):
                            return jsonify({'error': 'Not authorized'}), 403
                except Exception as e:
                    logger.error(f'Photo auth check failed: {e}')
                    return jsonify({'error': 'Not authorized'}), 403
            return send_from_directory(PHOTOS_DIR, filename)
        # Legacy photos (no owner row): fall back to the filename token check.
        owner_safe = ''
        parts = filename.split('_', 2)
        if len(parts) >= 3 and parts[0] == 'catch':
            owner_safe = parts[1]
        if owner_safe and owner_safe != _safe_user(viewer):
            try:
                with sqlite3.connect(DB_PATH, timeout=15) as ndb:
                    ndb.row_factory = sqlite3.Row
                    rows = ndb.execute('''
                        SELECT DISTINCT m2.username FROM group_members m1
                        JOIN group_members m2 ON m1.group_id = m2.group_id
                        WHERE m1.username = ?
                    ''', (viewer,)).fetchall()
                    allowed = any(_safe_user(r['username']) == owner_safe for r in rows)
                    if not allowed:
                        return jsonify({'error': 'Not authorized'}), 403
            except Exception as e:
                logger.error(f'Photo auth check failed: {e}')
                return jsonify({'error': 'Not authorized'}), 403
        return send_from_directory(PHOTOS_DIR, filename)

    @app.route('/instrument-photos/<filename>')
    @login_required
    def serve_instrument_photo(filename):
        """Serve a private Garmin/instrument screenshot. OWNER ONLY — these are
        never crew-shared and must NEVER appear in any feed. Unlike catch photos,
        there is no crew fallback: only the angler who logged it may view it."""
        if '..' in filename or '/' in filename or '\\' in filename:
            return jsonify({'error': 'Invalid filename'}), 400
        instr_path = os.path.join(INSTRUMENT_DIR, filename)
        if not os.path.exists(instr_path):
            return jsonify({'error': 'Not found'}), 404
        # Filename format: instrument_<safe_user>_<timestamp>.jpg — parse owner.
        viewer = session.get('username', '')
        owner = _photo_owner(filename)
        if owner is not None:
            if owner != viewer:
                return jsonify({'error': 'Not authorized'}), 403
            return send_from_directory(INSTRUMENT_DIR, filename)
        # Legacy fallback: filename token.
        owner_safe = ''
        parts = filename.split('_', 2)
        if len(parts) >= 3 and parts[0] == 'instrument':
            owner_safe = parts[1]
        if not owner_safe or owner_safe != _safe_user(viewer):
            return jsonify({'error': 'Not authorized'}), 403
        return send_from_directory(INSTRUMENT_DIR, filename)

    @app.route('/api/crew-feed')
    @login_required
    def crew_feed():
        """Last 7 days of catches across every crew the user belongs to.
        Returns named areas (no raw GPS). Deduplicates across overlapping crews."""
        username = session.get('username', '')
        cutoff = datetime.now() - timedelta(days=7)

        # Everyone whose catches this user is allowed to see: all members of all their crews
        # who have sharing enabled. Include self.
        try:
            with sqlite3.connect(DB_PATH, timeout=15) as ndb:
                ndb.row_factory = sqlite3.Row
                my_group_ids = [r['group_id'] for r in ndb.execute(
                    'SELECT group_id FROM group_members WHERE username = ?',
                    (username,)).fetchall()]
                visible = {username}
                user_groups = {}  # username -> first group name (for display)
                if my_group_ids:
                    placeholders = ','.join('?' for _ in my_group_ids)
                    rows = ndb.execute(f'''
                        SELECT DISTINCT gm.username, g.name as group_name
                        FROM group_members gm
                        JOIN friend_groups g ON g.id = gm.group_id
                        WHERE gm.group_id IN ({placeholders})
                          AND gm.share_my_catches = 1
                    ''', my_group_ids).fetchall()
                    for r in rows:
                        visible.add(r['username'])
                        user_groups.setdefault(r['username'], r['group_name'])
                # Display names
                name_rows = ndb.execute(
                    f'SELECT username, first_name FROM users WHERE username IN '
                    f'({",".join("?" for _ in visible)})',
                    list(visible)).fetchall() if visible else []
                display_names = {r['username']: (r['first_name'] or r['username'].split('@')[0])
                                 for r in name_rows}
        except Exception as e:
            logger.error(f'Feed DB error: {e}')
            return jsonify({'catches': []})

        # Walk recent catch logs (filename sort is roughly chronological but not strict —
        # iterate a bounded slice and filter by timestamp).
        files = sorted(globmod.glob(os.path.join(LOGS_DIR, 'catch_*.json')), reverse=True)[:500]
        feed = []
        for fp in files:
            try:
                with open(fp) as f:
                    entry = json.load(f)
                owner = entry.get('logged_by', '')
                if owner not in visible:
                    continue
                ts = entry.get('timestamp', '')
                try:
                    dt = datetime.fromisoformat(ts)
                except (TypeError, ValueError):
                    continue
                if dt < cutoff:
                    continue

                gps = entry.get('gps') or {}
                area = entry.get('area_name') or coords_to_area_name(
                    gps.get('lat'), gps.get('lon')) or entry.get('spot') or ''

                feed.append({
                    'captain': display_names.get(owner, owner.split('@')[0] if owner else ''),
                    'username': owner,
                    'species': entry.get('species', ''),
                    'size_inches': entry.get('size_inches'),
                    'area': area,
                    'crew': user_groups.get(owner, ''),
                    'photo_filename': entry.get('photo_filename'),
                    'time_ago': time_ago(ts),
                    'timestamp': ts,
                    'is_self': owner == username,
                })
            except Exception as e:
                logger.debug(f'Skipping catch file {fp}: {e}')

        return jsonify({'catches': feed[:100]})

    # ==================== POSTS ====================

    def _visible_usernames(viewer):
        """Set of usernames whose 'friends' content this viewer may see.
        Always includes the viewer themselves. Plus everyone in any shared group."""
        visible = {viewer}
        try:
            with sqlite3.connect(DB_PATH, timeout=15) as ndb:
                ndb.row_factory = sqlite3.Row
                rows = ndb.execute('''
                    SELECT DISTINCT m2.username
                    FROM group_members m1
                    JOIN group_members m2 ON m1.group_id = m2.group_id
                    WHERE m1.username = ?
                ''', (viewer,)).fetchall()
                for r in rows:
                    visible.add(r['username'])
        except Exception as e:
            logger.error(f'visible_usernames query failed: {e}')
        return visible

    def _display_names_for(usernames):
        if not usernames:
            return {}
        try:
            with sqlite3.connect(DB_PATH, timeout=15) as ndb:
                ndb.row_factory = sqlite3.Row
                placeholders = ','.join('?' for _ in usernames)
                rows = ndb.execute(
                    f'SELECT username, first_name FROM users WHERE username IN ({placeholders})',
                    list(usernames)).fetchall()
                return {r['username']: (r['first_name'] or r['username'].split('@')[0])
                        for r in rows}
        except Exception as e:
            logger.error(f'display_names query failed: {e}')
            return {}

    @app.route('/api/post', methods=['POST'])
    @login_required
    def create_post():
        """Create a text post (optional photo). multipart/form-data OR JSON.
        Fields: body (required unless photo), photo (optional), visibility (friends|public),
                lat, lon (optional — for area_name derivation; never returned as coords)."""
        _ensure_posts_table()
        username = session.get('username', 'unknown')

        if request.content_type and request.content_type.startswith('multipart/'):
            body = (request.form.get('body') or '').strip()
            visibility = (request.form.get('visibility') or 'friends').strip().lower()
            lat_raw = request.form.get('lat')
            lon_raw = request.form.get('lon')
            photo = request.files.get('photo')
        else:
            data = request.get_json(silent=True) or {}
            body = (data.get('body') or '').strip()
            visibility = (data.get('visibility') or 'friends').strip().lower()
            lat_raw = data.get('lat')
            lon_raw = data.get('lon')
            photo = None

        if visibility not in ('friends', 'public'):
            visibility = 'friends'
        if not body and not photo:
            return jsonify({'error': 'Write something or attach a photo'}), 400
        if len(body) > 2000:
            return jsonify({'error': 'Post too long (max 2000 chars)'}), 400

        try:
            lat = float(lat_raw) if lat_raw not in (None, '') else None
            lon = float(lon_raw) if lon_raw not in (None, '') else None
        except (TypeError, ValueError):
            lat = lon = None

        photo_filename = None
        if photo:
            image_bytes = photo.read()
            if not image_bytes:
                return jsonify({'error': 'Empty photo'}), 400
            if len(image_bytes) > 15 * 1024 * 1024:
                return jsonify({'error': 'Photo too large (max 15MB)'}), 413
            if lat is None or lon is None:
                exif_lat, exif_lon = _extract_exif_gps(image_bytes)
                if exif_lat is not None and exif_lon is not None:
                    lat, lon = exif_lat, exif_lon
            safe_user = _safe_user(username)
            ts = datetime.now().strftime('%Y-%m-%d_%H%M%S')
            photo_filename = f'post_{safe_user}_{ts}.jpg'
            try:
                _resize_and_save(image_bytes, os.path.join(POST_PHOTOS_DIR, photo_filename))
            except Exception as e:
                logger.error(f'Post photo save failed: {e}')
                return jsonify({'error': 'Could not save photo'}), 500
            _record_photo_owner(photo_filename, username)

        area_name = coords_to_area_name(lat, lon) if (lat is not None and lon is not None) else None

        try:
            with sqlite3.connect(DB_PATH, timeout=15) as ndb:
                cur = ndb.execute(
                    '''INSERT INTO posts (username, body, photo_filename, visibility, lat, lon, area_name)
                       VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (username, body, photo_filename, visibility, lat, lon, area_name))
                post_id = cur.lastrowid
                ndb.commit()
        except Exception as e:
            logger.error(f'Post insert failed: {e}')
            return jsonify({'error': 'Could not save post'}), 500

        logger.info(f'Post {post_id} by {username} ({visibility}, {len(body)} chars, '
                    f'photo={"yes" if photo_filename else "no"})')
        return jsonify({'saved': True, 'id': post_id})

    @app.route('/api/post/<int:post_id>', methods=['DELETE'])
    @login_required
    def delete_post(post_id):
        """Delete your own post. Also removes the photo file."""
        _ensure_posts_table()
        username = session.get('username', '')
        try:
            with sqlite3.connect(DB_PATH, timeout=15) as ndb:
                ndb.row_factory = sqlite3.Row
                row = ndb.execute(
                    'SELECT username, photo_filename FROM posts WHERE id = ?',
                    (post_id,)).fetchone()
                if not row:
                    return jsonify({'error': 'Not found'}), 404
                if row['username'] != username:
                    return jsonify({'error': 'Not your post'}), 403
                ndb.execute('DELETE FROM posts WHERE id = ?', (post_id,))
                ndb.commit()
            if row['photo_filename']:
                try:
                    os.remove(os.path.join(POST_PHOTOS_DIR, row['photo_filename']))
                except OSError:
                    pass
        except Exception as e:
            logger.error(f'Post delete failed: {e}')
            return jsonify({'error': 'Could not delete'}), 500
        return jsonify({'deleted': True})

    @app.route('/post-photos/<filename>')
    @login_required
    def serve_post_photo(filename):
        """Serve a post photo. Visibility enforced against the owning post row."""
        if '..' in filename or '/' in filename or '\\' in filename:
            return jsonify({'error': 'Invalid filename'}), 400
        photo_path = os.path.join(POST_PHOTOS_DIR, filename)
        if not os.path.exists(photo_path):
            return jsonify({'error': 'Not found'}), 404

        viewer = session.get('username', '')
        try:
            with sqlite3.connect(DB_PATH, timeout=15) as ndb:
                ndb.row_factory = sqlite3.Row
                row = ndb.execute(
                    'SELECT username, visibility FROM posts WHERE photo_filename = ?',
                    (filename,)).fetchone()
            if not row:
                return jsonify({'error': 'Not found'}), 404
            if row['visibility'] == 'public' or row['username'] == viewer:
                return send_from_directory(POST_PHOTOS_DIR, filename)
            if viewer in _visible_usernames(row['username']):
                return send_from_directory(POST_PHOTOS_DIR, filename)
        except Exception as e:
            logger.error(f'Post photo auth failed: {e}')
        return jsonify({'error': 'Not authorized'}), 403

    @app.route('/api/feed')
    @login_required
    def unified_feed():
        """Merged feed: recent catches (from shared crews) + posts (public + friends-of).
        Returns items sorted newest-first; no raw GPS ever leaves this endpoint."""
        _ensure_posts_table()
        username = session.get('username', '')
        cutoff = datetime.now() - timedelta(days=14)

        friends = _visible_usernames(username)

        items = []

        # --- Catches ---
        try:
            with sqlite3.connect(DB_PATH, timeout=15) as ndb:
                ndb.row_factory = sqlite3.Row
                # Who's in shared crews with me AND sharing enabled
                my_group_ids = [r['group_id'] for r in ndb.execute(
                    'SELECT group_id FROM group_members WHERE username = ?',
                    (username,)).fetchall()]
                catch_visible = {username}
                user_group_name = {}
                if my_group_ids:
                    placeholders = ','.join('?' for _ in my_group_ids)
                    rows = ndb.execute(f'''
                        SELECT DISTINCT gm.username, g.name as group_name
                        FROM group_members gm
                        JOIN friend_groups g ON g.id = gm.group_id
                        WHERE gm.group_id IN ({placeholders})
                          AND gm.share_my_catches = 1
                    ''', my_group_ids).fetchall()
                    for r in rows:
                        catch_visible.add(r['username'])
                        user_group_name.setdefault(r['username'], r['group_name'])
        except Exception as e:
            logger.error(f'Feed catch-visible query failed: {e}')
            catch_visible = {username}
            user_group_name = {}

        files = sorted(globmod.glob(os.path.join(LOGS_DIR, 'catch_*.json')), reverse=True)[:500]
        for fp in files:
            try:
                with open(fp) as f:
                    entry = json.load(f)
                owner = entry.get('logged_by', '')
                if owner not in catch_visible:
                    continue
                ts = entry.get('timestamp', '')
                try:
                    dt = datetime.fromisoformat(ts)
                except (TypeError, ValueError):
                    continue
                if dt < cutoff:
                    continue
                gps = entry.get('gps') or {}
                area = entry.get('area_name') or coords_to_area_name(
                    gps.get('lat'), gps.get('lon')) or entry.get('spot') or ''
                items.append({
                    'type': 'catch',
                    'sort_ts': ts,
                    'author': owner,
                    'species': entry.get('species', ''),
                    'size_inches': entry.get('size_inches'),
                    'area': area,
                    'crew': user_group_name.get(owner, ''),
                    'photo_filename': entry.get('photo_filename'),
                    'time_ago': time_ago(ts),
                })
            except Exception as e:
                logger.debug(f'Skipping catch file {fp}: {e}')

        # --- Posts ---
        try:
            with sqlite3.connect(DB_PATH, timeout=15) as ndb:
                ndb.row_factory = sqlite3.Row
                placeholders = ','.join('?' for _ in friends)
                post_rows = ndb.execute(f'''
                    SELECT id, username, body, photo_filename, visibility, area_name, created_at
                    FROM posts
                    WHERE (visibility = 'public' OR username IN ({placeholders}))
                      AND created_at >= ?
                    ORDER BY created_at DESC
                    LIMIT 200
                ''', list(friends) + [cutoff.strftime('%Y-%m-%d %H:%M:%S')]).fetchall()
                for r in post_rows:
                    ts = r['created_at']
                    if ts and 'T' not in str(ts):
                        ts_iso = str(ts).replace(' ', 'T')
                    else:
                        ts_iso = str(ts)
                    items.append({
                        'type': 'post',
                        'sort_ts': ts_iso,
                        'id': r['id'],
                        'author': r['username'],
                        'body': r['body'] or '',
                        'photo_filename': r['photo_filename'],
                        'visibility': r['visibility'],
                        'area': r['area_name'] or '',
                        'time_ago': time_ago(ts_iso),
                    })
        except Exception as e:
            logger.error(f'Feed posts query failed: {e}')

        all_authors = {it['author'] for it in items if it.get('author')}
        names = _display_names_for(all_authors)
        for it in items:
            it['captain'] = names.get(it['author'], (it['author'] or '').split('@')[0])
            it['is_self'] = it['author'] == username

        items.sort(key=lambda i: i.get('sort_ts', ''), reverse=True)
        return jsonify({'items': items[:150]})

    logger.info('Photo catch routes registered')
