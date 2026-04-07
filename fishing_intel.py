"""
Fishing Intelligence Module for Red Nun Dashboard
Proxies NOAA tides/currents, NWS weather, and provides SST/chlorophyll data sources.
"""

import requests
import logging
import json
import math
from datetime import datetime, timedelta
from functools import lru_cache
import time

logger = logging.getLogger('fishing_intel')

# --- Station Config ---
STATIONS = {
    'tides': {
        'chatham': {'id': '8447435', 'name': 'Chatham, Lydia Cove'},
        'stage_harbor': {'id': '8447270', 'name': 'Stage Harbor'},
    },
    'currents': {
        'pollock_rip': {'id': 'ACT1616', 'name': 'Pollock Rip Channel (Butler Hole)'},
        'chatham_roads': {'id': 'ACT1611', 'name': 'Chatham Roads'},
        'monomoy_point': {'id': 'ACT1626', 'name': 'Monomoy Point (West of)'},
    }
}

CHATHAM_LAT = 41.6723
CHATHAM_LON = -69.9597

NOAA_TIDE_BASE = 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
NWS_BASE = 'https://api.weather.gov'

# --- Cache with TTL ---
_cache = {}
CACHE_TTL = {
    'tides': 3600,       # 1 hour
    'currents': 3600,
    'weather': 1800,     # 30 min
    'marine': 1800,
    'buoy': 900,         # 15 min
}

def _cached(key, ttl_key, fetcher):
    now = time.time()
    if key in _cache:
        val, ts = _cache[key]
        if now - ts < CACHE_TTL.get(ttl_key, 1800):
            return val
    try:
        val = fetcher()
        _cache[key] = (val, now)
        return val
    except Exception as e:
        logger.error(f'Fetch error for {key}: {e}')
        if key in _cache:
            return _cache[key][0]
        return None


# ==================== TIDES ====================

