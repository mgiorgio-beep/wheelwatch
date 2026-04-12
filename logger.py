"""
Wheelhouse Conditions Logger — runs daily via cron at 6 AM.
Snapshots SST, chlorophyll, tides, buoy, solunar, and weather to SQLite.
"""

import os, sys, sqlite3, logging
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
    db.commit()
    db.close()


def snapshot():
    today = datetime.now().strftime('%Y-%m-%d')
    row = {'date': today}

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

    # Tides
    try:
        tides = get_tides('chatham')
        if tides and tides.get('predictions'):
            now = datetime.now()
            nearest = None
            next_high = None
            for p in tides['predictions']:
                t = datetime.strptime(p['t'], '%Y-%m-%d %H:%M')
                diff_hr = (t - now).total_seconds() / 3600
                if nearest is None or abs(diff_hr) < abs(nearest[0]):
                    nearest = (diff_hr, p['type'], float(p['v']))
                if p['type'] == 'H' and diff_hr > 0 and (next_high is None or diff_hr < next_high[0]):
                    next_high = (diff_hr, float(p['v']))
            if nearest:
                diff_hr, hilo, val = nearest
                if abs(diff_hr) < 0.5:
                    row['tide_phase'] = 'high' if hilo == 'H' else 'low'
                elif diff_hr > 0:
                    row['tide_phase'] = 'rising' if hilo == 'H' else 'falling'
                else:
                    row['tide_phase'] = 'falling' if hilo == 'H' else 'rising'
                row['tide_height_ft'] = val
            if next_high:
                row['next_high_hours'] = round(next_high[0], 2)
                row['next_high_ft'] = next_high[1]
        logger.info(f'Tides: {row.get("tide_phase")}')
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
    logger.info(f'Snapshot saved for {today}')
    return row


if __name__ == '__main__':
    logger.info('Starting conditions snapshot...')
    init_table()
    snapshot()
    logger.info('Done.')
