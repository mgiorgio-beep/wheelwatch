"""
Seed the Wheelhouse database with MRIP historical catch rate data
for Massachusetts striped bass.

MRIP data source:
https://www.fisheries.noaa.gov/data-tools/recreational-fisheries-statistics-queries

Download steps (manual, one-time):
1. Go to the MRIP Query Tool URL above
2. Select: Species = Striped Bass, Region = Massachusetts,
   Year = 1990-2024, Mode = All, Area = All
3. Export as CSV
4. Place file at /opt/wheelhouse/data/mrip/mrip_striper_ma.csv
5. Run this script

The script extracts monthly catch rates and stores them as a
relative index in the mrip_baseline table for use by pattern_intel.py
"""

import sqlite3
import csv
import os
import logging
from collections import defaultdict

DB_PATH = '/opt/wheelhouse/wheelhouse.db'
MRIP_CSV = '/opt/wheelhouse/data/mrip/mrip_striper_ma.csv'

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('mrip-seed')


def init_mrip_table():
    db = sqlite3.connect(DB_PATH)
    db.execute('''CREATE TABLE IF NOT EXISTS mrip_baseline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        month INTEGER NOT NULL,
        wave INTEGER,
        avg_catch_rate REAL,
        relative_index REAL,
        years_of_data INTEGER,
        source TEXT DEFAULT 'mrip',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(month)
    )''')
    db.commit()
    db.close()


def seed_from_csv(csv_path):
    """Parse MRIP CSV and compute monthly relative indices."""
    if not os.path.exists(csv_path):
        logger.warning(f'MRIP CSV not found at {csv_path}')
        logger.info('Using hardcoded MRIP estimates — see pattern_intel.py')
        return False

    monthly_rates = defaultdict(list)

    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                wave = int(row.get('Wave', 0))
                catch = float(row.get('Harvest', 0) or 0)
                trips = float(row.get('Angler Trips', 1) or 1)
                if trips > 0:
                    rate = catch / trips
                    month = (wave - 1) * 2 + 1
                    if 1 <= month <= 12:
                        monthly_rates[month].append(rate)
            except Exception:
                continue

    if not monthly_rates:
        logger.warning('No data parsed from CSV')
        return False

    avg_by_month = {m: sum(rates)/len(rates) for m, rates in monthly_rates.items()}
    annual_avg = sum(avg_by_month.values()) / len(avg_by_month) if avg_by_month else 1

    db = sqlite3.connect(DB_PATH)
    for month, avg in avg_by_month.items():
        relative = round(avg / annual_avg, 2) if annual_avg > 0 else 0
        db.execute('''
            INSERT INTO mrip_baseline (month, avg_catch_rate, relative_index, years_of_data)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(month) DO UPDATE SET
                avg_catch_rate = excluded.avg_catch_rate,
                relative_index = excluded.relative_index,
                updated_at = CURRENT_TIMESTAMP
        ''', (month, round(avg, 4), relative, len(monthly_rates[month])))
    db.commit()
    db.close()

    logger.info(f'Seeded MRIP data for {len(avg_by_month)} months')
    return True


if __name__ == '__main__':
    init_mrip_table()
    if not seed_from_csv(MRIP_CSV):
        logger.info('MRIP CSV not available — hardcoded estimates active in pattern_intel.py')
        logger.info('To seed with real data: download CSV from MRIP Query Tool and re-run')
    logger.info('Done')
