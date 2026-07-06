#!/usr/bin/env python3
"""
Wheelhouse Morning Briefing — emailed daily at 5 AM via cron.

Assembles today's fishing picture for Chatham/Monomoy and renders a
GO / CAUTION / NO-GO call for the morning window, grounded in local
knowledge about wind:

  - East-component winds (NE/E/SE) signal an approaching low: pressure
    falling, dirty water on the Atlantic side. "Wind from the east,
    fish bite the least."
  - The prevailing summer SW breeze builds through the afternoon --
    light early SW is prime, 15kt+ SW turns the Sound sloppy.
  - Wind against tide stands the Monomoy rips up dangerously (steep
    8-10ft faces in the worst spots). Strong wind opposing the current
    is a safety problem, not just a fishing problem.

Also folds in the captain's own history: catches per wind direction,
joined from catch logs + conditions_log.

Usage:
  morning_briefing.py            # send the email
  morning_briefing.py --print    # dry run, print to stdout

Cron (5 AM daily):
  0 5 * * * /opt/wheelhouse/venv/bin/python /opt/wheelhouse/morning_briefing.py >> /opt/wheelhouse/logs/briefing.log 2>&1
"""

import os
import sys
import json
import glob
import socket
import sqlite3
import smtplib
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
load_dotenv('/opt/rednun/.env', override=False)
sys.path.insert(0, '/opt/wheelhouse')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('wh-briefing')

# Belt-and-suspenders: no network call may hang forever.
socket.setdefaulttimeout(30)


def _with_budget(fn, seconds, label):
    """Run fn() with a hard time budget; None if it can't finish in time.
    The satellite (ERDDAP) endpoints in particular can stall for minutes —
    the briefing must never be hostage to one slow feed."""
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(fn).result(timeout=seconds)
    except FutureTimeout:
        logger.warning(f'{label} exceeded {seconds}s budget — skipped')
        return None
    except Exception as e:
        logger.warning(f'{label} failed: {e}')
        return None
    finally:
        # wait=False: never block on a stuck feed thread — it finishes (or
        # dies with the process) in the background.
        ex.shutdown(wait=False)

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'wheelhouse.db')
LOGS_DIR = os.path.join(BASE, 'logs')
TO_EMAIL = os.environ.get('BRIEFING_EMAIL', 'mgiorgio@rednun.com')

OCTANTS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']


