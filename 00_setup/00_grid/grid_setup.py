# Databricks notebook source
# MAGIC %md # Grid Setup

# COMMAND ----------

# MAGIC %pip install geopandas pyogrio pyproj shapely earthengine-api --quiet

# COMMAND ----------

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
import urllib.request, os, zipfile, time, ee
import warnings
warnings.filterwarnings("ignore")

# COMMAND ----------

# MAGIC %md ## Config

# COMMAND ----------

LAT_MIN = -42.0
LAT_MAX = -28.0
LON_MIN = -68.0
LON_MAX = -56.0
STEP    = 0.25

OUTPUT_TABLE = "fire_risk_project.00_landing.aux_grid_pampa"
GEE_PROJECT  = "fire-risk-project-19-04"

TMP_DIR = "/tmp/fire_grid"
os.makedirs(TMP_DIR, exist_ok=True)

# COMMAND ----------

# MAGIC %md ## Idempotence check

# COMMAND ----------

try:
    existing = spark.table(OUTPUT_TABLE)

    COLS_REQUERIDAS = {
        "cell_id", "latitude", "longitude", "grid_row", "grid_col",
        "subregion_id", "subregion_name",
        "elevation", "slope", "aspect",
        "dist_road_km", "pop_density_km2",
        "is_valid",
    }
    cols_existentes = set(existing.columns)
    cols_faltantes  = COLS_REQUERIDAS - cols_existentes

    if cols_faltantes:
        print(f"TABLE FOUND BUT COLUMNS MISSING {cols_faltantes} REBUILD")
    else:
        n_valid   = existing.filter("is_valid = true").count()
        n_con_topo = existing.filter("is_valid = true AND elevation IS NOT NULL").count()
        pct_topo  = n_con_topo / n_valid * 100 if n_valid > 0 else 0

        if n_valid == 0:
            print("TABLE FOUND BUT NO VALID CELLS REBUILD")
        elif pct_topo < 99:
            print(f"TABLE FOUND BUT TOPOGRAPHY INCOMPLETE "
                  f"{n_con_topo:,} OF {n_valid:,} SHARE {pct_topo:.1f} REBUILD")
        else:
            print(f"TABLE OK VALID CELLS {n_valid:,} TOPOGRAPHY SHARE {pct_topo:.1f} EXIT")
            print("NEXT STEP GRID DOWNLOAD STATIC DATA")
            dbutils.notebook.exit("SKIP GRID TABLE ALREADY GOOD")
except Exception:
    print("TABLE NOT FOUND BUILD FROM SCRATCH")

# COMMAND ----------

# MAGIC %md ## Earth Engine auth

# COMMAND ----------

ee.Authenticate()
ee.Initialize(project=GEE_PROJECT)
print("EARTH ENGINE READY")

# COMMAND ----------

# MAGIC %md ## Raw grid

# COMMAND ----------

n_lats = round((LAT_MAX - LAT_MIN) / STEP) + 1
n_lons = round((LON_MAX - LON_MIN) / STEP) + 1

lats = np.linspace(LAT_MIN, LAT_MAX, n_lats)
lons = np.linspace(LON_MIN, LON_MAX, n_lons)

lat_grid, lon_grid = np.meshgrid(lats, lons, indexing="ij")

df_raw = pd.DataFrame({
    "latitude":  lat_grid.ravel(),
    "longitude": lon_grid.ravel(),
})
df_raw["cell_id"] = (
    df_raw["latitude"].map(lambda x: f"{x:.4f}") + "_" +
    df_raw["longitude"].map(lambda x: f"{x:.4f}")
)
print(f"RAW GRID CELLS {len(df_raw):,}")

# COMMAND ----------

# MAGIC %md ## Land mask

# COMMAND ----------

NE_URL = "https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_admin_0_countries.zip"
NE_ZIP = f"{TMP_DIR}/ne_10m_countries.zip"
NE_SHP = f"{TMP_DIR}/ne_10m_admin_0_countries.shp"

if not os.path.exists(NE_SHP):
    print("DOWNLOAD NATURAL EARTH SHAPES")
    urllib.request.urlretrieve(NE_URL, NE_ZIP)
    with zipfile.ZipFile(NE_ZIP) as z:
        z.extractall(TMP_DIR)

world  = gpd.read_file(NE_SHP, engine="pyogrio").to_crs("EPSG:4326")
ar_uy  = world[world["NAME"].isin(["Argentina", "Uruguay"])].geometry.unary_union

