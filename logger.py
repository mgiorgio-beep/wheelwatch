"""
Wheelhouse Conditions Logger — runs 3x daily via cron (6 AM, noon, 6 PM).
Snapshots SST, chlorophyll, tides (tide-relative), buoy, solunar, and weather to SQLite.
"""

import os, sys, sqlite3, logging, math
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
load_dotenv('/opt/rednun/.env', override=False)
sys.path.insert(0, '/opt/wheelhouse')

from fishing_intel import get_erddap_conditions, get_buoy, get_tides, get_weather, get_lunar

DB_PATH = '/opt/wheelhouse/wheelhouse.db'
os.makedirs('/opt/wheelhouse/logs', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('/opt/wheelhouse/logs/logger.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('wh-logger')


def init_table():
    db = sqlite3.connect(DB_PATH)
    db.execute('''CREATE TABLE IF NOT EXISTS conditions_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sst_sound_side REAL, sst_monomoy_tip REAL, sst_stonehorse REAL,
        sst_east_atlantic REAL, sst_offshore REAL, sst_gradient_f REAL,
        chl_sound_side REAL, chl_stonehorse REAL, chl_east_atlantic REAL,
        chl_source TEXT,
        water_temp_f REAL, wave_height_ft REAL, wave_period_s REAL,
        wave_direction INTEGER, wind_speed_kt REAL, wind_direction INTEGER,
        tide_phase TEXT, tide_height_ft REAL, next_high_ft REAL, next_high_hours REAL,
        moon_phase TEXT, moon_illumination INTEGER, solunar_rating TEXT,
        major_period_1 TEXT, major_period_2 TEXT,
        air_temp_f REAL, wind_speed_nws TEXT, wind_dir_nws TEXT, forecast_short TEXT
    )''')
    # Add tide-relative and trend columns (safe if already exist)
    for col in [
        "ALTER TABLE conditions_log ADD COLUMN snapshot_hour INTEGER DEFAULT 6",
        "ALTER TABLE conditions_log ADD COLUMN tide_hours_to_next_high REAL",
        "ALTER TABLE conditions_log ADD COLUMN tide_hours_since_last_high REAL",
        "ALTER TABLE conditions_log ADD COLUMN tide_direction TEXT",
        "ALTER TABLE conditions_log ADD COLUMN tide_strength TEXT",
        "ALTER TABLE conditions_log ADD COLUMN sst_trend TEXT",
        "ALTER TABLE conditions_log ADD COLUMN chl_trend TEXT",
    ]:
        try:
            db.execute(col)
        except Exception:
            pass
    db.commit()
    db.close()


def snapshot():
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    row = {'date': today, 'snapshot_hour': now.hour}

    # ERDDAP
    try:
        erddap = get_erddap_conditions()
        sst = erddap.get('sst', {})
        chl = erddap.get('chlorophyll', {})
        gradient = erddap.get('temp_gradient')
        for key in ('sound_side', 'monomoy_tip', 'stonehorse', 'east_atlantic', 'offshore'):
            row[f'sst_{key}'] = sst.get(key, {}).get('temp_f')
        row['sst_gradient_f'] = gradient['difference_f'] if gradient else None
        for key in ('sound_side', 'stonehorse', 'east_atlantic'):
            row[f'chl_{key}'] = chl.get(key, {}).get('chlor_a')
        sources = [chl[k]['source'] for k in chl]
        row['chl_source'] = sources[0] if sources else None
        logger.info(f'ERDDAP: gradient={row.get("sst_gradient_f")}F')
    except Exception as e:
        logger.error(f'ERDDAP failed: {e}')

    # SST trend — compare to previous snapshot at same hour
    try:
        db_check = sqlite3.connect(DB_PATH)
        db_check.row_factory = sqlite3.Row
        yesterday = db_check.execute('''
            SELECT sst_gradient_f FROM conditions_log
            WHERE snapshot_hour = ?
            ORDER BY date DESC LIMIT 1
        ''', (row.get('snapshot_hour', 6),)).fetchone()
        db_check.close()

        today_grad = row.get('sst_gradient_f')
        if yesterday and yesterday['sst_gradient_f'] and today_grad:
            diff = today_grad - yesterday['sst_gradient_f']
            if diff > 0.5:
                row['sst_trend'] = 'strengthening'
            elif diff < -0.5:
                row['sst_trend'] = 'weakening'
            else:
                row['sst_trend'] = 'stable'
    except Exception as e:
        logger.error(f'SST trend failed: {e}')

    # Chlorophyll trend
    try:
        db_check = sqlite3.connect(DB_PATH)
        db_check.row_factory = sqlite3.Row
        yesterday_chl = db_check.execute('''
            SELECT chl_stonehorse FROM conditions_log
            WHERE snapshot_hour = ?
            ORDER BY date DESC LIMIT 1
        ''', (row.get('snapshot_hour', 6),)).fetchone()
        db_check.close()

        today_chl = row.get('chl_stonehorse')
        if yesterday_chl and yesterday_chl['chl_stonehorse'] and today_chl:
            diff = today_chl - yesterday_chl['chl_stonehorse']
            if diff > 0.1:
                row['chl_trend'] = 'building'
            elif diff < -0.1:
                row['chl_trend'] = 'falling'
            else:
                row['chl_trend'] = 'stable'
    except Exception as e:
        logger.error(f'Chlorophyll trend failed: {e}')

    # Buoy
    try:
        buoy = get_buoy()
        if buoy and buoy.get('observation'):
            obs = buoy['observation']
            def sf(v): return float(v) if v and v != 'MM' else None
            wtmp = sf(obs.get('WTMP'))
            row['water_temp_f'] = round(wtmp * 9/5 + 32, 1) if wtmp else None
            row['wave_height_ft'] = sf(obs.get('WVHT'))
            row['wave_period_s'] = sf(obs.get('DPD'))
            mwd = sf(obs.get('MWD'))
            row['wave_direction'] = int(mwd) if mwd else None
            wspd = sf(obs.get('WSPD'))
            row['wind_speed_kt'] = round(wspd * 1.944, 1) if wspd else None
            wdir = sf(obs.get('WDIR'))
            row['wind_direction'] = int(wdir) if wdir else None
        logger.info(f'Buoy: water={row.get("water_temp_f")}F waves={row.get("wave_height_ft")}ft')
    except Exception as e:
        logger.error(f'Buoy failed: {e}')

    # Tides — compute tide-relative fields
    try:
        tides = get_tides('chatham')
        if tides and tides.get('predictions'):
            preds = tides['predictions']

            prev_high = None
            next_high = None
            prev_low = None
            next_low = None

            for p in preds:
                t = datetime.strptime(p['t'], '%Y-%m-%d %H:%M')
                diff_hr = (t - now).total_seconds() / 3600
                hilo = p['type']
                val = float(p['v'])

                if hilo == 'H':
                    if diff_hr < 0 and (prev_high is None or diff_hr > prev_high[0]):
                        prev_high = (diff_hr, val, t)
                    if diff_hr > 0 and (next_high is None or diff_hr < next_high[0]):
                        next_high = (diff_hr, val, t)
                else:
                    if diff_hr < 0 and (prev_low is None or diff_hr > prev_low[0]):
                        prev_low = (diff_hr, val, t)
                    if diff_hr > 0 and (next_low is None or diff_hr < next_low[0]):
                        next_low = (diff_hr, val, t)

            if next_high:
                row['tide_hours_to_next_high'] = round(next_high[0], 2)
                row['next_high_ft'] = next_high[1]
            if prev_high:
                row['tide_hours_since_last_high'] = round(abs(prev_high[0]), 2)

            # Direction: flooding if next high is sooner than next low
            if next_high and next_low:
                row['tide_direction'] = 'flooding' if next_high[0] < next_low[0] else 'ebbing'

            # Tide height at snapshot — sine interpolation
            if prev_high and next_high:
                cycle_hrs = next_high[0] - prev_high[0]
                elapsed = abs(prev_high[0])
                if cycle_hrs > 0:
                    phase = math.pi * elapsed / cycle_hrs
                    approx_ht = prev_high[1] + (next_high[1] - prev_high[1]) * (1 - math.cos(phase)) / 2
                    row['tide_height_ft'] = round(approx_ht, 1)

            # Tide phase label (keep for backward compat)
            nearest = None
            for p in preds:
                t = datetime.strptime(p['t'], '%Y-%m-%d %H:%M')
                diff_hr = (t - now).total_seconds() / 3600
                if nearest is None or abs(diff_hr) < abs(nearest[0]):
                    nearest = (diff_hr, p['type'], float(p['v']))
            if nearest:
                diff_hr, hilo, val = nearest
                if abs(diff_hr) < 0.5:
                    row['tide_phase'] = 'high' if hilo == 'H' else 'low'
                elif diff_hr > 0:
                    row['tide_phase'] = 'rising' if hilo == 'H' else 'falling'
                else:
                    row['tide_phase'] = 'falling' if hilo == 'H' else 'rising'

            # Spring vs neap: spring tides near new/full moon
            try:
                lunar = get_lunar()
                moon_illum = lunar['illumination']
                row['tide_strength'] = 'spring' if (moon_illum < 15 or moon_illum > 85) else 'neap'
            except Exception:
                pass

            logger.info(f'Tides: {row.get("tide_direction")} {row.get("tide_hours_to_next_high")}hrs to high')
    except Exception as e:
        logger.error(f'Tides failed: {e}')

    # Solunar
    try:
        lunar = get_lunar()
        row['moon_phase'] = lunar['phase_name']
        row['moon_illumination'] = lunar['illumination']
        row['solunar_rating'] = lunar['rating']
        row['major_period_1'] = lunar['major_periods'][0]
        row['major_period_2'] = lunar['major_periods'][1]
        logger.info(f'Solunar: {lunar["phase_name"]} {lunar["rating"]}')
    except Exception as e:
        logger.error(f'Solunar failed: {e}')

    # Weather
    try:
        weather = get_weather()
        if weather and weather.get('hourly'):
            h = next((x for x in weather['hourly']
                      if 'T12:' in x.get('startTime','') or 'T11:' in x.get('startTime','')),
                     weather['hourly'][0])
            row['air_temp_f'] = h.get('temperature')
            row['wind_speed_nws'] = h.get('windSpeed')
            row['wind_dir_nws'] = h.get('windDirection')
            row['forecast_short'] = h.get('shortForecast', '')[:80]
        logger.info(f'Weather: {row.get("air_temp_f")}F')
    except Exception as e:
        logger.error(f'Weather failed: {e}')

    # Write to DB
    db = sqlite3.connect(DB_PATH)
    cols = ', '.join(row.keys())
    placeholders = ', '.join(['?' for _ in row])
    db.execute(f'INSERT INTO conditions_log ({cols}) VALUES ({placeholders})', list(row.values()))
    db.commit()
    db.close()
    logger.info(f'Snapshot saved for {today} hour={row["snapshot_hour"]}')
    return row


if __name__ == '__main__':
    logger.info('Starting conditions snapshot...')
    init_table()
    snapshot()
    logger.info('Done.')
