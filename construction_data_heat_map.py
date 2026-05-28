# -*- coding: utf-8 -*-
"""
Created on Tue Apr 29 21:31:54 2025

@author: Admin
"""

# -*- coding: utf-8 -*-
"""
Created on Tue Apr 29 2025
@author: Admin

Script pipeline:
 1. Scrape daily IGC URLs from multiple SoaringSpot championships
 2. Download .igc flight files
 3. Detect thermals and compute climb-rate statistics
 4. Aggregate by rounded coordinates
 5. Render two Folium maps with:
    • Heatmap of average climb rates
    • Circle markers sized by thermal counts
    Both maps feature topographic and OpenAIP airspace layers.
"""

import os
import re
import math
import socket

import pandas as pd
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from urllib.request import urlopen
from urllib.error import URLError
from requests.exceptions import ConnectTimeout, RequestException

from aerofiles.igc import Reader
import geopy.distance
from opensoar.competition.soaringspot import SoaringSpotDaily
from opensoar.thermals.pysoar_thermal_detector import PySoarThermalDetector
from opensoar.utilities.helper_functions import calculate_distance_bearing

# Monkey-patch: AAT.__eq__ crashe si comparé à un RaceTask (pas d'attr t_min)
from opensoar.task.aat import AAT
def _aat_eq_safe(self, other):
    if not hasattr(other, 't_min'):
        return False
    return self.t_min == other.t_min
AAT.__eq__ = _aat_eq_safe

from help_function import datetime_difference, vitesse
import numpy as np


# --- Configuration -----------------------------------------------------
CHAMP_RESULTS_URLS = [
    # "https://www.soaringspot.com/fr/az-cup-2025-zbraslavice-2025/results",
    # "https://www.soaringspot.com/fr/22nd-fai-european-gliding-championships-tabor-2024/results",
    # "https://www.soaringspot.com/fr/plachtarske-mistrovstvi-cr-2024-touzim-2024/results",
    # "https://www.soaringspot.com/fr/az-cup-2024-zbraslavice-2024/results",
    # "https://www.soaringspot.com/fr/junior-world-gliding-championships-2022-tabor-2022/results",
    # "https://www.soaringspot.com/fr/pmcr-pre-jwgc2022-tabor-2021/results",
    # "https://www.soaringspot.com/fr/9th-fai-womens-world-gliding-championship-2017-zbraslavice-2017/results",
    # "https://www.soaringspot.com/fr/35th-world-gliding-championships-hosin-2018/results", ###
    # "https://www.soaringspot.com/fr/19th-fai-european-gliding-championships-moravska-trebova-2017/results", ###
    # "https://www.soaringspot.com/fr/plachtarske-mistrovstvi-cr-2020-tabor-2020/results",
    # "https://www.soaringspot.com/fr/plachtarske-mistrovstvi-cr-2024-touzim-2024//results",
    # "https://www.soaringspot.com/fr/202615m/results",
    # "https://www.soaringspot.com/fr/53smpj-ostrow-glide-2025/results",
    # "https://www.soaringspot.com/fr/qzsa-lm20251/results",
    # "https://www.soaringspot.com/fr/preegc-2025/results",
    # "https://www.soaringspot.com/fr/kluba2025/results",
    # "https://www.soaringspot.com/fr/open2025/results",
    # "https://www.soaringspot.com/fr/49-polish-gliding-championship-of-the-open-rudniki-2024/results",
    # "https://www.soaringspot.com/fr/smpa-2024/results",
    # "https://www.soaringspot.com/fr/jwgc2024/results",
    # "https://www.soaringspot.com/fr/53smpj-ostrow-glide-2025/results",
    # "https://www.soaringspot.com/fr/preegc-2025/results",
    # "https://www.soaringspot.com/fr/polish-junior-nationals-2023/results",
    # "https://www.soaringspot.com/fr/egc2023/results",
    # "https://www.soaringspot.com/fr/prejwgc-2023/results",
    # "https://www.soaringspot.com/fr/polish-50th-junior-nationals-ostrow-glide-2022/results",
    # "https://www.soaringspot.com/fr/ostrow-glide-2022-07/results",
    # "https://www.soaringspot.com/fr/202206eplsozskluba/results",
    # "https://www.soaringspot.com/fr/58-hww/results",
    # "https://www.soaringspot.com/fr/57-hww/results",
    # "https://www.soaringspot.com/fr/56-hww/results",
    # "https://www.soaringspot.com/fr/55-hww/results",
    # "https://www.soaringspot.com/fr/british-team-training-aalen-heidenheim-elchingen-2025/results",
    # "https://www.soaringspot.com/fr/dmj-edpa-2023/results",
    # "https://www.soaringspot.com/fr/ina-2025-romorantin-pruniers-2025/results",
    # "https://www.soaringspot.com/fr/ina-2022-romorantin-pruniers-2022/results",
    
    
    
    # add more championship "results" URLs here
]
SAVE_FOLDER = "downloaded_igc_files"


