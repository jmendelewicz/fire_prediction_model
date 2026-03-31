# Databricks notebook source
# MAGIC %md
# MAGIC # Subregion Classification — RESOLVE Ecoregions 2017
# MAGIC
# MAGIC Completa `subregion_id` y `subregion_name` en `aux_grid_pampa`
# MAGIC usando el shapefile oficial RESOLVE Ecoregions 2017.
# MAGIC
# MAGIC **Ejecutar después de grid_setup** — requiere que `aux_grid_pampa` exista.
# MAGIC
# MAGIC **Prerequisito:** subir el shapefile al volumen:
# MAGIC `/Volumes/fire_risk_project/00_landing/ecoregions/Ecoregions2017.zip`
# MAGIC Fuente: https://freegisdata.rtwilson.com (WWF World Ecoregions)

# COMMAND ----------

# MAGIC %pip install geopandas pyogrio pyproj shapely earthengine-api --quiet

# COMMAND ----------

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, box
import zipfile, os
import warnings
from pyspark.sql import SparkSession
warnings.filterwarnings("ignore")

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

TABLE     = "fire_risk_project.00_landing.aux_grid_pampa"
ZIP_PATH  = "/Volumes/fire_risk_project/00_landing/ecoregions/Ecoregions2017.zip"
TMP_DIR   = "/tmp/resolve_ecoregions"
BBOX      = (-69, -43, -55, -27)

RESOLVE_MAP = {
    "Humid Pampas":                        (1, "Pampa Humeda"),
    "Uruguayan savanna":                   (1, "Pampa Humeda"),
    "Paraná flooded savanna":              (3, "Delta/Litoral"),
    "Southern Cone Mesopotamian savanna":  (3, "Delta/Litoral"),
    "Low Monte":                           (4, "Monte"),
    "High Monte":                          (4, "Monte"),
    "Espinal":                             (5, "Espinal"),
    "Dry Chaco":                           (6, "Chaco Seco"),
    "Humid Chaco":                         (7, "Chaco Humedo"),
    "Alto Paraná Atlantic forests":        (7, "Chaco Humedo"),
    "Patagonian steppe":                   (9, "Patagonia norte"),
    "Southern Andean steppe":              (9, "Patagonia norte"),
    "Southern Andean Yungas":              (9, "Patagonia norte"),
    "Central Andean puna":                 (9, "Patagonia norte"),
}

# COMMAND ----------

# MAGIC %md ## Verificación de Idempotencia

# COMMAND ----------

try:
    df_check  = spark.table(TABLE).toPandas()
    n_clasif  = (df_check["subregion_id"] != 0).sum()
    pct_otro  = (df_check["subregion_name"] == "Otro").mean() * 100

    if n_clasif > 0 and pct_otro < 5:
        print(f"Subregiones ya clasificadas ({n_clasif:,} nodos, {pct_otro:.1f}% sin clasificar) — saliendo.")
        dbutils.notebook.exit("SKIP: subregiones ya clasificadas.")
    else:
        print(f"Clasificando subregiones ({n_clasif:,} nodos clasificados, {pct_otro:.1f}% sin clasificar)...")
except Exception:
    print("Tabla no existe o sin subregiones — clasificando...")

# COMMAND ----------

# MAGIC %md ## 1 · Cargar grilla existente

# COMMAND ----------

# DBTITLE 1,Untitled
df_grid = spark.table(TABLE).toPandas()
print(f"Grilla: {len(df_grid):,} nodos")
print(f"Subregiones actuales: {df_grid['subregion_name'].value_counts().to_dict()}")

# COMMAND ----------

# MAGIC %md ## 2 · Extraer shapefile RESOLVE

# COMMAND ----------

if not os.path.exists(ZIP_PATH):
    raise FileNotFoundError(
        f"No se encontró el shapefile RESOLVE en {ZIP_PATH}\n"
        "Subir Ecoregions2017.zip al volumen ecoregions/ antes de continuar."
    )

