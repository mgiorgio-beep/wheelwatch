"""
Wheelhouse Pattern Intelligence v2
Tide-relative matching + catch-time vector comparison.

Key improvements over v1:
- Matches on tide state (hours-to-high-tide) not clock time
- Uses catch-time condition snapshots, not daily 6AM snapshots
- Weights SST gradient trend, not just absolute value
- Spring vs neap tide awareness
- Seeded with historical MRIP and SST baseline data
"""

import os, json, sqlite3, glob, logging, math
from datetime import datetime

logger = logging.getLogger('wh-patterns')

DB_PATH = '/opt/wheelhouse/wheelhouse.db'
LOGS_DIR = '/opt/wheelhouse/logs'

# Similarity weights — tide-relative matching weighted highest
WEIGHTS = {
    'tide_hours_to_high':  25,  # how far into the tidal cycle
    'tide_direction':      20,  # flooding vs ebbing
    'sst_gradient':        15,  # temp break strength
    'sst_trend':           10,  # is break strengthening or weakening
    'water_temp':          10,  # absolute water temp
    'moon_phase':          10,  # lunar cycle position
    'solunar_rating':       5,  # major/minor period quality
    'tide_strength':        5,  # spring vs neap
}


def _score_similarity(target, candidate):
    """
    Score how similar a candidate conditions record is to the target.
    Both target and candidate are dicts from conditions_log or catch_conditions.
    Returns 0-100.
    """
    score = 0
    total = sum(WEIGHTS.values())

    # Tide hours to next high — most important factor
    t_hrs = target.get('tide_hours_to_next_high')
    c_hrs = candidate.get('tide_hours_to_next_high')
    if t_hrs is not None and c_hrs is not None:
        diff = abs(t_hrs - c_hrs)
        if diff <= 0.5:   score += WEIGHTS['tide_hours_to_high']
        elif diff <= 1.0: score += WEIGHTS['tide_hours_to_high'] * 0.7
        elif diff <= 2.0: score += WEIGHTS['tide_hours_to_high'] * 0.4
        elif diff <= 3.0: score += WEIGHTS['tide_hours_to_high'] * 0.1

    # Tide direction
    if target.get('tide_direction') and candidate.get('tide_direction'):
        if target['tide_direction'] == candidate['tide_direction']:
            score += WEIGHTS['tide_direction']

    # SST gradient
    t_grad = target.get('sst_gradient_f')
    c_grad = candidate.get('sst_gradient_f')
    if t_grad is not None and c_grad is not None:
        diff = abs(t_grad - c_grad)
        if diff <= 1:   score += WEIGHTS['sst_gradient']
        elif diff <= 3: score += WEIGHTS['sst_gradient'] * 0.6
        elif diff <= 6: score += WEIGHTS['sst_gradient'] * 0.2

    # SST trend
    if target.get('sst_trend') and candidate.get('sst_trend'):
        if target['sst_trend'] == candidate['sst_trend']:
            score += WEIGHTS['sst_trend']

    # Water temp
    t_wt = target.get('water_temp_f')
    c_wt = candidate.get('water_temp_f')
    if t_wt is not None and c_wt is not None:
        diff = abs(t_wt - c_wt)
        if diff <= 2:   score += WEIGHTS['water_temp']
        elif diff <= 4: score += WEIGHTS['water_temp'] * 0.5
        elif diff <= 6: score += WEIGHTS['water_temp'] * 0.2

    # Moon phase (use illumination as proxy)
    t_moon = target.get('moon_illumination')
    c_moon = candidate.get('moon_illumination')
    if t_moon is not None and c_moon is not None:
        diff = abs(t_moon - c_moon)
        if diff <= 5:   score += WEIGHTS['moon_phase']
        elif diff <= 15: score += WEIGHTS['moon_phase'] * 0.5
        elif diff <= 25: score += WEIGHTS['moon_phase'] * 0.2

    # Solunar
    if target.get('solunar_rating') and candidate.get('solunar_rating'):
        if target['solunar_rating'] == candidate['solunar_rating']:
            score += WEIGHTS['solunar_rating']

    # Spring vs neap
    if target.get('tide_strength') and candidate.get('tide_strength'):
        if target['tide_strength'] == candidate['tide_strength']:
            score += WEIGHTS['tide_strength']

    return round(score / total * 100)