# --- Utility functions ------------------------------------------------
def get_daily_urls(championship_url):
    """Scrape and return all daily result page URLs for a championship."""
    base = "https://www.soaringspot.com"
    resp = requests.get(championship_url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    # find class pages: /fr/.../results/<class>
    cls_pattern = re.compile(r"^/fr/.+?/results/[^/]+$")
    class_pages = {
        urljoin(base, a['href'])
        for a in soup.find_all('a', href=cls_pattern)
        if 'task-' not in a['href']
    }

    daily_urls = []
    for cls_page in class_pages:
        try:
            r = requests.get(cls_page)
            r.raise_for_status()
            sc = BeautifulSoup(r.text, 'html.parser')
        except Exception:
            continue
        # links ending with /daily
        for a in sc.find_all('a', href=re.compile(r"/daily$")):
            daily_urls.append(urljoin(base, a['href']))
    return sorted(daily_urls)


def round_coordinates(lat, lon, resolution=0.001):
    """Round lat/lon to grid for grouping (~resolution degrees)."""
    rlat = round(lat / resolution) * resolution + 0.5 * resolution
    rlon = round(lon / resolution) * resolution + 0.5 * resolution
    return rlat, rlon



def get_igc_timezone(igc_path):
    """Extract timezone offset (hours) from IGC comment lines. Returns int or None."""
    with open(igc_path, 'r', encoding='ISO-8859-1') as f:
        for line in f:
            if line.startswith('LCU::HPTZNTIMEZONE:'):
                try:
                    return int(line.split(':')[3].strip())
                except (ValueError, IndexError):
                    pass
    return None
def local_time_to_utc(local_time, tz_offset_hours):
    """Convert a datetime.time from local (UTC+tz_offset_hours) to UTC."""
    import datetime
    dt = datetime.datetime.combine(datetime.date.today(), local_time)
    dt_utc = dt - datetime.timedelta(hours=tz_offset_hours)
    return dt_utc.time()

def get_championship_name(url):
    """Extract championship name from SoaringSpot URL."""
    # ex: .../fr/az-cup-2025-zbraslavice-2025/results -> az-cup-2025-zbraslavice-2025
    parts = url.rstrip('/').split('/')
    idx = parts.index('results') if 'results' in parts else -1
    return parts[idx - 1] if idx > 0 else 'unknown'

# --- Step 1: Scrape daily URLs and download IGC files ------------------
os.makedirs(SAVE_FOLDER, exist_ok=True)
daily_to_champ = {}
for idx, champ_url in enumerate(CHAMP_RESULTS_URLS):
    print(f" [{idx+1}/{len(CHAMP_RESULTS_URLS)}] Récupération des journées pour championnat : {champ_url}")
    try:
        daily_pages = get_daily_urls(champ_url)
        champ_name = get_championship_name(champ_url)
        for dp in daily_pages:
            daily_to_champ[dp] = champ_name
        print(f" {len(daily_pages)} journées trouvées.")
    except Exception as e:
        print(f" Échec pour {champ_url} : {e}")
        continue

def scrape_daily_start_times(daily_url):
    """Scrape start times and points per pilot (CN) from a SoaringSpot daily page.
    Returns dict: {competition_id: {'start_time': datetime.time or None, 'points': int or None}}
    """
    import datetime
    resp = requests.get(daily_url.replace('/fr/', '/fr/'))
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    table = soup.find('table')
    if not table:
        return {}

    # detect column indices from headers
    headers = table.find('thead').find_all('th') if table.find('thead') else []
    col_cn, col_start, col_points = None, None, None
    for i, th in enumerate(headers):
        txt = th.text.strip().lower()
        if txt == 'cn':
            col_cn = i
        elif txt == 'start':
            col_start = i
        elif txt == 'points':
            col_points = i
    if col_cn is None or col_start is None:
        return {}

    result = {}
    for row in table.find('tbody').find_all('tr') if table.find('tbody') else table.find_all('tr')[1:]:
        cells = row.find_all('td')
        if len(cells) <= max(col_cn, col_start):
            continue
        cn = cells[col_cn].text.strip()
        if not cn:
            continue
        # parse start time
        st_text = cells[col_start].text.strip()
        start_time = None
        if st_text and ':' in st_text:
            parts = st_text.split(':')
            try:
                start_time = datetime.time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
            except (ValueError, IndexError):
                pass
        # parse points
        pts = None
        if col_points is not None and col_points < len(cells):
            pts_text = cells[col_points].text.strip().replace(',', '')
            try:
                pts = int(pts_text)
            except (ValueError, TypeError):
                pass
        result[cn] = {'start_time': start_time, 'points': pts}
    return result

def thermal_mean_time(thermal_fixes):
    """Compute mean UTC time of a thermal as datetime.time."""
    import datetime
    total_s = 0
    for fix in thermal_fixes:
        t = fix['time']
        total_s += t.hour * 3600 + t.minute * 60 + t.second
    avg_s = total_s / len(thermal_fixes)
    h = int(avg_s // 3600) % 24
    m = int((avg_s % 3600) // 60)
    return datetime.time(h, m)

def thermal_wind_estimate(thermal_fixes):
    """Estimate wind speed and direction from ground speed variation vs heading.
    Uses Fourier decomposition: V_sol(theta) = V_air + V_wind*cos(theta - theta_wind)
    - Skip if < 4 turns
    - Trim 30s start/end
    Returns (wind_speed_ms, wind_dir_deg) or (None, None)."""
    
    if len(thermal_fixes) < 10:
        return None, None

    duration = datetime_difference(
        thermal_fixes[0]['time'],
        thermal_fixes[-1]['time']
    )
    if duration < 40:
        return None, None

    t0 = thermal_fixes[0]['time']
    t = np.array([datetime_difference(t0, f['time']) for f in thermal_fixes])
    lats = np.array([f['lat'] for f in thermal_fixes])
    lons = np.array([f['lon'] for f in thermal_fixes])

    # Segment headings
    dlat = np.diff(lats)
    dlon = np.diff(lons)
    headings = np.unwrap(np.arctan2(dlon, dlat))

    # Number of turns
    n_turns = abs(headings[-1] - headings[0]) / (2 * np.pi)
    if n_turns < 4:
        return None, None

    # Ground speed for each segment
    dt_seg = np.diff(t)
    dist_seg = np.array([
        geopy.distance.geodesic(
            (lats[i], lons[i]),
            (lats[i + 1], lons[i + 1])
        ).m
        for i in range(len(lats) - 1)
    ])

    valid = dt_seg > 0
    if valid.sum() < 10:
        return None, None

    gs = dist_seg[valid] / dt_seg[valid]
    t_mid = ((t[:-1] + t[1:]) / 2)[valid]
    theta = headings[valid]

    # Trim first/last 30 s
    mask_trim = (t_mid >= 30) & (t_mid <= duration - 30)
    if mask_trim.sum() < 10:
        mask_trim = np.ones_like(t_mid, dtype=bool)

    gs_t = gs[mask_trim]
    theta_t = theta[mask_trim]

    # Fourier fit
    A = np.column_stack([
        np.ones(len(theta_t)),
        np.cos(theta_t),
        np.sin(theta_t)
    ])

    coeffs, _, _, _ = np.linalg.lstsq(A, gs_t, rcond=None)
    a0, a1, b1 = coeffs

    wind_speed = np.hypot(a1, b1)*3.6
    wind_dir = (np.degrees(np.arctan2(b1, a1))+180 )% 360

    return round(wind_speed, 2), round(wind_dir, 1)


def thermal_drift(thermal_fixes):
    """Compute drift vector (dlat/dt, dlon/dt) from linear regression.
    Used for ground source projection (step 4).
    Returns (dlat_dt, dlon_dt) in degrees/s or (None, None).
    """
    import numpy as np
    if len(thermal_fixes) < 2:
        return None, None
    t0 = thermal_fixes[0]['time']
    t = np.array([datetime_difference(t0, f['time']) for f in thermal_fixes])
    if t[-1] - t[0] < 40:
        return None, None
    lats = np.array([f['lat'] for f in thermal_fixes])
    lons = np.array([f['lon'] for f in thermal_fixes])
    dlat_dt = np.polyfit(t, lats, 1)[0]
    dlon_dt = np.polyfit(t, lons, 1)[0]
    return dlat_dt, dlon_dt

def circular_mean_std(angles_deg):
    """Circular mean and std using numpy."""
    if not angles_deg:
        return None, None
    rads = np.radians(angles_deg)
    mean_sin = np.mean(np.sin(rads))
    mean_cos = np.mean(np.cos(rads))
    mean_deg = np.degrees(np.arctan2(mean_sin, mean_cos)) % 360
    R = np.sqrt(mean_sin ** 2 + mean_cos ** 2)
    std_deg = np.degrees(np.sqrt(-2 * np.log(R))) if R > 0 else 0
    return mean_deg, std_deg

def batch_elevation(coords, batch_size=100, timeout=10):
    """Query Open-Elevation API in batches. 
    coords: list of (lat, lon)
    Returns list of elevations (meters) or None for failures.
    """
    elevations = [None] * len(coords)
    for i in range(0, len(coords), batch_size):
        batch = coords[i:i + batch_size]
        payload = {"locations": [{"latitude": lat, "longitude": lon} for lat, lon in batch]}
        try:
            resp = requests.post(
                "https://api.open-elevation.com/api/v1/lookup",
                json=payload, timeout=timeout
            )
            resp.raise_for_status()
            results = resp.json().get('results', [])
            for j, r in enumerate(results):
                elevations[i + j] = r.get('elevation')
        except Exception as e:
            print(f"  Elevation API error batch {i}: {e}")
            continue
    return elevations


all_daily = sorted(daily_to_champ.keys())

CSV_SEP = ';'
CSV_OUTPUT = 'dataframe_23204_therlique.csv'
existing_keys = set()
if os.path.isfile(CSV_OUTPUT):
    df_existing = pd.read_csv(CSV_OUTPUT, sep=CSV_SEP)
    if 'source_key' in df_existing.columns:
        existing_keys = set(df_existing['source_key'].unique())
    print(f" {len(existing_keys)} source_keys déjà dans le CSV.")


igc_paths = []
igc_meta = {}  # path -> {source_key, date, championship}

for daily_url in all_daily:
    champ_name = daily_to_champ.get(daily_url, 'unknown')
    print(f"\n Téléchargement et analyse de la journée : {daily_url}")
    try:
        daily_page = SoaringSpotDaily(daily_url)
        competition_day = daily_page.generate_competition_day(SAVE_FOLDER)
        print(f" Compétition : {competition_day.name}, Date : {competition_day.date}, Classe : {competition_day.plane_class}")
        print(f" {len(competition_day.competitors)} vols détectés.")
    except (URLError, ConnectTimeout, socket.error, OSError, RequestException, ValueError, AttributeError):
        print("Erreur inattendue")
        continue
    # task waypoints
    task_wps = ''
    if competition_day.task is not None:
        task_wps = '|'.join(
            f"{wp.latitude},{wp.longitude}" for wp in competition_day.task.waypoints
        )
    # scrape start times & points
    try:
        pilot_info = scrape_daily_start_times(daily_url)
    except Exception:
        pilot_info = {}
        print("Pilote info probleme")
    for c in competition_day.competitors:
        path = os.path.join(
            SAVE_FOLDER,
            competition_day.name.replace(' ', '_'),
            competition_day.plane_class.replace(' ', '_'),
            competition_day.date.strftime('%d-%m-%Y'),
            f"{c.competition_id}.igc"
        )
        if os.path.isfile(path):
            source_key = f"{c.competition_id}_{competition_day.date.strftime('%Y%m%d')}"
            if source_key in existing_keys:
                print(f"  Skip {source_key} (déjà traité)")
                continue
            igc_paths.append(path)
            p_info = pilot_info.get(c.competition_id, {})
            igc_meta[path] = {
                'source_key': source_key,
                'date': competition_day.date.strftime('%Y-%m-%d'),
                'championship': champ_name,
                'start_time': p_info.get('start_time'),
                'points': p_info.get('points'),
                'task_wps': task_wps,
            }

# --- Step 2: Detect thermals and aggregate stats ------------------------
stats = []

for igc in igc_paths:
    with open(igc, 'r', encoding='ISO-8859-1') as f:
        parsed = Reader().read(f)
    _, trace = parsed['fix_records']
    tz_offset = get_igc_timezone(igc)
    # compute deltas for each fix
    for i in range(len(trace)-1):
        p0, p1 = trace[i], trace[i+1]
        p0['Velocity'] = 3.6 * vitesse(p0['time'], p1['time'], p0['lon'], p1['lon'], p0['lat'], p1['lat'])
        p0['delta_pressure'] = p1['pressure_alt'] - p0['pressure_alt']
        p0['delta_gps'] = p1['gps_alt'] - p0['gps_alt']
        _, brg = calculate_distance_bearing(p1, p0)
        p0['bearing'] = brg
        p0['delta_t'] = datetime_difference(p0['time'], p1['time'])
        p0['delta_d'] = geopy.distance.geodesic((p0['lat'], p0['lon']), (p1['lat'], p1['lon'])).km
    # last fix defaults
    trace[-1].update({'Velocity':0,'delta_pressure':0,'delta_gps':0,
                      'bearing': trace[-2]['bearing'],
                      'delta_t': datetime_difference(trace[-2]['time'], trace[-1]['time']),
                      'delta_d': 0})
    # thermal detection
    det = PySoarThermalDetector()
    try:
        phases = det.analyse(trace)
    except Exception:
        continue
    thermals = [ph[1] for ph in phases if not ph[0]]
    # pass 1: wind estimates per thermal
    wind_estimates = []
    for th in thermals:
        ws, wd = thermal_wind_estimate(th)
        if ws is not None:
            wind_estimates.append((ws, wd))
    # aggregate wind stats for this flight
    if wind_estimates:
        speeds, dirs = zip(*wind_estimates)
        wind_speed_mean = sum(speeds) / len(speeds)
        wind_speed_std = (sum((s - wind_speed_mean) ** 2 for s in speeds) / len(speeds)) ** 0.5
        wind_dir_mean, wind_dir_std = circular_mean_std(dirs)
        
    else:
        wind_speed_mean, wind_dir_mean, wind_speed_std, wind_dir_std = None, None, None, None
    # pass 2: collect thermal stats
    for th in thermals:
        if len(th) < 2: continue
        duration = datetime_difference(th[0]['time'], th[-1]['time'])
        if duration < 40: continue
        gain = th[-1]['gps_alt'] - th[0]['gps_alt']
        avgv = gain / duration if duration else 0
        mean_lat = sum(p['lat'] for p in th) / len(th)
        mean_lon = sum(p['lon'] for p in th) / len(th)
        mean_alt = sum(p['gps_alt'] for p in th) / len(th)
        dlat_dt, dlon_dt = thermal_drift(th)
        
        meta = igc_meta[igc]
        hour = thermal_mean_time(th)
        if meta['start_time'] is not None:
            start_utc = local_time_to_utc(meta['start_time'], tz_offset) if tz_offset is not None else meta['start_time']
            if hour < start_utc:
                continue
        stats.append({
            'mean_lat': mean_lat, 'mean_lon': mean_lon, 'avg_vario': avgv,
            'source_key': meta['source_key'],
            'date': meta['date'],
            'championship': meta['championship'],
            'hour': hour.strftime('%H:%M'),
            'points': meta['points'],
            'wind_speed_mean': round(wind_speed_mean, 2) if wind_speed_mean is not None else None,
            'wind_dir_mean': round(wind_dir_mean, 1) if wind_dir_mean is not None else None,
            'wind_speed_std': round(wind_speed_std, 2) if wind_speed_std is not None else None,
            'wind_dir_std': round(wind_dir_std, 1) if wind_dir_std is not None else None,
            'mean_alt': mean_alt,
            'dlat_dt': dlat_dt,
            'dlon_dt': dlon_dt,
            'task_wps': meta['task_wps'],
        })
# build DataFrame
df_th = pd.DataFrame(stats)
if not df_th.empty:
    # raise RuntimeError("No thermals detected.")
    
    # --- Ground source projection ---
    # batch query terrain elevation at mean positions
    coords_elev = list(zip(df_th['mean_lat'], df_th['mean_lon']))
    print(f" Requête élévation pour {len(coords_elev)} thermiques...")
    terrain_alts = batch_elevation(coords_elev)
    
    ground_lats, ground_lons = [], []
    for i, row in df_th.iterrows():
        terrain = terrain_alts[i]
        dlat_dt = row['dlat_dt']
        dlon_dt = row['dlon_dt']
        if terrain is not None and dlat_dt is not None and dlon_dt is not None:
            # height above ground at mean position
            height_agl = row['mean_alt'] - terrain
            if height_agl > 0:
                # drift speed in deg/s -> time to descend from mean_alt to ground
                # assume thermal rises ~1.5 m/s on average -> time from ground to mean_alt
                t_ground = height_agl / max(row['avg_vario'], 0.5)
                # project backwards in time
                ground_lats.append(row['mean_lat'] - dlat_dt * t_ground)
                ground_lons.append(row['mean_lon'] - dlon_dt * t_ground)
            else:
                ground_lats.append(row['mean_lat'])
                ground_lons.append(row['mean_lon'])
        else:
            # fallback
            ground_lats.append(row['mean_lat'])
            ground_lons.append(row['mean_lon'])
    
    df_th['ground_lat'] = ground_lats
    df_th['ground_lon'] = ground_lons
    # drop intermediate columns
    df_th.drop(columns=['mean_alt', 'dlat_dt', 'dlon_dt'], inplace=True)
    
    # round & group
    coords = df_th.apply(lambda r: round_coordinates(r['mean_lat'], r['mean_lon'], resolution=0.01), axis=1)
    df_th['lat_r'], df_th['lon_r'] = zip(*coords)
    
    # Colonnes finales ordonnées
    CSV_COLUMNS = [
        'mean_lat', 'mean_lon', 'avg_vario', 'lat_r', 'lon_r',
        'source_key', 'date', 'hour', 'points',
        'ground_lat', 'ground_lon',
        'wind_speed_mean', 'wind_dir_mean', 'wind_speed_std', 'wind_dir_std',
        'task_wps', 'championship',
    ]
    df_th = df_th[CSV_COLUMNS]
    
    
    # Append au CSV existant
    if os.path.isfile(CSV_OUTPUT):
        existing_cols = pd.read_csv(CSV_OUTPUT, nrows=0, sep=CSV_SEP).columns.tolist()
        if 'source_key' in existing_cols:
            df_th[CSV_COLUMNS].to_csv(CSV_OUTPUT, mode='a', header=False, index=False, sep=CSV_SEP)
        else:
            df_th[CSV_COLUMNS].to_csv(CSV_OUTPUT, index=False, sep=CSV_SEP)
    else:
        df_th[CSV_COLUMNS].to_csv(CSV_OUTPUT, index=False, sep=CSV_SEP)
    print(f" {len(df_th)} thermiques ajoutées au CSV.")