# -*- coding: utf-8 -*-
"""
enrich_csv_landcover.py
-----------------------
Enrichit un CSV de thermiques avec la couverture du sol Corine Land Cover 2018
via le service WMS ArcGIS de Copernicus (GetFeatureInfo, live).

Stratégie :
 1. Arrondi ground_lat/lon à la grille 0.01° → cellules uniques
 2. 1 requête GetFeatureInfo par cellule (centre)
 3. Cache disque (reprise possible après interruption)
 4. Mapping code CLC niveau 3 → groupe simplifié
 5. Join sur (gr_lat, gr_lon) → 2 nouvelles colonnes : land_cover, land_cover_group

Usage :
    python enrich_csv_landcover.py input.csv output.csv [--workers 8]

@author: Aurélien Doriat
"""

import os
import sys
import json
import time
import argparse
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- Configuration ----------------------------------------------------
GRID_RES = 0.001
CACHE_FILE = "_clc_cache.json"
CHECKPOINT_EVERY = 200
MAX_WORKERS_DEFAULT = 8
REQ_TIMEOUT = 15
RETRIES = 3

# --- Mapping code CLC niveau 3 → groupe simplifié --------------------
# Source: https://land.copernicus.eu/content/corine-land-cover-nomenclature-guidelines
CLC_GROUPS = {
    # Tissu urbain
    111: "Tissu urbain",      112: "Tissu urbain",
    # Zones industrielles / commerciales
    121: "Zones industrielles / commerciales",
    122: "Zones industrielles / commerciales",
    123: "Zones industrielles / commerciales",
    124: "Zones industrielles / commerciales",
    # Mines / décharges / chantiers
    131: "Mines / décharges / chantiers",
    132: "Mines / décharges / chantiers",
    133: "Mines / décharges / chantiers",
    # Espaces verts urbains
    141: "Espaces verts urbains",
    142: "Espaces verts urbains",
    # Terres arables
    211: "Terres arables",
    212: "Terres arables",
    213: "Terres arables",
    # Cultures permanentes
    221: "Cultures permanentes",
    222: "Cultures permanentes",
    223: "Cultures permanentes",
    # Prairies
    231: "Prairies",
    # Zones agricoles hétérogènes
    241: "Zones agricoles hétérogènes",
    242: "Zones agricoles hétérogènes",
    243: "Zones agricoles hétérogènes",
    244: "Zones agricoles hétérogènes",
    # Forêts
    311: "Forêts feuillus",
    312: "Forêts conifères",
    313: "Forêts mixtes",
    # Pelouses / landes
    321: "Pelouses / landes",
    322: "Pelouses / landes",
    # Végétation arbustive
    323: "Végétation arbustive",
    324: "Végétation arbustive",
    # Espaces ouverts / sol nu
    331: "Espaces ouverts / sol nu",
    332: "Espaces ouverts / sol nu",
    333: "Espaces ouverts / sol nu",
    334: "Espaces ouverts / sol nu",
    335: "Espaces ouverts / sol nu",
    # Zones humides
    411: "Zones humides", 412: "Zones humides",
    421: "Zones humides", 422: "Zones humides", 423: "Zones humides",
    # Eau (gardé pour complétude, même si pas dans la liste utilisateur)
    511: "Eaux continentales", 512: "Eaux continentales",
    521: "Eaux marines", 522: "Eaux marines", 523: "Eaux marines",
}

# --- Session HTTP avec retry ------------------------------------------
def make_session():

    session = requests.Session()

    retries = Retry(
        total=RETRIES,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"]
    )

    adapter = HTTPAdapter(
        max_retries=retries,
        pool_connections=32,
        pool_maxsize=32
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session

# --- Conversion lat/lon → Web Mercator (EPSG:3857) -------------------
def lonlat_to_mercator(lon, lat):
    x = lon * 20037508.34 / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    y = y * 20037508.34 / 180.0
    return x, y


# --- Requête GetFeatureInfo CLC ---------------------------------------
WMS_URL = "https://image.discomap.eea.europa.eu/arcgis/rest/services/Corine/CLC2018_WM/MapServer/0/query"
WMS_LAYER = "12"


def query_clc(session, lat, lon):
    """
    ArcGIS REST Query sur layer 0 (CLC 2018 vecteur) en WGS84.
    Retourne le code CLC niveau 3 (int) ou None.
    """
    try:
        params = {
            "f": "json",
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "Code_18",
            "returnGeometry": "false",
        }
        r = session.get(WMS_URL, params=params, timeout=(10, 20))
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            return None
        feats = data.get("features", [])
        if not feats:
            return None
        attrs = feats[0].get("attributes", {})
        code = attrs.get("Code_18")
        if code is None:
            return None
        try:
            return int(str(code).strip())
        except Exception:
            return None
    except Exception:
        return None# --- Cache -----------------------------------------------------------
def load_cache(path):
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_cache(path, cache):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, path)