def deg_to_octant(deg):
    try:
        return OCTANTS[int((float(deg) + 22.5) % 360 // 45)]
    except (TypeError, ValueError):
        return None


def compass_to_octant(s):
    """Collapse NWS 16-point strings ('WSW', 'ENE') to 8 octants."""
    s = (s or '').strip().upper()
    if s in OCTANTS:
        return s
    m = {'NNE': 'NE', 'ENE': 'NE', 'ESE': 'SE', 'SSE': 'SE',
         'SSW': 'SW', 'WSW': 'SW', 'WNW': 'NW', 'NNW': 'NW'}
    return m.get(s)


def parse_mph(s):
    """NWS windSpeed like '10 mph' or '10 to 15 mph' -> max kt."""
    try:
        nums = [int(w) for w in str(s).replace('mph', '').replace('to', ' ').split()]
        return round(max(nums) * 0.869, 1) if nums else None
    except ValueError:
        return None


# ==================== GO / NO-GO ====================

EAST_OCTANTS = ('NE', 'E', 'SE')


def assess_window(wind_kt, octant, pressure_trend_mb, tide_direction):
    """Local-knowledge wind assessment for the morning window.
    Returns (verdict, [reasons]). Verdict: GO | CAUTION | NO-GO | UNKNOWN."""
    reasons = []
    if wind_kt is None or octant is None:
        return 'UNKNOWN', ['Wind forecast unavailable -- check NWS before leaving the dock.']

    verdict = 'GO'

    if octant in EAST_OCTANTS:
        if wind_kt >= 15:
            verdict = 'NO-GO'
            reasons.append(f'{octant} wind at {wind_kt:.0f}kt -- east wind means an approaching low: '
                           'sloppy Atlantic side, dirty water, and the bite historically shuts down.')
        else:
            verdict = 'CAUTION'
            reasons.append(f'{octant} wind ({wind_kt:.0f}kt). "Wind from the east, fish bite the least" -- '
                           'expect a slow bite; the Sound side will fish better than the backside.')
    elif wind_kt >= 20:
        verdict = 'NO-GO'
        reasons.append(f'{octant} wind at {wind_kt:.0f}kt -- small-craft conditions on the shoals.')
    elif wind_kt >= 15:
        verdict = 'CAUTION'
        reasons.append(f'{octant} wind at {wind_kt:.0f}kt -- fishable early but the rips will be rough; '
                       'pick protected water.')
    elif octant in ('S', 'SW') :
        reasons.append(f'Light {octant} at {wind_kt:.0f}kt -- prime Monomoy conditions. The SW builds '
                       'by afternoon, so get the morning tide.')
    else:
        reasons.append(f'{octant} at {wind_kt:.0f}kt -- workable. W/NW gives you a lee on the backside beaches.')

    # Wind against tide -- the rip-safety wildcard.
    if wind_kt >= 12 and tide_direction:
        reasons.append(f'At {wind_kt:.0f}kt, watch wind-against-tide in the rips: the {tide_direction} '
                       'current opposing this breeze stands waves straight up. Fish the stage where '
                       'wind and current run together.')

    # Barometer.
    if pressure_trend_mb is not None:
        if pressure_trend_mb <= -2:
            reasons.append(f'Pressure falling fast ({pressure_trend_mb:+.1f}mb/3h) -- front inbound. '
                           'Classic pre-front feed window, but weather is deteriorating: short trip.')
            if verdict == 'GO':
                verdict = 'CAUTION'
        elif pressure_trend_mb <= -1:
            reasons.append(f'Pressure falling ({pressure_trend_mb:+.1f}mb/3h) -- pre-front bite window.')
        elif pressure_trend_mb >= 2:
            reasons.append(f'Pressure rising hard ({pressure_trend_mb:+.1f}mb/3h) -- post-front. '
                           'Bite often lags a day; work bait schools and deeper edges.')
    return verdict, reasons


# ==================== PERSONAL WIND HISTORY ====================

def personal_wind_stats():
    """Join the captain's catch logs to conditions_log wind rows -> catches per octant.
    Returns display lines, or [] if too little data."""
    try:
        db = sqlite3.connect(DB_PATH, timeout=15)
        db.row_factory = sqlite3.Row
        wind_by_datehour = {}
        for r in db.execute('SELECT date, snapshot_hour, wind_direction, wind_speed_kt '
                            'FROM conditions_log WHERE wind_direction IS NOT NULL'):
            wind_by_datehour[(r['date'], int(r['snapshot_hour'] or 6))] = r['wind_direction']
        db.close()
    except Exception as e:
        logger.warning(f'wind history query failed: {e}')
        return []

    counts = {}
    matched = 0
    for fp in glob.glob(os.path.join(LOGS_DIR, 'catch_*.json')):
        try:
            with open(fp) as f:
                entry = json.load(f)
            dt = datetime.fromisoformat(entry.get('timestamp', ''))
        except (ValueError, TypeError, OSError, json.JSONDecodeError):
            continue
        # nearest logged hour for that date
        hours = [h for (d, h) in wind_by_datehour if d == dt.strftime('%Y-%m-%d')]
        if not hours:
            continue
        nearest = min(hours, key=lambda h: abs(h - dt.hour))
        octant = deg_to_octant(wind_by_datehour[(dt.strftime('%Y-%m-%d'), nearest)])
        if octant:
            counts[octant] = counts.get(octant, 0) + 1
            matched += 1

    if matched < 5:
        return []
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    lines = ['YOUR WIND TRACK RECORD (%d catches with wind data)' % matched]
    lines += ['  %-3s %s (%d)' % (o, '#' * n, n) for o, n in ranked]
    worst = [o for o in EAST_OCTANTS if counts.get(o, 0) == 0]
    if worst:
        lines.append('  Zero logged catches on %s wind days -- matches the local rule.' % '/'.join(worst))
    return lines


# ==================== ASSEMBLE ====================

def build_briefing():
    from fishing_intel import get_tides, get_buoy, get_weather, get_lunar, get_erddap_conditions

    today = datetime.now()
    L = []
    L.append('WHEELHOUSE MORNING BRIEFING -- %s' % today.strftime('%A, %B %d, %Y'))
    L.append('=' * 58)

    # --- Wind for the morning window (05:00-13:00) from NWS hourly ---
    wind_kt = octant = None
    wind_timeline = []
    logger.info('Fetching NWS wind forecast...')
    try:
        wx = _with_budget(get_weather, 45, 'NWS weather')
        for p in (wx or {}).get('hourly', [])[:9]:
            kt = parse_mph(p.get('windSpeed'))
            oc = compass_to_octant(p.get('windDirection'))
            t = p.get('startTime', '')[11:16]
            wind_timeline.append('  %s  %-3s %4skt  %s' % (
                t, oc or '?', ('%.0f' % kt) if kt else '?', p.get('shortForecast', '')[:34]))
            if kt is not None and (wind_kt is None or kt > wind_kt):
                wind_kt, octant = kt, oc or octant
            if octant is None:
                octant = oc
    except Exception as e:
        logger.warning(f'weather failed: {e}')

    # --- Buoy: pressure + temp ---
    pressure_mb = trend_mb = water_f = buoy_id = None
    logger.info('Fetching buoy...')
    try:
        buoy = _with_budget(get_buoy, 45, 'buoy')
        obs = (buoy or {}).get('observation') or {}
        if obs.get('PRES') not in (None, 'MM'):
            pressure_mb = float(obs['PRES'])
        if obs.get('WTMP') not in (None, 'MM'):
            water_f = round(float(obs['WTMP']) * 9 / 5 + 32, 1)
        trend_mb = (buoy or {}).get('pressure_trend_mb_3h')
        buoy_id = (buoy or {}).get('station')
    except Exception as e:
        logger.warning(f'buoy failed: {e}')

    # --- Tides today ---
    tide_lines, tide_direction = [], None
    logger.info('Fetching tides...')
    try:
        tides = _with_budget(lambda: get_tides('chatham'), 30, 'tides')
        now = datetime.now()
        nh = nl = None
        for p in (tides or {}).get('predictions', []):
            t = datetime.strptime(p['t'], '%Y-%m-%d %H:%M')
            if t.date() == now.date():
                tide_lines.append('  %s %s  %.1fft' % (
                    'HIGH' if p['type'] == 'H' else 'LOW ', t.strftime('%I:%M %p'), float(p['v'])))
            if t > now:
                if p['type'] == 'H' and nh is None:
                    nh = t
                if p['type'] == 'L' and nl is None:
                    nl = t
        if nh and nl:
            tide_direction = 'flooding' if nh < nl else 'ebbing'
    except Exception as e:
        logger.warning(f'tides failed: {e}')

    # --- Verdict ---
    verdict, reasons = assess_window(wind_kt, octant, trend_mb, tide_direction)
    L.append('')
    L.append('>>> %s <<<' % verdict)
    for r in reasons:
        L.append('  - %s' % r)

    L.append('')
    L.append('TIDES (Chatham)%s' % (('  -- currently ' + tide_direction) if tide_direction else ''))
    L.extend(tide_lines or ['  unavailable'])

    L.append('')
    L.append('CONDITIONS')
    if water_f:
        L.append('  Water: %.1fF (buoy %s)' % (water_f, buoy_id or '?'))
    if pressure_mb:
        L.append('  Pressure: %.1fmb, %s (%s/3h)' % (
            pressure_mb,
            'falling' if (trend_mb or 0) <= -1 else 'rising' if (trend_mb or 0) >= 1 else 'steady',
            ('%+.1fmb' % trend_mb) if trend_mb is not None else '?'))
    logger.info('Fetching satellite SST/clarity (slowest feed, 90s budget)...')
    try:
        erddap = _with_budget(get_erddap_conditions, 90, 'ERDDAP satellite')
        g = (erddap or {}).get('temp_gradient')
        if g:
            L.append('  Temp break: %s' % g['summary'])
        clarity = (erddap or {}).get('water_clarity') or {}
        for k in ('stonehorse', 'sound_side', 'east_atlantic'):
            if k in clarity:
                kd = clarity[k]['kd490']
                L.append('  Clarity (Kd490 %s): %.2f -- %s' % (
                    clarity[k]['name'], kd,
                    'clean' if kd < 0.15 else 'average' if kd < 0.3 else 'murky'))
                break
    except Exception as e:
        logger.warning(f'erddap failed: {e}')
    try:
        lunar = get_lunar()
        L.append('  Moon: %s (%d%%), solunar %s' % (
            lunar['phase_name'], lunar['illumination'], lunar['rating']))
    except Exception as e:
        logger.warning(f'lunar failed: {e}')

    if wind_timeline:
        L.append('')
        L.append('WIND, HOUR BY HOUR')
        L.extend(wind_timeline)

    stats = personal_wind_stats()
    if stats:
        L.append('')
        L.extend(stats)

    L.append('')
    L.append('Log every catch -- the pattern engine gets sharper with each one.')
    L.append('https://wheelhouse.rednun.com')
    return '\n'.join(L), verdict


def send_email(body, verdict):
    user = os.environ.get('GMAIL_ADDRESS', '')
    pw = os.environ.get('GMAIL_APP_PASSWORD', '')
    if not user or not pw:
        logger.error('Gmail creds not configured -- cannot send briefing')
        return False
    msg = MIMEText(body, 'plain')
    msg['Subject'] = 'Wheelhouse %s -- %s' % (verdict, datetime.now().strftime('%b %d'))
    msg['From'] = user
    msg['To'] = TO_EMAIL
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as smtp:
        smtp.login(user, pw)
        smtp.send_message(msg)
    logger.info(f'Briefing sent to {TO_EMAIL} ({verdict})')
    return True


if __name__ == '__main__':
    logger.info('Building briefing...')
    body, verdict = build_briefing()
    logger.info('Briefing assembled.')
    if '--print' in sys.argv:
        print(body)
    else:
        send_email(body, verdict)
        # Push/Telegram: verdict + first reason to everyone subscribed.
        try:
            from push_notify import notify_user, all_push_usernames
            first_reason = next((l.strip('- ').strip() for l in body.splitlines()
                                 if l.strip().startswith('-')), '')
            for u in all_push_usernames():
                notify_user(u, f'Wheelhouse {verdict} \u2014 {datetime.now().strftime("%b %d")}',
                            first_reason[:180], url='/')
            logger.info('Verdict pushed to subscribers')
        except Exception as e:
            logger.warning(f'briefing push skipped: {e}')
