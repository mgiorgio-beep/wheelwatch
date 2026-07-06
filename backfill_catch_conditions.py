#!/usr/bin/env python3
"""
Backfill canonical conditions onto historical catch logs.

Older catches were saved with empty or sparse conditions (e.g. {} or a single
human-readable water_temp string) because the catch loggers predated the shared
conditions.build_conditions_snapshot() schema. This script populates each such
catch from the nearest conditions_log snapshot (matched by date + the closest
snapshot_hour of 6 / 12 / 18 to the catch time) using the canonical numeric keys
that pattern_intel scores against.

Idempotent: catches already carrying a real catch-time snapshot, or already
tagged "conditions_source":"backfill", are left untouched. Safe to re-run.
"""

import os
import json
import glob
import sqlite3
from datetime import datetime

DB_PATH = '/opt/wheelhouse/wheelhouse.db'
LOGS_DIR = '/opt/wheelhouse/logs'
SNAPSHOT_HOURS = [6, 12, 18]

# Canonical numeric keys copied from a conditions_log row onto a catch.
CANONICAL_KEYS = [
    'tide_hours_to_next_high',
    'tide_direction',
    'tide_strength',
    'sst_gradient_f',
    'sst_trend',
    'water_temp_f',
    'moon_phase',
    'moon_illumination',
    'solunar_rating',
]

# Presence of any of these means the catch already has a genuine canonical
# snapshot — never overwrite it.
CANONICAL_MARKERS = {
    'tide_direction', 'tide_hours_to_next_high', 'sst_gradient_f', 'moon_illumination',
}


def _is_canonical(cond):
    return any(k in cond for k in CANONICAL_MARKERS)


def _nearest_snapshot(db, catch_date, catch_hour):
    """Return the conditions_log row for catch_date at the snapshot hour closest to
    catch_hour, falling back to the nearest available row by date."""
    closest = min(SNAPSHOT_HOURS, key=lambda h: abs(h - catch_hour))
    row = db.execute(
        'SELECT * FROM conditions_log WHERE date = ? AND snapshot_hour = ? '
        'ORDER BY logged_at DESC LIMIT 1',
        (catch_date, closest)).fetchone()
    if row:
        return row, closest
    # Fallback: any snapshot that day
    row = db.execute(
        'SELECT * FROM conditions_log WHERE date = ? ORDER BY snapshot_hour LIMIT 1',
        (catch_date,)).fetchone()
    if row:
        return row, row['snapshot_hour']
    # Last resort: nearest date overall
    row = db.execute(
        'SELECT * FROM conditions_log ORDER BY ABS(julianday(date) - julianday(?)) LIMIT 1',
        (catch_date,)).fetchone()
    return (row, row['snapshot_hour'] if row else None) if row else (None, None)


def main():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row

    files = sorted(glob.glob(os.path.join(LOGS_DIR, 'catch_*.json')))
    n_total = len(files)
    n_backfilled = 0
    n_already_canonical = 0
    n_already_tagged = 0
    n_no_match = 0

    for fp in files:
        try:
            with open(fp) as f:
                entry = json.load(f)
        except Exception as e:
            print(f'  ! skip {os.path.basename(fp)}: unreadable ({e})')
            continue

        cond = entry.get('conditions') or {}

        if entry.get('conditions_source') == 'backfill':
            n_already_tagged += 1
            continue
        if _is_canonical(cond):
            n_already_canonical += 1
            continue

        ts = entry.get('timestamp', '')
        try:
            dt = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            print(f'  ! skip {os.path.basename(fp)}: bad timestamp {ts!r}')
            n_no_match += 1
            continue

        catch_date = dt.strftime('%Y-%m-%d')
        row, used_hour = _nearest_snapshot(db, catch_date, dt.hour)
        if not row:
            print(f'  ! no conditions_log match for {os.path.basename(fp)} ({catch_date})')
            n_no_match += 1
            continue

        new_cond = {}
        for k in CANONICAL_KEYS:
            v = row[k] if k in row.keys() else None
            if v is not None and v != '':
                new_cond[k] = v

        # Carry the catch's own position if it has GPS.
        gps = entry.get('gps') or {}
        if gps.get('lat') is not None and gps.get('lon') is not None:
            new_cond['lat'] = gps['lat']
            new_cond['lon'] = gps['lon']

        entry['conditions'] = new_cond
        entry['conditions_source'] = 'backfill'

        with open(fp, 'w') as f:
            json.dump(entry, f, indent=2)

        n_backfilled += 1
        print(f'  + {os.path.basename(fp)} <- {catch_date} h{used_hour} '
              f'({len(new_cond)} keys)')

    db.close()

    print('\nBackfill summary')
    print(f'  catch files scanned : {n_total}')
    print(f'  backfilled          : {n_backfilled}')
    print(f'  already canonical   : {n_already_canonical}')
    print(f'  already tagged      : {n_already_tagged}')
    print(f'  no match / skipped  : {n_no_match}')


if __name__ == '__main__':
    main()
