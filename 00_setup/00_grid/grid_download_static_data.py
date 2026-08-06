# Databricks notebook source
# MAGIC %md # Download Static Grid Data

# COMMAND ----------

# MAGIC %pip install geopandas shapely pyproj earthengine-api --quiet

# COMMAND ----------

import os
import time
import requests
import zipfile
import pandas as pd
import numpy as np
import geopandas as gpd
from shapely.geometry import Point
from shapely.ops import unary_union
import ee

# COMMAND ----------

# MAGIC %md ## Config

# COMMAND ----------

GEE_PROJECT = "gee_project_id"

PATH_GRID_SETUP = "/Volumes/fire_risk_project/00_landing/grid_setup"
PATH_OSM        = f"{PATH_GRID_SETUP}/osm_road_distance.csv"
PATH_POP        = f"{PATH_GRID_SETUP}/population_density.csv"

TABLE_GRID      = "fire_risk_project.00_landing.aux_grid_pampa"

OSM_URL     = "https://download.geofabrik.de/south-america/argentina-latest-free.shp.zip"
OSM_DIR     = "/tmp/argentina_osm"
ZIP_PATH    = "/tmp/argentina_osm.zip"
SHP_PATH    = f"{OSM_DIR}/gis_osm_roads_free_1.shp"
CRS_METRICO = "EPSG:22175"
TIPOS_RUTA  = [
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
    "residential", "unclassified", "track"
]

DELTA = 0.125

# COMMAND ----------

# MAGIC %md ## Idempotence check

# COMMAND ----------

osm_exists = os.path.exists(PATH_OSM)
pop_exists = os.path.exists(PATH_POP)

if osm_exists and pop_exists:
    print("BOTH FILES ALREADY THERE NOTHING TO DOWNLOAD")
    print(f"  {PATH_OSM}")
    print(f"  {PATH_POP}")
    dbutils.notebook.exit("SKIP STATIC FILES ALREADY THERE")

print(f"ROAD FILE {'FOUND' if osm_exists else 'MISSING'}")
print(f"POPULATION FILE {'FOUND' if pop_exists else 'MISSING'}")

# COMMAND ----------

# MAGIC %md ## Load valid grid

# COMMAND ----------

df_grid = (
    spark.table(TABLE_GRID)
    .filter("is_valid = true")
    .select("cell_id", "latitude", "longitude")
    .toPandas()
)
print(f"VALID CELLS {len(df_grid):,}")

# COMMAND ----------

# MAGIC %md ## Road distance from OSM

# COMMAND ----------