os.makedirs(TMP_DIR, exist_ok=True)
with zipfile.ZipFile(ZIP_PATH, "r") as z:
    z.extractall(TMP_DIR)
    archivos = z.namelist()

shp_file = next(
    (os.path.join(TMP_DIR, f) for f in archivos if f.endswith(".shp")), None
)
if shp_file is None:
    raise FileNotFoundError("No se encontró .shp dentro del zip.")

gdf_resolve = gpd.read_file(shp_file, engine="pyogrio").to_crs("EPSG:4326")
print(f"Ecoregiones globales: {len(gdf_resolve):,}")

# COMMAND ----------

# MAGIC %md ## 3 · Filtrar y mapear subregiones pampeanas

# COMMAND ----------

bbox_pampa  = box(*BBOX)
gdf_pampa   = gdf_resolve[gdf_resolve.intersects(bbox_pampa)].copy()

gdf_pampa["subregion_id"]   = gdf_pampa["ECO_NAME"].map(
    lambda x: RESOLVE_MAP.get(x, (0, "Otro"))[0]
)
gdf_pampa["subregion_name"] = gdf_pampa["ECO_NAME"].map(
    lambda x: RESOLVE_MAP.get(x, (0, "Otro"))[1]
)

gdf_subregiones = (
    gdf_pampa[gdf_pampa["subregion_id"] != 0]
    .dissolve(by=["subregion_id", "subregion_name"])
    .reset_index()[["subregion_id", "subregion_name", "geometry"]]
)

print(f"Subregiones mapeadas: {len(gdf_subregiones)}")
for _, r in gdf_subregiones.iterrows():
    print(f"  {int(r.subregion_id):2d}  {r.subregion_name}")

# COMMAND ----------

# MAGIC %md ## 4 · Spatial join sobre la grilla

# COMMAND ----------

gdf_grid = gpd.GeoDataFrame(
    df_grid[["cell_id", "latitude", "longitude"]].copy(),
    geometry=[Point(row.longitude, row.latitude) for row in df_grid.itertuples()],
    crs="EPSG:4326"
)

df_joined = gpd.sjoin(
    gdf_grid,
    gdf_subregiones[["subregion_id", "subregion_name", "geometry"]],
    how="left",
    predicate="within"
)
df_joined = (
    df_joined
    .sort_values("subregion_id")
    .drop_duplicates(subset="cell_id", keep="first")
)
df_joined["subregion_id"]   = df_joined["subregion_id"].fillna(0).astype(int)
df_joined["subregion_name"] = df_joined["subregion_name"].fillna("Otro")

pct_otro = (df_joined["subregion_name"] == "Otro").mean() * 100
print(f"Sin clasificar (Otro): {pct_otro:.1f}%  — objetivo: < 5%")

# COMMAND ----------

# MAGIC %md ## 5 · Actualizar tabla

# COMMAND ----------

cols_base = [c for c in df_grid.columns if c not in ["subregion_id", "subregion_name"]]
df_final  = df_grid[cols_base].merge(
    df_joined[["cell_id", "subregion_id", "subregion_name"]],
    on="cell_id", how="left"
)

assert len(df_final) == len(df_grid), \
    f"Error: se perdieron nodos ({len(df_final)} vs {len(df_grid)})"

sdf = spark.createDataFrame(df_final)
(
    sdf.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(TABLE)
)
print(f"Tabla actualizada: {TABLE}  ({len(df_final):,} nodos)")

# COMMAND ----------

# MAGIC %md ## 6 · Verificación

# COMMAND ----------

spark.sql(f"""
    SELECT subregion_id, subregion_name,
           COUNT(*) AS nodos,
           ROUND(AVG(elevation), 0) AS elev_media_m
    FROM {TABLE}
    GROUP BY subregion_id, subregion_name
    ORDER BY subregion_id
""").show(truncate=False)

dbutils.notebook.exit(f"OK: subregiones clasificadas en {TABLE}")