# --- Main ------------------------------------------------------------
# --- Main ------------------------------------------------------------
def main(input_csv,
         output_csv,
         workers=MAX_WORKERS_DEFAULT,
         cache_file=CACHE_FILE):
    # Test 1 requête pour valider la connectivité
    print("[0/5] Test connexion CLC…")
    test_session = make_session()
    test_code = query_clc(test_session, 48.85, 2.35)   # Paris
    print(f"      Paris (48.85, 2.35) → code CLC = {test_code}")
    if test_code is None:
        print("      ATTENTION: la requête test a échoué. Vérifie le réseau / l'URL.")
        print("      Mode verbose activé pour la 1re requête réelle.")
        
    print(f"[1/5] Lecture {input_csv}…")
    
    df = pd.read_csv(input_csv, sep=';')
    print(f"      {len(df):,} lignes, colonnes: {list(df.columns)}")

    if "ground_lat" not in df.columns or "ground_lon" not in df.columns:
        sys.exit("ERREUR: colonnes ground_lat / ground_lon absentes.")

    print("[2/5] Construction grille au sol (0.01°)…")
    valid = df.dropna(subset=["ground_lat", "ground_lon"]).copy()

    valid["gr_lat"] = (
        (valid["ground_lat"] / GRID_RES).round() * GRID_RES
        + 0.5 * GRID_RES
    )

    valid["gr_lon"] = (
        (valid["ground_lon"] / GRID_RES).round() * GRID_RES
        + 0.5 * GRID_RES
    )

    cells = valid[["gr_lat", "gr_lon"]].drop_duplicates().reset_index(drop=True)

    print(f"      {len(cells):,} cellules uniques (vs {len(valid):,} thermiques)")

    print(f"[3/5] Chargement cache disque ({cache_file})…")
    cache = load_cache(cache_file)

    print(f"      {len(cache):,} cellules déjà en cache")

    todo = []

    for _, row in cells.iterrows():
        key = f"{row.gr_lat:.4f},{row.gr_lon:.4f}"

        if key not in cache:
            todo.append((key, row.gr_lat, row.gr_lon))

    print(f"      {len(todo):,} cellules à requêter, {workers} workers")

    if todo:

        session = make_session()

        t0 = time.time()

        n_done = 0
        n_ok = 0
        n_fail = 0

        def worker(item):
            key, lat, lon = item
            code = query_clc(session, lat, lon)
            return key, code

        print("[4/5] Requêtes WMS GetFeatureInfo…")

        with ThreadPoolExecutor(max_workers=workers) as ex:

            futures = {
                ex.submit(worker, item): item
                for item in todo
            }

            for fut in as_completed(futures):

                key, code = fut.result()

                cache[key] = code

                n_done += 1

                if code is None:
                    n_fail += 1
                else:
                    n_ok += 1

                if n_done % 100 == 0 or n_done == len(todo):

                    elapsed = time.time() - t0

                    rate = n_done / elapsed if elapsed > 0 else 0

                    eta = (
                        (len(todo) - n_done) / rate
                        if rate > 0 else 0
                    )

                    print(
                        f"      {n_done:,}/{len(todo):,}  "
                        f"ok={n_ok:,}  fail={n_fail:,}  "
                        f"rate={rate:.1f}/s  ETA={eta:.0f}s"
                    )

                if n_done % CHECKPOINT_EVERY == 0:
                    save_cache(cache_file, cache)

        save_cache(cache_file, cache)

        print(
            f"      Terminé en {time.time()-t0:.1f}s  "
            f"(ok={n_ok}, fail={n_fail})"
        )

    else:
        print("[4/5] Rien à requêter (cache complet).")

    print("[5/5] Application au CSV complet…")

    def lookup(row):

        if pd.isna(row.get("ground_lat")) or pd.isna(row.get("ground_lon")):
            return (None, None)

        glat = (
            round(row["ground_lat"] / GRID_RES) * GRID_RES
            + 0.5 * GRID_RES
        )

        glon = (
            round(row["ground_lon"] / GRID_RES) * GRID_RES
            + 0.5 * GRID_RES
        )

        key = f"{glat:.4f},{glon:.4f}"

        code = cache.get(key)

        if code is None:
            return (None, None)

        return (code, CLC_GROUPS.get(code, "Inconnu"))

    results = df.apply(lookup, axis=1, result_type="expand")

    df["land_cover"] = results[0]
    df["land_cover_group"] = results[1]

    df.to_csv(output_csv, index=False)

    print(f"      Écrit {output_csv}")

    # Stats finales
    grp = df["land_cover_group"].value_counts(dropna=False)

    print("\nRépartition land_cover_group :")

    for k, v in grp.items():

        pct = 100 * v / len(df)

        print(f"  {str(k):40s} {v:7,d}  ({pct:5.1f} %)")


# --------------------------------------------------------------------
if __name__ == "__main__":

    INPUT_CSV = r"C:\Users\aurel\Documents\analyse_vol\analyse_vol\Heat_map_v2\dataframe_23204_therlique.csv"

    OUTPUT_CSV = r"C:\Users\aurel\Documents\analyse_vol\analyse_vol\Heat_map_v2\dataframe_23204_therlique_landcover.csv"

    WORKERS = 8

    CACHE_FILE_PATH = r"C:\Users\aurel\Documents\analyse_vol\analyse_vol\Heat_map_v2\cache_landcover.json"

    if os.path.exists(CACHE_FILE_PATH):
        os.remove(CACHE_FILE_PATH)
        print("Cache précédent supprimé.")
    
    main(
        input_csv=INPUT_CSV,
        output_csv=OUTPUT_CSV,
        workers=WORKERS,
        cache_file=CACHE_FILE_PATH
    )