gdf = gpd.GeoDataFrame(
    df_raw.copy(),
    geometry=[Point(row.longitude, row.latitude) for row in df_raw.itertuples()],
    crs="EPSG:4326"
)
gdf["is_valid"] = gdf.geometry.within(ar_uy)
n_valid         = gdf["is_valid"].sum()
df_valid        = gdf[gdf["is_valid"]].copy().reset_index(drop=True)
print(f"VALID CELLS {n_valid:,} OF {len(gdf):,} SHARE {100 * n_valid / len(gdf):.1f}")

# COMMAND ----------

# MAGIC %md ## Topography from SRTM

# COMMAND ----------

def extraer_topografia_gee(df_nodos: pd.DataFrame, batch_size: int = 150) -> pd.DataFrame:
    dem     = ee.Image("USGS/SRTMGL1_003")
    terreno = ee.Terrain.products(dem)
    img     = terreno.select(["elevation", "slope", "aspect"])
    resultados = []
    total      = len(df_nodos)
    n_batches  = (total - 1) // batch_size + 1

    for i in range(0, total, batch_size):
        batch    = df_nodos.iloc[i: i + batch_size]
        features = [
            ee.Feature(
                ee.Geometry.Point([row.longitude, row.latitude]),
                {"cell_id": row.cell_id}
            )
            for row in batch.itertuples()
        ]
        try:
            muestras = img.sampleRegions(
                collection=ee.FeatureCollection(features),
                scale=500, geometries=False
            ).getInfo()
            for feat in muestras["features"]:
                p = feat["properties"]
                resultados.append({
                    "cell_id":   p.get("cell_id"),
                    "elevation": round(float(p.get("elevation", 0)), 1),
                    "slope":     round(float(p.get("slope", 0)), 2),
                    "aspect":    round(float(p.get("aspect", 0)), 1),
                })
            print(f"  BATCH {i // batch_size + 1} OF {n_batches} CELLS {min(i + batch_size, total)} OF {total}")
        except Exception as e:
            print(f"  BATCH {i // batch_size + 1} OF {n_batches} FAILED REASON {e}")
            for row in batch.itertuples():
                resultados.append({"cell_id": row.cell_id,
                                   "elevation": None, "slope": None, "aspect": None})
        time.sleep(0.5)
    return pd.DataFrame(resultados)

print(f"TOPOGRAPHY FETCH START CELLS {len(df_valid):,}")
df_topo = extraer_topografia_gee(df_valid[["cell_id", "latitude", "longitude"]])
print(f"TOPOGRAPHY ROWS {len(df_topo):,} NULLS {df_topo.isnull().sum().sum()}")

# COMMAND ----------

# MAGIC %md ## Assemble and save

# COMMAND ----------

df_grid = df_valid[["cell_id", "latitude", "longitude", "is_valid"]].merge(
    df_topo, on="cell_id", how="left"
)

df_grid["grid_row"] = ((df_grid["latitude"]  - LAT_MIN) / STEP).round().astype(int)
df_grid["grid_col"] = ((df_grid["longitude"] - LON_MIN) / STEP).round().astype(int)

df_grid["subregion_id"]   = 0
df_grid["subregion_name"] = "Pendiente"
df_grid["dist_road_km"]   = None
df_grid["pop_density_km2"] = None

COLS_FINAL = [
    "cell_id", "latitude", "longitude",
    "grid_row", "grid_col",
    "subregion_id", "subregion_name",
    "elevation", "slope", "aspect",
    "dist_road_km", "pop_density_km2",
    "is_valid",
]
df_grid = df_grid[COLS_FINAL].copy()

df_grid["dist_road_km"]    = df_grid["dist_road_km"].astype("float64")
df_grid["pop_density_km2"] = df_grid["pop_density_km2"].astype("float64")

sdf = spark.createDataFrame(df_grid)
(
    sdf.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(OUTPUT_TABLE)
)
print(f"TABLE SAVED {OUTPUT_TABLE} ROWS {sdf.count():,}")

# COMMAND ----------

# MAGIC %md ## Check

# COMMAND ----------

df_check = spark.table(OUTPUT_TABLE)

n_total = df_check.count()
n_valid = df_check.filter("is_valid = true").count()
n_topo  = df_check.filter("elevation IS NOT NULL").count()

print(f"TOTAL CELLS {n_total:,}")
print(f"VALID CELLS {n_valid:,}")
print(f"CELLS WITH TOPOGRAPHY {n_topo:,}")
print(f"ROAD DISTANCE STILL EMPTY")
print(f"POPULATION DENSITY STILL EMPTY")
print(f"SUBREGION STILL EMPTY")

print(f"\nNEXT STEP GRID DOWNLOAD STATIC DATA")
dbutils.notebook.exit(f"GRID READY {OUTPUT_TABLE} VALID CELLS {n_valid:,}")
