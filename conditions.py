"""
Shared catch-conditions snapshot.

ONE canonical numeric schema, used by every catch logger (captain_advisor,
photo_catch, SMS webhook) and matched directly by pattern_intel. This replaces
the three divergent, lossy snapshots that previously existed (human-readable
strings in captain_advisor, water_temp-only in photo_catch, string re-parsing
in pattern_intel).

Reuses the same fishing_intel data sources and tide/sst/moon/solunar math as
logger.py (which writes the daily conditions_log rows), so a catch snapshot is
directly comparable to a logged conditions row.

Canonical keys (any may be absent if its source fails — missing is never an
error and is skipped, never penalized, by the matcher):
    tide_hours_to_next_high  float
    tide_direction           str   flooding | ebbing
    tide_strength            str   spring | neap
    sst_gradient_f           float
    sst_trend                str   strengthening | weakening | stable
    water_temp_f             float   <-- the ONE canonical temp key
    pressure_mb              float
    pressure_trend           str   rising | falling | steady (3h barometric)
    buoy_id                  str   which NDBC buoy supplied temp/pressure
    kd490                    float water clarity (higher = murkier)
    moon_phase               str
    moon_illumination        int
    solunar_rating           str
    depth_ft                 float (nullable)
    lat, lon                 float (nullable)
"""

import sqlite3
import logging
import math
from datetime import datetime, timedelta

logger = logging.getLogger('wh-conditions')

DB_PATH = '/opt/wheelhouse/wheelhouse.db'


def _latest_sst_trend():
    """Most recent computed SST trend from conditions_log (logger computes it 3x/day)."""
    try:
        db = sqlite3.connect(DB_PATH, timeout=15)
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT sst_trend FROM conditions_log "
            "WHERE sst_trend IS NOT NULL ORDER BY date DESC, snapshot_hour DESC LIMIT 1"
        ).fetchone()
        db.close()
        return row['sst_trend'] if row else None
    except Exception:
        return None


def _nearest_logged_conditions(at, max_hours=6):
    """Nearest conditions_log row to `at`, within max_hours. Used for backdated
    catches (photo EXIF time) where live buoy/satellite reads would be wrong.
    Returns a sqlite3.Row or None."""
    try:
        db = sqlite3.connect(DB_PATH, timeout=15)
        db.row_factory = sqlite3.Row
        days = [(at + timedelta(days=d)).strftime('%Y-%m-%d') for d in (-1, 0, 1)]
        rows = db.execute(
            "SELECT * FROM conditions_log WHERE date IN (?, ?, ?)", days).fetchall()
        db.close()
        best, best_diff = None, None
        for r in rows:
            try:
                row_dt = datetime.strptime(r['date'], '%Y-%m-%d').replace(
                    hour=int(r['snapshot_hour'] if r['snapshot_hour'] is not None else 6))
            except (TypeError, ValueError):
                continue
            diff = abs((row_dt - at).total_seconds()) / 3600
            if best_diff is None or diff < best_diff:
                best, best_diff = r, diff
        if best is not None and best_diff <= max_hours:
            return best
    except Exception as e:
        logger.debug(f'nearest logged conditions lookup failed: {e}')
    return None


def _row_get(row, key):
    """sqlite3.Row.get() — None when the column doesn't exist (old DBs)."""
    try:
        return row[key] if row is not None and key in row.keys() else None
    except Exception:
        return None