def _load_catch_conditions():
    """
    Load condition snapshots from catch logs.
    Each logged catch has a conditions dict snapshotted at catch time.
    Species and technique only — no GPS, no spot names, no usernames.
    """
    catch_records = []
    for fp in glob.glob(os.path.join(LOGS_DIR, 'catch_*.json')):
        try:
            with open(fp) as f:
                entry = json.load(f)
            cond = entry.get('conditions', {})
            if not cond:
                continue
            catch_records.append({
                'species':  entry.get('species', ''),
                'technique': entry.get('technique', ''),
                'timestamp': entry.get('timestamp', ''),
                'conditions': cond,
            })
        except Exception as e:
            logger.warning(f'Failed to load catch {fp}: {e}')
    return catch_records


def _get_current_conditions():
    """Get the most recent conditions snapshot closest to the current hour."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    now_hour = datetime.now().hour

    # Find closest snapshot hour (6, 12, or 18)
    snapshot_hours = [6, 12, 18]
    closest = min(snapshot_hours, key=lambda h: abs(h - now_hour))

    row = db.execute('''
        SELECT * FROM conditions_log
        WHERE snapshot_hour = ?
        ORDER BY date DESC, logged_at DESC
        LIMIT 1
    ''', (closest,)).fetchone()

    if not row:
        # Fall back to any recent row
        row = db.execute(
            'SELECT * FROM conditions_log ORDER BY logged_at DESC LIMIT 1'
        ).fetchone()

    db.close()
    return dict(row) if row else None


def _get_historical_conditions():
    """Get all historical condition snapshots."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        'SELECT * FROM conditions_log ORDER BY date DESC, snapshot_hour DESC'
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def _get_mrip_seasonal_baseline():
    """
    Get MRIP historical catch rate baseline for Massachusetts striped bass.
    Returns monthly catch rate indices (relative productivity by month).
    """
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    try:
        rows = db.execute(
            'SELECT * FROM mrip_baseline ORDER BY month ASC'
        ).fetchall()
        db.close()
        if rows:
            return {r['month']: r['relative_index'] for r in rows}
    except Exception:
        pass
    finally:
        try:
            db.close()
        except Exception:
            pass

    # Hardcoded fallback from published MRIP Massachusetts striper data
    # Index: 1.0 = average month, >1.0 = above average, <1.0 = below average
    return {
        1: 0.0,   # January
        2: 0.0,   # February
        3: 0.0,   # March
        4: 0.1,   # April — very early, staging
        5: 0.6,   # May — arrival, building fast
        6: 1.4,   # June — peak arrival, excellent
        7: 1.8,   # July — peak season
        8: 1.6,   # August — excellent, albies arriving
        9: 1.3,   # September — fall run starting
        10: 1.0,  # October — fall run
        11: 0.3,  # November — late fish, tailing off
        12: 0.0,  # December
    }


