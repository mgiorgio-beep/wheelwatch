"""
Seed historical SST gradient data from NASA MUR ERDDAP.
Uses bulk time-range queries (one per monitoring point) rather than
per-day fetches. Runs in ~2 minutes for 3 years of data.
"""

import sqlite3
import requests
import logging
import time
from datetime import datetime, timedelta

DB_PATH = '/opt/wheelhouse/wheelhouse.db'
ERDDAP_BASE = 'https://coastwatch.pfeg.noaa.gov/erddap/griddap'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger('sst-seed')

SOUND_LAT, SOUND_LON = 41.66, -70.03
ATLANTIC_LAT, ATLANTIC_LON = 41.55, -69.88


def to_fahrenheit(val):
    """Convert SST value to Fahrenheit. Handles both Kelvin and Celsius."""
    if val > 200:
        return round((val - 273.15) * 9/5 + 32, 1)
    return round(val * 9/5 + 32, 1)


def fetch_sst_timeseries(start_date, end_date, lat, lon):
    """
    Fetch daily SST for a date range at a single point.
    Returns dict of {date_str: avg_temp_value}
    Uses ERDDAP's time range query — one HTTP request for many days.
    """
    delta = 0.02  # tighter bbox for faster response
    url = (
        f'{ERDDAP_BASE}/jplMURSST41.json'
        f'?analysed_sst[({start_date}T09:00:00Z):1:({end_date}T09:00:00Z)]'
        f'[({lat-delta:.3f}):({lat+delta:.3f})]'
        f'[({lon-delta:.3f}):({lon+delta:.3f})]'
    )
    logger.info(f'Fetching SST {start_date} to {end_date} at ({lat},{lon})...')
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    data = r.json()
    rows = data['table']['rows']

    # Group values by date
    daily = {}
    for row in rows:
        time_str = row[0][:10]  # 'YYYY-MM-DD'
        val = row[-1]
        if val is not None and ((-2 < val < 40) or (250 < val < 320)):
            if time_str not in daily:
                daily[time_str] = []
            daily[time_str].append(val)

    # Average per day
    result = {}
    for date_str, vals in daily.items():
        result[date_str] = sum(vals) / len(vals)

    logger.info(f'  Got {len(result)} days of data')
    return result


def seed_historical_sst(days_back=365*3):
    """Seed historical SST gradient data using bulk queries."""
    db = sqlite3.connect(DB_PATH)

    existing = set(
        row[0] for row in
        db.execute("SELECT DISTINCT date FROM conditions_log WHERE sst_gradient_f IS NOT NULL").fetchall()
    )
    logger.info(f'Already have {len(existing)} dates with SST data')

    end_date = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')

    # Fetch in 30-day chunks to stay within ERDDAP timeout limits
    chunk_days = 30
    current_start = datetime.now() - timedelta(days=days_back)
    total_seeded = 0

    while current_start < datetime.now() - timedelta(days=2):
        chunk_end = min(current_start + timedelta(days=chunk_days),
                        datetime.now() - timedelta(days=2))
        s = current_start.strftime('%Y-%m-%d')
        e = chunk_end.strftime('%Y-%m-%d')

        try:
            sound_data = fetch_sst_timeseries(s, e, SOUND_LAT, SOUND_LON)
            time.sleep(1)
            atlantic_data = fetch_sst_timeseries(s, e, ATLANTIC_LAT, ATLANTIC_LON)
            time.sleep(1)

            seeded = 0
            for date_str in sound_data:
                if date_str in existing:
                    continue
                if date_str not in atlantic_data:
                    continue

                sound_f = to_fahrenheit(sound_data[date_str])
                atlantic_f = to_fahrenheit(atlantic_data[date_str])
                gradient = round(sound_f - atlantic_f, 1)

                db.execute('''
                    INSERT OR IGNORE INTO conditions_log
                    (date, snapshot_hour, sst_sound_side, sst_east_atlantic, sst_gradient_f, logged_at)
                    VALUES (?, 12, ?, ?, ?, ?)
                ''', (date_str, sound_f, atlantic_f, gradient, datetime.now().isoformat()))
                seeded += 1

            db.commit()
            total_seeded += seeded
            logger.info(f'Chunk {s} to {e}: {seeded} days seeded (total: {total_seeded})')

        except Exception as ex:
            logger.error(f'Chunk {s} to {e} failed: {ex}')

        current_start = chunk_end + timedelta(days=1)

    db.close()
    logger.info(f'SST seeding complete: {total_seeded} days added')


if __name__ == '__main__':
    logger.info('Starting historical SST seed...')
    seed_historical_sst(days_back=365*3)
    logger.info('Done. Pattern engine now has historical SST baseline.')