def build_conditions_snapshot(lat=None, lon=None, depth_ft=None, water_temp_f=None,
                              at=None):
    """
    Build the canonical catch-time conditions snapshot.

    Optional overrides (e.g. from a Garmin instrument photo) take precedence over
    computed values:
        lat, lon        — catch position (also stored on the snapshot)
        depth_ft        — measured depth
        water_temp_f    — measured surface/water temp, overrides the buoy value
        at              — datetime of the catch (e.g. photo EXIF time). Tide and
                          moon fields are computed for this moment. If `at` is more
                          than 3h from now, water temp / SST gradient / SST trend come
                          from the nearest conditions_log snapshot (within 6h) instead
                          of live feeds; omitted if no snapshot is near enough.

    Returns a dict with only the keys that could be resolved. Never raises.
    """
    from fishing_intel import get_erddap_conditions, get_buoy, get_tides, get_lunar

    now = at or datetime.now()
    # Backdated catch? Live buoy/satellite values would be wrong; use the
    # nearest logged snapshot instead (or omit those fields entirely).
    historical = at is not None and abs((datetime.now() - at).total_seconds()) > 3 * 3600
    hist_row = _nearest_logged_conditions(at) if historical else None
    cond = {}

    # --- Position / instrument-supplied fields ---
    if lat is not None:
        cond['lat'] = lat
    if lon is not None:
        cond['lon'] = lon
    if depth_ft is not None:
        cond['depth_ft'] = depth_ft

    # --- SST gradient (temp break strength) ---
    try:
        if historical:
            if _row_get(hist_row, 'sst_gradient_f') is not None:
                cond['sst_gradient_f'] = hist_row['sst_gradient_f']
            if _row_get(hist_row, 'kd490') is not None:
                cond['kd490'] = hist_row['kd490']
        else:
            erddap = get_erddap_conditions()
            gradient = (erddap or {}).get('temp_gradient')
            if gradient and gradient.get('difference_f') is not None:
                cond['sst_gradient_f'] = gradient['difference_f']
            clarity = (erddap or {}).get('water_clarity') or {}
            for key in ('stonehorse', 'sound_side', 'east_atlantic'):
                if key in clarity and clarity[key].get('kd490') is not None:
                    cond['kd490'] = clarity[key]['kd490']
                    break
    except Exception as e:
        logger.debug(f'conditions: SST gradient skipped: {e}')

    # --- SST trend (needs history; reuse logger's daily computation) ---
    try:
        trend = _row_get(hist_row, 'sst_trend') if historical else _latest_sst_trend()
        if trend:
            cond['sst_trend'] = trend
    except Exception as e:
        logger.debug(f'conditions: SST trend skipped: {e}')

    # --- Water temp from buoy (override wins) ---
    try:
        if historical:
            if _row_get(hist_row, 'water_temp_f') is not None and water_temp_f is None:
                cond['water_temp_f'] = round(float(hist_row['water_temp_f']), 1)
            for k in ('pressure_mb', 'pressure_trend', 'buoy_id'):
                if _row_get(hist_row, k) is not None:
                    cond[k] = hist_row[k]
        else:
            buoy = get_buoy()
            obs = (buoy or {}).get('observation') or {}
            wtmp = obs.get('WTMP')
            if wtmp and wtmp != 'MM' and water_temp_f is None:
                cond['water_temp_f'] = round(float(wtmp) * 9 / 5 + 32, 1)
            pres = obs.get('PRES')
            if pres and pres != 'MM':
                cond['pressure_mb'] = round(float(pres), 1)
            if (buoy or {}).get('pressure_trend'):
                cond['pressure_trend'] = buoy['pressure_trend']
            if (buoy or {}).get('station'):
                cond['buoy_id'] = buoy['station']
        if water_temp_f is not None:
            # Garmin/instrument override always wins for temp.
            cond['water_temp_f'] = round(float(water_temp_f), 1)
    except Exception as e:
        logger.debug(f'conditions: water temp skipped: {e}')

    # --- Tide-relative fields (same math as logger.snapshot) ---
    try:
        if at is not None and at.date() != datetime.now().date():
            tides = get_tides('chatham', begin=at.strftime('%Y%m%d'))
        else:
            tides = get_tides('chatham')
        preds = (tides or {}).get('predictions') or []
        prev_high = next_high = next_low = None
        for p in preds:
            t = datetime.strptime(p['t'], '%Y-%m-%d %H:%M')
            diff_hr = (t - now).total_seconds() / 3600
            if p['type'] == 'H':
                if diff_hr < 0 and (prev_high is None or diff_hr > prev_high[0]):
                    prev_high = (diff_hr, float(p['v']), t)
                if diff_hr > 0 and (next_high is None or diff_hr < next_high[0]):
                    next_high = (diff_hr, float(p['v']), t)
            else:
                if diff_hr > 0 and (next_low is None or diff_hr < next_low[0]):
                    next_low = (diff_hr, float(p['v']), t)

        if next_high:
            cond['tide_hours_to_next_high'] = round(next_high[0], 2)
        # Direction: flooding if next high arrives before next low
        if next_high and next_low:
            cond['tide_direction'] = 'flooding' if next_high[0] < next_low[0] else 'ebbing'

        # Spring vs neap from moon illumination
        try:
            lunar = get_lunar(at=now)
            illum = lunar['illumination']
            cond['tide_strength'] = 'spring' if (illum < 15 or illum > 85) else 'neap'
        except Exception:
            pass
    except Exception as e:
        logger.debug(f'conditions: tides skipped: {e}')

    # --- Solunar / moon ---
    try:
        lunar = get_lunar(at=now)
        cond['moon_phase'] = lunar['phase_name']
        cond['moon_illumination'] = lunar['illumination']
        cond['solunar_rating'] = lunar['rating']
    except Exception as e:
        logger.debug(f'conditions: solunar skipped: {e}')

    return cond