if not osm_exists:
    os.makedirs(OSM_DIR, exist_ok=True)

    if not os.path.exists(SHP_PATH):
        print("DOWNLOAD OSM ARGENTINA BIG FILE")
        r = requests.get(OSM_URL, stream=True, timeout=600)
        with open(ZIP_PATH, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        with zipfile.ZipFile(ZIP_PATH, "r") as z:
            z.extractall(OSM_DIR)
        print("DOWNLOAD DONE")
    else:
        print("OSM SHAPEFILE ALREADY IN TMP REUSE IT")

    roads       = gpd.read_file(SHP_PATH)
    roads_f     = roads[roads["fclass"].isin(TIPOS_RUTA)].copy()
    roads_proj  = roads_f.to_crs(CRS_METRICO)
    roads_union = unary_union(roads_proj.geometry)
    print(f"ROADS KEPT {len(roads_f):,}")

    gdf = gpd.GeoDataFrame(
        df_grid[["cell_id", "latitude", "longitude"]],
        geometry=[Point(lon, lat)
                  for lat, lon in zip(df_grid["latitude"], df_grid["longitude"])],
        crs="EPSG:4326"
    ).to_crs(CRS_METRICO)

    print("DISTANCE COMPUTE START")
    dist_km = []
    n = len(gdf)
    for i, geom in enumerate(gdf.geometry):
        dist_km.append(geom.distance(roads_union) / 1000)
        if i % 300 == 0:
            print(f"  CELL {i+1} OF {n} SAMPLE DISTANCE KM {dist_km[-1]:.2f}")

    df_osm = gdf[["cell_id"]].copy()
    df_osm["dist_road_km"] = dist_km

    os.makedirs(PATH_GRID_SETUP, exist_ok=True)
    df_osm.to_csv(PATH_OSM, index=False)
    print(f"FILE SAVED {PATH_OSM} ROWS {len(df_osm):,}")
else:
    print(f"ROAD FILE ALREADY THERE SKIP")

# COMMAND ----------

# MAGIC %md ## Population density from WorldPop

# COMMAND ----------

if not pop_exists:
    ee.Authenticate()
    ee.Initialize(project=GEE_PROJECT)
    print("EARTH ENGINE READY")

    fc_poligonos = ee.FeatureCollection([
        ee.Feature(
            ee.Geometry.Rectangle([
                float(row.longitude) - DELTA, float(row.latitude) - DELTA,
                float(row.longitude) + DELTA, float(row.latitude) + DELTA,
            ]),
            {"cell_id": str(row.cell_id)}
        )
        for row in df_grid.itertuples()
    ])

    worldpop = (
        ee.ImageCollection("WorldPop/GP/100m/pop")
        .filterDate("2020-01-01", "2020-12-31")
        .filter(ee.Filter.eq("country", "ARG"))
        .mosaic()
        .select("population")
    )

    def calcular_densidad(feat):
        pop_total = ee.Number(feat.get("sum")).max(0)
        area_km2  = feat.geometry().area().divide(1e6)
        densidad  = pop_total.divide(area_km2)
        return feat.set({
            "fecha":           "2020-01-01",
            "pop_total":       pop_total.round().toInt(),
            "pop_density_km2": densidad,
        })

    pop_por_celda = worldpop.reduceRegions(
        collection=fc_poligonos,
        reducer=ee.Reducer.sum(),
        scale=100,
        tileScale=8
    ).map(calcular_densidad)

    COLS_POP = ["cell_id", "fecha", "pop_total", "pop_density_km2"]
    n_total  = pop_por_celda.size().getInfo()
    features = []
    offset   = 0
    batch    = 500

    while True:
        batch_info = pop_por_celda.toList(batch, offset).getInfo()
        if not batch_info:
            break
        for f in batch_info:
            props = f.get("properties", {})
            features.append({c: props.get(c) for c in COLS_POP})
        offset += len(batch_info)
        print(f"DOWNLOADED {offset:,} OF {n_total:,}")
        time.sleep(0.3)
        if len(batch_info) < batch:
            break

    df_pop = pd.DataFrame(features)
    df_pop["pop_total"]       = pd.to_numeric(df_pop["pop_total"],       errors="coerce").fillna(0).astype(int)
    df_pop["pop_density_km2"] = pd.to_numeric(df_pop["pop_density_km2"], errors="coerce").fillna(0.0)

    n_nonzero = (df_pop["pop_density_km2"] > 0).sum()
    print(f"CELLS WITH PEOPLE {n_nonzero:,} OF {len(df_pop):,}")

    os.makedirs(PATH_GRID_SETUP, exist_ok=True)
    df_pop.to_csv(PATH_POP, index=False)
    print(f"FILE SAVED {PATH_POP} ROWS {len(df_pop):,}")
else:
    print(f"POPULATION FILE ALREADY THERE SKIP")

# COMMAND ----------

# MAGIC %md ## Check

# COMMAND ----------

for path in [PATH_OSM, PATH_POP]:
    if os.path.exists(path):
        df = pd.read_csv(path)
        print(f"FILE OK {path} ROWS {len(df):,}")
    else:
        print(f"FILE MISSING {path}")

dbutils.notebook.exit("STATIC FILES READY IN GRID SETUP FOLDER")