def get_tides(station_key='chatham', hours=48):
    """Get hi/lo tide predictions from NOAA."""
    def fetch():
        station = STATIONS['tides'].get(station_key, STATIONS['tides']['chatham'])
        now = datetime.now()
        begin = now.strftime('%Y%m%d')
        end = (now + timedelta(hours=hours)).strftime('%Y%m%d')
        params = {
            'begin_date': begin,
            'end_date': end,
            'station': station['id'],
            'product': 'predictions',
            'datum': 'MLLW',
            'time_zone': 'lst_ldt',
            'units': 'english',
            'interval': 'hilo',
            'format': 'json',
        }
        r = requests.get(NOAA_TIDE_BASE, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        preds = data.get('predictions', [])
        return {
            'station': station,
            'predictions': preds,
            'fetched': datetime.now().isoformat(),
        }
    return _cached(f'tides_{station_key}', 'tides', fetch)


def get_tide_hourly(station_key='chatham', hours=48):
    """Get hourly tide height predictions for charting."""
    def fetch():
        station = STATIONS['tides'].get(station_key, STATIONS['tides']['chatham'])
        now = datetime.now()
        begin = now.strftime('%Y%m%d %H:%M')
        end = (now + timedelta(hours=hours)).strftime('%Y%m%d %H:%M')
        params = {
            'begin_date': begin,
            'end_date': end,
            'station': station['id'],
            'product': 'predictions',
            'datum': 'MLLW',
            'time_zone': 'lst_ldt',
            'units': 'english',
            'interval': '6',  # every 6 minutes
            'format': 'json',
        }
        r = requests.get(NOAA_TIDE_BASE, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        preds = data.get('predictions', [])
        return {
            'station': station,
            'predictions': preds,
            'fetched': datetime.now().isoformat(),
        }
    return _cached(f'tide_hourly_{station_key}', 'tides', fetch)


# ==================== CURRENTS ====================

def get_currents(station_key='pollock_rip', hours=48):
    """Get current predictions (max flood/ebb/slack) from NOAA."""
    def fetch():
        station = STATIONS['currents'].get(station_key, STATIONS['currents']['pollock_rip'])
        now = datetime.now()
        begin = now.strftime('%Y%m%d')
        end = (now + timedelta(hours=hours)).strftime('%Y%m%d')
        params = {
            'begin_date': begin,
            'end_date': end,
            'station': station['id'],
            'product': 'currents_predictions',
            'time_zone': 'lst_ldt',
            'units': 'english',
            'interval': 'MAX_SLACK',
            'format': 'json',
        }
        r = requests.get(NOAA_TIDE_BASE, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        cp = data.get('current_predictions', {}).get('cp', [])
        return {
            'station': station,
            'predictions': cp,
            'fetched': datetime.now().isoformat(),
        }
    return _cached(f'currents_{station_key}', 'currents', fetch)


# ==================== WEATHER ====================

def get_weather():
    """Get hourly weather forecast from NWS."""
    def fetch():
        # First get the forecast URL for our coordinates
        headers = {'User-Agent': 'RedNunDashboard/1.0 (mike@rednun.com)'}
        r = requests.get(f'{NWS_BASE}/points/{CHATHAM_LAT},{CHATHAM_LON}',
                        headers=headers, timeout=10)
        r.raise_for_status()
        meta = r.json()
        
        # Get hourly forecast
        hourly_url = meta['properties']['forecastHourly']
        r2 = requests.get(hourly_url, headers=headers, timeout=10)
        r2.raise_for_status()
        hourly = r2.json()
        
        # Get standard forecast
        forecast_url = meta['properties']['forecast']
        r3 = requests.get(forecast_url, headers=headers, timeout=10)
        r3.raise_for_status()
        forecast = r3.json()
        
        return {
            'hourly': hourly['properties']['periods'][:24],
            'forecast': forecast['properties']['periods'][:6],
            'fetched': datetime.now().isoformat(),
        }
    return _cached('weather', 'weather', fetch)


# ==================== BUOY DATA ====================

def get_buoy(station='44018'):
    """Get latest observations from NDBC buoy (44018 = SE Cape Cod)."""
    def fetch():
        url = f'https://www.ndbc.noaa.gov/data/realtime2/{station}.txt'
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        lines = r.text.strip().split('\n')
        if len(lines) < 3:
            return None
        headers = lines[0].replace('#', '').split()
        # Skip the units line (line 1)
        latest = lines[2].split()
        obs = {}
        for i, h in enumerate(headers):
            if i < len(latest):
                val = latest[i]
                obs[h] = None if val == 'MM' else val
        return {
            'station': station,
            'observation': obs,
            'fetched': datetime.now().isoformat(),
        }
    return _cached(f'buoy_{station}', 'buoy', fetch)


# ==================== SST & CHLOROPHYLL ====================

def get_sst_sources():
    """Return current SST imagery sources and URLs."""
    today = datetime.now().strftime('%Y%m%d')
    return {
        'sources': [
            {
                'name': 'Rutgers COOL — Cape Cod SST',
                'url': 'https://marine.rutgers.edu/cool/sat_data/?nothumbs=0&product=sst&region=capecod',
                'type': 'satellite',
                'desc': 'High-res infrared SST, multiple passes daily. Best for sharp temp breaks.',
                'priority': 1,
            },
            {
                'name': 'Rutgers COOL — Mid-Atlantic Bight',
                'url': 'https://marine.rutgers.edu/cool/sat_data/?nothumbs=0&product=sst&region=nybight',
                'type': 'satellite',
                'desc': 'Wider view showing Gulf Stream influence and offshore breaks.',
                'priority': 2,
            },
            {
                'name': 'NOAA CoastWatch Geo-Polar SST',
                'url': 'https://coastwatch.noaa.gov/cw_html/cwViewer.html',
                'type': 'interactive_map',
                'desc': '5km blended SST. Good for cloud-covered days (fills gaps).',
                'priority': 3,
            },
            {
                'name': 'NOAA OSPO SST Contour Charts',
                'url': 'https://www.ospo.noaa.gov/products/ocean/sst/contour/',
                'type': 'contour',
                'desc': 'Contour lines showing temp gradients. Good for finding 2-degree breaks.',
                'priority': 4,
            },
            {
                'name': 'SatFish SST Charts',
                'url': 'https://www.satfish.com/sea-surface-temperature/',
                'type': 'premium',
                'desc': 'Up to 12 SST images/day. Sharpest temp break resolution. Paid service.',
                'priority': 5,
            },
        ],
        'fetched': datetime.now().isoformat(),
    }


def get_visual_satellite_sources():
    """Return true-color / visible satellite imagery sources."""
    today = datetime.now().strftime('%Y-%m-%d')
    return {
        'sources': [
            {
                'name': 'NASA Worldview — True Color (Today)',
                'url': f'https://worldview.earthdata.nasa.gov/?v=-71.5,40.5,-68.5,42.5&l=VIIRS_NOAA21_CorrectedReflectance_TrueColor,Coastlines_15m&t={today}',
                'type': 'interactive_map',
                'desc': 'Daily true-color satellite photo. See water color changes, plankton blooms, sediment plumes, current boundaries. Updated within 3 hours.',
                'priority': 1,
            },
            {
                'name': 'NASA Worldview — Ocean Color (Today)',
                'url': f'https://worldview.earthdata.nasa.gov/?v=-71.5,40.5,-68.5,42.5&l=MODIS_Aqua_Chlorophyll_a,Coastlines_15m&t={today}',
                'type': 'interactive_map',
                'desc': 'Chlorophyll overlay on satellite view. Green = plankton = bait concentration.',
                'priority': 2,
            },
            {
                'name': 'NOAA OCView — VIIRS True Color',
                'url': 'https://www.star.nesdis.noaa.gov/socd/mecb/color/ocview/ocview.html',
                'type': 'interactive_map',
                'desc': 'VIIRS ocean color at 375m resolution. Best detail for seeing water color breaks off Monomoy.',
                'priority': 3,
            },
            {
                'name': 'GOES-East GeoColor — Near Real-Time',
                'url': 'https://www.star.nesdis.noaa.gov/goes/conus.php?sat=G16&img=GEOCOLOR&length=12',
                'type': 'near_realtime',
                'desc': 'Updated every 5 min. Lower res but near real-time. Check cloud cover and fog before running out.',
                'priority': 4,
            },
            {
                'name': 'Zoom Earth — Live Satellite',
                'url': f'https://zoom.earth/#view=41.55,-69.97,10z/date={today},pm/layers=base',
                'type': 'interactive_map',
                'desc': 'Easy-to-use live satellite viewer. Pinch to zoom right into the shoals.',
                'priority': 5,
            },
        ],
        'fetched': datetime.now().isoformat(),
    }


# ==================== SATELLITE IMAGE PROXY ====================

GIBS_WMS = 'https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi'

# Bounding box: Cape Cod / Monomoy area
BBOX_CHATHAM = '40.8,-70.8,42.2,-69.2'      # Tight on Chatham/Monomoy
BBOX_CAPECOD = '40.0,-71.5,42.5,-68.5'      # Wider Cape Cod view

SATELLITE_LAYERS = {
    'truecolor': {
        'layer': 'VIIRS_NOAA21_CorrectedReflectance_TrueColor',
        'label': 'True Color — VIIRS',
        'format': 'image/jpeg',
    },
    'truecolor_terra': {
        'layer': 'MODIS_Terra_CorrectedReflectance_TrueColor',
        'label': 'True Color — Terra MODIS',
        'format': 'image/jpeg',
    },
    'truecolor_aqua': {
        'layer': 'MODIS_Aqua_CorrectedReflectance_TrueColor',
        'label': 'True Color — Aqua MODIS',
        'format': 'image/jpeg',
    },
    'chlorophyll': {
        'layer': 'MODIS_Aqua_Chlorophyll_a',
        'label': 'Chlorophyll-a — Aqua MODIS',
        'format': 'image/png',
    },
    'sst': {
        'layer': 'GHRSST_L4_MUR_Sea_Surface_Temperature',
        'label': 'Sea Surface Temperature',
        'format': 'image/png',
    },
}

def fetch_satellite_image(layer_key='truecolor', date=None, bbox=None, width=800, height=600):
    """Fetch a satellite image from NASA GIBS WMS."""
    if date is None:
        # Try today, fall back to yesterday (today's image may not be ready yet)
        date = datetime.now().strftime('%Y-%m-%d')
    if bbox is None:
        bbox = BBOX_CHATHAM

    layer_info = SATELLITE_LAYERS.get(layer_key, SATELLITE_LAYERS['truecolor'])

    params = {
        'SERVICE': 'WMS',
        'REQUEST': 'GetMap',
        'LAYERS': layer_info['layer'],
        'FORMAT': layer_info['format'],
        'WIDTH': width,
        'HEIGHT': height,
        'BBOX': bbox,
        'CRS': 'EPSG:4326',
        'TIME': date,
        'VERSION': '1.3.0',
        'STYLES': '',
    }

    cache_key = f"sat_{layer_key}_{date}_{bbox}"

    def fetch():
        r = requests.get(GIBS_WMS, params=params, timeout=15)
        r.raise_for_status()
        content_type = r.headers.get('Content-Type', '')
        if 'image' in content_type:
            import base64
            b64 = base64.b64encode(r.content).decode('utf-8')
            return {
                'image': b64,
                'content_type': content_type,
                'layer': layer_info['label'],
                'date': date,
                'bbox': bbox,
            }
        else:
            logger.warning(f'GIBS returned non-image: {content_type}')
            return None

    return _cached(cache_key, 'tides', fetch)  # reuse 1hr cache TTL


def get_chlorophyll_sources():
    """Return chlorophyll/ocean color data sources."""
    return {
        'sources': [
            {
                'name': 'NOAA CoastWatch — Chlorophyll-a (Daily)',
                'url': 'https://coastwatch.pfeg.noaa.gov/erddap/griddap/erdMH1chla1day.graph?chlorophyll%5B(last)%5D%5B(38.0):(44.0)%5D%5B(-72.0):(-68.0)%5D&.draw=surface&.vars=longitude%7Clatitude%7Cchlorophyll&.colorBar=KT_algae%7C%7CLog%7C0.01%7C30%7C&.bgColor=0xffccccff',
                'type': 'erddap',
                'desc': 'Daily chlorophyll concentration. Green = plankton bloom = bait concentration.',
                'priority': 1,
            },
            {
                'name': 'NOAA CoastWatch — Chlorophyll-a (8-day composite)',
                'url': 'https://coastwatch.pfeg.noaa.gov/erddap/griddap/erdMH1chla8day.graph?chlorophyll%5B(last)%5D%5B(38.0):(44.0)%5D%5B(-72.0):(-68.0)%5D&.draw=surface&.vars=longitude%7Clatitude%7Cchlorophyll&.colorBar=KT_algae%7C%7CLog%7C0.01%7C30%7C&.bgColor=0xffccccff',
                'type': 'erddap',
                'desc': '8-day composite fills cloud gaps. Better coverage, less sharp.',
                'priority': 2,
            },
            {
                'name': 'NASA Worldview — Ocean Color',
                'url': 'https://worldview.earthdata.nasa.gov/?v=-73,-37,-66,45&l=MODIS_Aqua_Chlorophyll_a&t=' + datetime.now().strftime('%Y-%m-%d'),
                'type': 'interactive_map',
                'desc': 'Interactive NASA viewer. Overlay chlorophyll on satellite imagery.',
                'priority': 3,
            },
        ],
        'fetched': datetime.now().isoformat(),
    }


# ==================== LUNAR / SOLUNAR ====================

def get_lunar():
    """Calculate moon phase and solunar feeding periods."""
    now = datetime.now()

    # Moon phase calculation (Metonic cycle approximation)
    # Reference new moon: Jan 6, 2000 18:14 UTC
    ref = datetime(2000, 1, 6, 18, 14)
    days_since = (now - ref).total_seconds() / 86400
    synodic = 29.53058867
    phase_frac = (days_since % synodic) / synodic  # 0=new, 0.5=full

    # Phase name and emoji
    if phase_frac < 0.0625:
        phase_name, phase_icon = 'New Moon', '🌑'
    elif phase_frac < 0.1875:
        phase_name, phase_icon = 'Waxing Crescent', '🌒'
    elif phase_frac < 0.3125:
        phase_name, phase_icon = 'First Quarter', '🌓'
    elif phase_frac < 0.4375:
        phase_name, phase_icon = 'Waxing Gibbous', '🌔'
    elif phase_frac < 0.5625:
        phase_name, phase_icon = 'Full Moon', '🌕'
    elif phase_frac < 0.6875:
        phase_name, phase_icon = 'Waning Gibbous', '🌖'
    elif phase_frac < 0.8125:
        phase_name, phase_icon = 'Last Quarter', '🌗'
    elif phase_frac < 0.9375:
        phase_name, phase_icon = 'Waning Crescent', '🌘'
    else:
        phase_name, phase_icon = 'New Moon', '🌑'

    illumination = round((1 - math.cos(2 * math.pi * phase_frac)) / 2 * 100)

    # Solunar theory: major periods at moon transit (overhead/underfoot)
    # Minor periods at moonrise/moonset
    # Approximate using lunar day (24h 50min)
    lunar_day = 24 * 60 + 50  # minutes
    mins_today = now.hour * 60 + now.minute
    # Moon transit time shifts ~50 min later each day
    day_of_year = now.timetuple().tm_yday
    transit_offset = (day_of_year * 50) % (24 * 60)
    major1 = transit_offset % (24 * 60)
    major2 = (major1 + 12 * 60 + 25) % (24 * 60)
    minor1 = (major1 + 6 * 60 + 12) % (24 * 60)
    minor2 = (minor1 + 12 * 60 + 25) % (24 * 60)

    def fmt_mins(m):
        h = int(m // 60) % 24
        mn = int(m % 60)
        ampm = 'AM' if h < 12 else 'PM'
        h12 = h % 12 or 12
        return f'{h12}:{mn:02d} {ampm}'

    # Rating: best fishing around new/full moon
    if phase_frac < 0.1 or phase_frac > 0.9 or (0.4 < phase_frac < 0.6):
        rating = 'Excellent'
        rating_stars = '★★★★★'
    elif (0.1 < phase_frac < 0.25) or (0.75 < phase_frac < 0.9) or (0.25 < phase_frac < 0.4) or (0.6 < phase_frac < 0.75):
        rating = 'Good'
        rating_stars = '★★★☆☆'
    else:
        rating = 'Fair'
        rating_stars = '★★☆☆☆'

    return {
        'phase_name': phase_name,
        'phase_icon': phase_icon,
        'phase_fraction': round(phase_frac, 3),
        'illumination': illumination,
        'rating': rating,
        'rating_stars': rating_stars,
        'major_periods': [fmt_mins(major1), fmt_mins(major2)],
        'minor_periods': [fmt_mins(minor1), fmt_mins(minor2)],
        'fetched': datetime.now().isoformat(),
    }


# ==================== NDBC SPECTRAL WAVE DATA ====================

def get_wave_spectral(station='44020'):
    """Get spectral wave summary from NDBC."""
    def fetch():
        url = f'https://www.ndbc.noaa.gov/data/realtime2/{station}.spec'
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        lines = r.text.strip().split('\n')
        if len(lines) < 3:
            return None
        headers = lines[0].replace('#', '').split()
        latest = lines[2].split()
        obs = {}
        for i, h in enumerate(headers):
            if i < len(latest):
                val = latest[i]
                obs[h] = None if val == 'MM' else val
        # Parse key fields
        result = {
            'station': station,
            'fetched': datetime.now().isoformat(),
        }
        wvht = obs.get('WVHT')
        if wvht:
            result['sig_wave_height'] = f'{float(wvht):.1f}m ({float(wvht)*3.281:.1f}ft)'
        swh = obs.get('SwH')
        if swh:
            result['swell_height'] = f'{float(swh):.1f}m ({float(swh)*3.281:.1f}ft)'
        swp = obs.get('SwP')
        if swp:
            result['swell_period'] = f'{float(swp):.1f}s'
        swd = obs.get('SwD')
        if swd:
            result['swell_direction'] = swd
        wwh = obs.get('WWH')
        if wwh:
            result['wind_wave_height'] = f'{float(wwh):.1f}m ({float(wwh)*3.281:.1f}ft)'
        wwp = obs.get('WWP')
        if wwp:
            result['wind_wave_period'] = f'{float(wwp):.1f}s'
        wwd = obs.get('WWD')
        if wwd:
            result['wind_wave_direction'] = wwd
        return result
    return _cached(f'wave_spec_{station}', 'buoy', fetch)


# ==================== CONTINUOUS WIND ====================

def get_continuous_wind(station='44020'):
    """Get recent wind history from NDBC standard data (hourly observations)."""
    def fetch():
        url = f'https://www.ndbc.noaa.gov/data/realtime2/{station}.txt'
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        lines = r.text.strip().split('\n')
        if len(lines) < 3:
            return None
        headers = lines[0].replace('#', '').split()
        # Get last 12 hourly observations
        observations = []
        for line in lines[2:14]:
            vals = line.split()
            obs = {}
            for i, h in enumerate(headers):
                if i < len(vals):
                    obs[h] = None if vals[i] == 'MM' else vals[i]
            if obs.get('WSPD'):
                wind_kt = round(float(obs['WSPD']) * 1.944, 1)
                gust_kt = round(float(obs['GST']) * 1.944, 1) if obs.get('GST') else None
                observations.append({
                    'time': f"{obs.get('hh', '??')}:{obs.get('mm', '??')}",
                    'direction': obs.get('WDIR', '--'),
                    'speed_kt': wind_kt,
                    'gust_kt': gust_kt,
                })
        return {
            'station': station,
            'observations': observations,
            'fetched': datetime.now().isoformat(),
        }
    return _cached(f'cwind_{station}', 'buoy', fetch)


# ==================== CAPTAIN'S BRIEFING ====================

def get_briefing():
    """Compile a full captain's briefing with all data sources."""
    briefing = {
        'generated': datetime.now().isoformat(),
        'tides': {},
        'currents': {},
        'weather': None,
        'buoy': None,
        'lunar': get_lunar(),
        'wave_spectral': get_wave_spectral(),
        'wind_history': get_continuous_wind(),
        'sst': get_sst_sources(),
        'visual': get_visual_satellite_sources(),
        'chlorophyll': get_chlorophyll_sources(),
    }

    # Tides for both stations
    for key in STATIONS['tides']:
        briefing['tides'][key] = get_tides(key)

    # Currents for both stations  
    for key in STATIONS['currents']:
        briefing['currents'][key] = get_currents(key)

    # Weather
    briefing['weather'] = get_weather()

    # Buoy
    briefing['buoy'] = get_buoy()

    # Tide chart data
    briefing['tide_chart'] = get_tide_hourly('chatham', 48)

    return briefing


# ==================== FLASK ROUTES ====================

def register_routes(app, login_required):
    """Register fishing intel routes with the Flask app."""
    from flask import jsonify, request

    @app.route('/api/fishing/briefing')
    @login_required
    def api_fishing_briefing():
        try:
            return jsonify(get_briefing())
        except Exception as e:
            logger.error(f'Briefing error: {e}')
            return jsonify({'error': str(e)}), 500

    @app.route('/api/fishing/tides')
    @login_required
    def api_fishing_tides():
        station = request.args.get('station', 'chatham')
        data = get_tides(station)
        return jsonify(data) if data else jsonify({'error': 'unavailable'}), 503

    @app.route('/api/fishing/tides/hourly')
    @login_required
    def api_fishing_tides_hourly():
        station = request.args.get('station', 'chatham')
        data = get_tide_hourly(station)
        return jsonify(data) if data else jsonify({'error': 'unavailable'}), 503

    @app.route('/api/fishing/currents')
    @login_required
    def api_fishing_currents():
        station = request.args.get('station', 'pollock_rip')
        data = get_currents(station)
        return jsonify(data) if data else jsonify({'error': 'unavailable'}), 503

    @app.route('/api/fishing/weather')
    @login_required
    def api_fishing_weather():
        data = get_weather()
        return jsonify(data) if data else jsonify({'error': 'unavailable'}), 503

    @app.route('/api/fishing/buoy')
    @login_required
    def api_fishing_buoy():
        station = request.args.get('station', '44018')
        data = get_buoy(station)
        return jsonify(data) if data else jsonify({'error': 'unavailable'}), 503

    @app.route('/api/fishing/sst')
    @login_required
    def api_fishing_sst():
        return jsonify(get_sst_sources())

    @app.route('/api/fishing/chlorophyll')
    @login_required
    def api_fishing_chlorophyll():
        return jsonify(get_chlorophyll_sources())

    @app.route('/api/fishing/visual')
    @login_required
    def api_fishing_visual():
        return jsonify(get_visual_satellite_sources())

    @app.route('/api/fishing/satellite')
    @login_required
    def api_fishing_satellite():
        """Return a satellite image as base64 for embedding."""
        layer = request.args.get('layer', 'truecolor')
        date = request.args.get('date', None)
        bbox = request.args.get('bbox', None)
        view = request.args.get('view', 'chatham')  # chatham or capecod
        if bbox is None:
            bbox = BBOX_CHATHAM if view == 'chatham' else BBOX_CAPECOD
        data = fetch_satellite_image(layer, date, bbox)
        # Check if image is too small (blank/no data yet) — under 5KB of base64
        if data and len(data.get('image', '')) > 6000:
            return jsonify(data)
        # Try yesterday if today's image is blank or unavailable
        if date is None:
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            data2 = fetch_satellite_image(layer, yesterday, bbox)
            if data2 and len(data2.get('image', '')) > 6000:
                return jsonify(data2)
            # Try 2 days ago
            two_days = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
            data3 = fetch_satellite_image(layer, two_days, bbox)
            if data3 and len(data3.get('image', '')) > 6000:
                return jsonify(data3)
        # Return whatever we have, even if small
        if data:
            return jsonify(data)
        return jsonify({'error': 'Image not available'}), 503

    @app.route('/api/fishing/satellite/layers')
    @login_required
    def api_fishing_satellite_layers():
        """Return available satellite layers."""
        return jsonify({
            'layers': {k: v['label'] for k, v in SATELLITE_LAYERS.items()},
            'views': {
                'chatham': {'bbox': BBOX_CHATHAM, 'label': 'Chatham / Monomoy (tight)'},
                'capecod': {'bbox': BBOX_CAPECOD, 'label': 'Cape Cod (wide)'},
            }
        })

    # Serve the fishing page
    @app.route('/fishing')
    @login_required
    def fishing_page():
        from flask import send_from_directory
        return send_from_directory('static', 'fishing.html')

    @app.route('/api/fishing/lunar')
    @login_required
    def api_fishing_lunar():
        return jsonify(get_lunar())

    @app.route('/api/fishing/waves')
    @login_required
    def api_fishing_waves():
        station = request.args.get('station', '44020')
        data = get_wave_spectral(station)
        if data:
            return jsonify(data)
        return (jsonify({'error': 'unavailable'}), 503)

    @app.route('/api/fishing/wind')
    @login_required
    def api_fishing_wind():
        station = request.args.get('station', '44020')
        data = get_continuous_wind(station)
        if data:
            return jsonify(data)
        return (jsonify({'error': 'unavailable'}), 503)

    logger.info('Fishing intel routes registered')
