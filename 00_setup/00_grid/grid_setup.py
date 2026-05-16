# Databricks notebook source
# MAGIC %md
# MAGIC # Grid Setup — Grilla Maestra Región Pampeana (Paso 1 de 3)
# MAGIC
# MAGIC Genera la versión base de `aux_grid_pampa` con:
# MAGIC - Coordenadas (lat, lon, cell_id, grid_row, grid_col)
# MAGIC - Máscara de tierra (is_valid)
# MAGIC - Topografía (elevation, slope, aspect) via GEE SRTM
# MAGIC
# MAGIC Las columnas de datos estáticos (dist_road_km, pop_density_km2) y subregiones
# MAGIC se inicializan como NULL/0 y se completan en los pasos 2 y 3.
# MAGIC
# MAGIC **Flujo de setup (3 scripts en orden):**
# MAGIC 1. **Este script** — crea la grilla base + topografía → guarda `aux_grid_pampa`
# MAGIC 2. `grid_download_static_data` — lee la grilla, descarga OSM/WorldPop/LandCover
# MAGIC 3. `grid_subregion_classification` — clasifica subregiones + merge estáticos → UPDATE
# MAGIC
# MAGIC **Ejecutar UNA SOLA VEZ** — verifica si la tabla ya existe.

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

# MAGIC %md ## Configuración

# COMMAND ----------

LAT_MIN = -42.0
LAT_MAX = -28.0
LON_MIN = -68.0
LON_MAX = -56.0
STEP    = 0.25

OUTPUT_TABLE = "fire_risk_project.00_landing.aux_grid_pampa"
GEE_PROJECT  = "fire-risk-project-19-04"   ###

TMP_DIR = "/tmp/fire_grid"
os.makedirs(TMP_DIR, exist_ok=True)

# COMMAND ----------

# MAGIC %md ## 0 · Verificación de idempotencia

# COMMAND ----------

try:
    existing = spark.table(OUTPUT_TABLE)

    # 1. Verificar que la tabla tenga todas las columnas esperadas
    # Nota: land_cover_cat NO es una columna de la grilla — fluye por el pipeline
    # Bronze→Silver→Gold junto a las features climáticas.
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
        print(f"Tabla existe pero le faltan columnas: {cols_faltantes} — regenerando.")
    else:
        # 2. Verificar nodos válidos y topografía
        n_valid   = existing.filter("is_valid = true").count()
        n_con_topo = existing.filter("is_valid = true AND elevation IS NOT NULL").count()
        pct_topo  = n_con_topo / n_valid * 100 if n_valid > 0 else 0

        if n_valid == 0:
            print("Tabla existe pero sin nodos válidos — regenerando.")
        elif pct_topo < 99:
            print(f"Tabla existe pero topografía incompleta "
                  f"({n_con_topo:,}/{n_valid:,} = {pct_topo:.1f}%) — regenerando.")
        else:
            print(f"Tabla OK: {n_valid:,} nodos válidos, {pct_topo:.1f}% con topografía — saliendo.")
            print("Siguiente paso: grid_download_static_data")
            dbutils.notebook.exit("SKIP: aux_grid_pampa ya existe con schema y datos correctos.")
except Exception:
    print("Tabla no existe — generando desde cero.")

# COMMAND ----------

# MAGIC %md ## 1 · Autenticación GEE

# COMMAND ----------

ee.Authenticate()
ee.Initialize(project=GEE_PROJECT)
print("GEE inicializado")

# COMMAND ----------

# MAGIC %md ## 2 · Grilla bruta

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
print(f"Total nodos brutos: {len(df_raw):,}")

# COMMAND ----------

# MAGIC %md ## 3 · Máscara de tierra

# COMMAND ----------

NE_URL = "https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_admin_0_countries.zip"
NE_ZIP = f"{TMP_DIR}/ne_10m_countries.zip"
NE_SHP = f"{TMP_DIR}/ne_10m_admin_0_countries.shp"

if not os.path.exists(NE_SHP):
    print("Descargando Natural Earth 10m...")
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
print(f"Nodos válidos: {n_valid:,} / {len(gdf):,}  ({100 * n_valid / len(gdf):.1f}%)")

# COMMAND ----------

# MAGIC %md ## 4 · Features topográficos via GEE (SRTM)

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
            print(f"  [{i // batch_size + 1}/{n_batches}] {min(i + batch_size, total)}/{total} nodos")
        except Exception as e:
            print(f"  [{i // batch_size + 1}/{n_batches}] Error: {e}")
            for row in batch.itertuples():
                resultados.append({"cell_id": row.cell_id,
                                   "elevation": None, "slope": None, "aspect": None})
        time.sleep(0.5)
    return pd.DataFrame(resultados)

print(f"Extrayendo topografía para {len(df_valid):,} nodos...")
df_topo = extraer_topografia_gee(df_valid[["cell_id", "latitude", "longitude"]])
print(f"Topografía: {len(df_topo):,} registros | nulos: {df_topo.isnull().sum().sum()}")

# COMMAND ----------

# MAGIC %md ## 5 · Ensamble y guardado
# MAGIC
# MAGIC Se guarda la grilla base con topografía. Las columnas de datos estáticos
# MAGIC (dist_road_km, pop_density_km2, subregion_id, subregion_name) se inicializan
# MAGIC vacías — se completan en los pasos 2 y 3 del setup.

# COMMAND ----------

df_grid = df_valid[["cell_id", "latitude", "longitude", "is_valid"]].merge(
    df_topo, on="cell_id", how="left"
)

# Columnas de grid
df_grid["grid_row"] = ((df_grid["latitude"]  - LAT_MIN) / STEP).round().astype(int)
df_grid["grid_col"] = ((df_grid["longitude"] - LON_MIN) / STEP).round().astype(int)

# Columnas que se completan en pasos posteriores (inicializadas como NULL/0)
df_grid["subregion_id"]   = 0
df_grid["subregion_name"] = "Pendiente"
df_grid["dist_road_km"]   = None
df_grid["pop_density_km2"] = None

# Orden final de columnas (coincide con DDL de aux_grid_pampa)
COLS_FINAL = [
    "cell_id", "latitude", "longitude",
    "grid_row", "grid_col",
    "subregion_id", "subregion_name",
    "elevation", "slope", "aspect",
    "dist_road_km", "pop_density_km2",
    "is_valid",
]
df_grid = df_grid[COLS_FINAL].copy()

# Asegurar tipos correctos para Delta
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
print(f"Tabla guardada: {OUTPUT_TABLE}  ({sdf.count():,} registros)")

# COMMAND ----------

# MAGIC %md ## 6 · Verificación

# COMMAND ----------

df_check = spark.table(OUTPUT_TABLE)

n_total = df_check.count()
n_valid = df_check.filter("is_valid = true").count()
n_topo  = df_check.filter("elevation IS NOT NULL").count()

print(f"Total nodos:    {n_total:,}")
print(f"Nodos válidos:  {n_valid:,}")
print(f"Con topografía: {n_topo:,}")
print(f"dist_road_km:   NULL (se completa en paso 3)")
print(f"pop_density:    NULL (se completa en paso 3)")
print(f"subregion:      Pendiente (se completa en paso 3)")

print(f"\nSiguiente paso: correr grid_download_static_data")
dbutils.notebook.exit(f"OK: {OUTPUT_TABLE} — {n_valid:,} nodos válidos con topografía")