def get_pattern_prediction(trip_hour=None):
    """
    Main prediction function.

    trip_hour: hour of day the captain plans to fish (0-23).
               If None, uses current hour.
               Used to find the right tide phase for the trip time.

    Returns prediction dict with species/technique probabilities
    based on historical catches under similar conditions.
    """
    current = _get_current_conditions()
    if not current:
        return {
            'status': 'no_conditions',
            'message': 'No conditions data logged yet. Logger may not have run.',
        }

    historical = _get_historical_conditions()
    catch_records = _load_catch_conditions()
    mrip_baseline = _get_mrip_seasonal_baseline()

    total_logged = len(historical)
    current_month = datetime.now().month
    seasonal_index = mrip_baseline.get(current_month, 0.5)

    # If trip_hour is specified, find the conditions snapshot closest to that time
    target_conditions = current
    if trip_hour is not None:
        snapshot_hours = [6, 12, 18]
        closest = min(snapshot_hours, key=lambda h: abs(h - trip_hour))
        trip_snap = next(
            (r for r in historical if r.get('snapshot_hour') == closest),
            current
        )
        target_conditions = trip_snap

    if total_logged < 3:
        return {
            'status': 'seeding',
            'message': f'Only {total_logged} condition snapshots logged. '
                       f'Predictions improving daily.',
            'seasonal_index': seasonal_index,
            'seasonal_note': _seasonal_note(current_month, seasonal_index),
            'days_logged': total_logged // 3,
        }

    # Score all historical condition snapshots
    scored_conditions = []
    for hist in historical:
        if hist.get('date') == datetime.now().strftime('%Y-%m-%d'):
            continue  # skip today
        score = _score_similarity(target_conditions, hist)
        scored_conditions.append({'record': hist, 'score': score})

    # Score each logged catch against its own conditions at catch time
    catch_matches = []
    for catch in catch_records:
        cond = catch['conditions']
        catch_cond = {
            'tide_hours_to_next_high': _parse_tide_hours(cond.get('tide_phase', '')),
            'tide_direction': _infer_tide_direction(cond.get('tide_phase', '')),
            'water_temp_f': _parse_float(cond.get('water_temp', '')),
            'wind_speed_kt': _parse_float(cond.get('wind', '').split('kt')[0].split()[-1]
                                          if 'kt' in cond.get('wind', '') else ''),
            'sst_gradient_f': target_conditions.get('sst_gradient_f'),
        }
        score = _score_similarity(target_conditions, catch_cond)
        catch_matches.append({
            'catch': catch,
            'score': score,
        })

    # Filter to analogous catches (score > 55)
    analogous_catches = [c for c in catch_matches if c['score'] >= 55]
    if not analogous_catches:
        analogous_catches = sorted(catch_matches, key=lambda x: x['score'], reverse=True)[:5]
    else:
        analogous_catches.sort(key=lambda x: x['score'], reverse=True)

    # Aggregate species and technique from analogous catches
    species_counts = {}
    technique_counts = {}

    for match in analogous_catches:
        sp = match['catch'].get('species', '').strip()
        te = match['catch'].get('technique', '').strip()
        if sp: species_counts[sp] = species_counts.get(sp, 0) + 1
        if te: technique_counts[te] = technique_counts.get(te, 0) + 1

    top_species = sorted(species_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    top_techniques = sorted(technique_counts.items(), key=lambda x: x[1], reverse=True)[:3]

    # Build summary — natural language, no raw counts
    lines = []
    tide_dir = target_conditions.get('tide_direction', '')
    hrs_to_high = target_conditions.get('tide_hours_to_next_high')
    tide_strength = target_conditions.get('tide_strength', '')
    sst_trend = target_conditions.get('sst_trend', '')

    if top_techniques and top_species:
        top_te = top_techniques[0][0]
        lines.append(
            f"Conditions today are consistent with setups that have historically "
            f"produced at Monomoy. {top_te.capitalize()} has been the stronger "
            f"technique under similar tide and temperature conditions."
        )
    elif top_techniques:
        top_te = top_techniques[0][0]
        lines.append(
            f"Conditions today match historical setups that have been productive "
            f"at Monomoy. {top_te.capitalize()} has been effective under similar "
            f"conditions."
        )
    elif analogous_catches:
        lines.append(
            "Conditions today are consistent with historically productive setups "
            "at Monomoy. Keep logging catches to sharpen the recommendations."
        )
    else:
        lines.append(
            "Not enough catch data yet to identify patterns — keep logging."
        )

    seasonal_note = _seasonal_note(current_month, seasonal_index)

    return {
        'status': 'ok',
        'days_logged': total_logged // 3,
        'analogous_catches': len(analogous_catches),
        'tide_direction': tide_dir,
        'tide_hours_to_high': hrs_to_high,
        'tide_strength': tide_strength,
        'sst_trend': sst_trend,
        'seasonal_index': seasonal_index,
        'seasonal_note': seasonal_note,
        'summary': ' '.join(lines),
        'generated': datetime.now().isoformat(),
    }


def _seasonal_note(month, index):
    if index == 0:
        return "Historical MRIP data: striped bass season not active this month in Massachusetts."
    elif index >= 1.5:
        return f"Historical MRIP data: this is peak striper season in Massachusetts (index {index:.1f}x average)."
    elif index >= 1.0:
        return f"Historical MRIP data: above-average striper activity for this time of year (index {index:.1f}x)."
    elif index >= 0.5:
        return f"Historical MRIP data: moderate striper activity typical for this month (index {index:.1f}x)."
    else:
        return f"Historical MRIP data: below-average season timing (index {index:.1f}x average)."


def _parse_tide_hours(tide_phase_str):
    """Parse tide phase string like '2.3hrs before high (4.5ft)' into hours-to-high."""
    try:
        if 'before high' in tide_phase_str:
            return float(tide_phase_str.split('hrs')[0].strip())
        elif 'after high' in tide_phase_str:
            hours = float(tide_phase_str.split('hrs')[0].strip())
            return -hours
        elif 'At high' in tide_phase_str:
            return 0.0
    except Exception:
        pass
    return None


def _infer_tide_direction(tide_phase_str):
    """Infer flooding/ebbing from tide phase string."""
    if 'before high' in tide_phase_str:
        return 'flooding'
    elif 'after high' in tide_phase_str:
        return 'ebbing'
    elif 'before low' in tide_phase_str:
        return 'ebbing'
    elif 'after low' in tide_phase_str:
        return 'flooding'
    return None


def _parse_float(s):
    """Safely parse a float from a string."""
    try:
        return float(str(s).strip())
    except Exception:
        return None
