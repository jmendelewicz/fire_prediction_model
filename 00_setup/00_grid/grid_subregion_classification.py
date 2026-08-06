# Databricks notebook source
# MAGIC %md
# MAGIC # Subregion Classification + Static Features Merge (Paso 3 de 3)
# MAGIC
# MAGIC Completa `aux_grid_pampa` con:
# MAGIC - `subregion_id` y `subregion_name` (RESOLVE Ecoregions 2017)
# MAGIC - `dist_road_km` (desde grid_setup/osm_road_distance.csv)
# MAGIC - `pop_density_km2` (desde grid_setup/population_density.csv)
# MAGIC
# MAGIC **Prerequisitos:**
# MAGIC 1. `grid_setup` ejecutado (tabla `aux_grid_pampa` con grilla + topografía)
# MAGIC 2. `grid_download_static_data` ejecutado (archivos CSV en `/grid_setup/`)
# MAGIC 3. Shapefile subido: `/Volumes/.../ecoregions/Ecoregions2017.zip`
# MAGIC
# MAGIC **Idempotente:** sale si la tabla ya tiene subregiones clasificadas y datos estáticos.

# COMMAND ----------

# MAGIC %pip install geopandas pyogrio pyproj shapely --quiet

# COMMAND ----------

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, box
import zipfile, os
import warnings
warnings.filterwarnings("ignore")

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

TABLE     = "fire_risk_project.00_landing.aux_grid_pampa"

PATH_GRID_SETUP = "/Volumes/fire_risk_project/00_landing/grid_setup"
PATH_OSM        = f"{PATH_GRID_SETUP}/osm_road_distance.csv"
PATH_POP        = f"{PATH_GRID_SETUP}/population_density.csv"

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

# MAGIC %md ## Verificación de prerequisitos

# COMMAND ----------

try:
    df_grid = spark.table(TABLE).toPandas()
    print(f"Grilla cargada: {len(df_grid):,} nodos")
except Exception:
    raise RuntimeError("aux_grid_pampa no existe. Correr grid_setup primero.")

faltantes = [p for p in [PATH_OSM, PATH_POP] if not os.path.exists(p)]
if faltantes:
    raise FileNotFoundError(
        f"Archivos estáticos no encontrados: {faltantes}\n"
        "Correr grid_download_static_data primero."
    )

if not os.path.exists(ZIP_PATH):
    raise FileNotFoundError(
        f"No se encontró {ZIP_PATH}\n"
        "Subir Ecoregions2017.zip al volumen ecoregions/."
    )

# COMMAND ----------

# MAGIC %md ## Idempotencia
# MAGIC
# MAGIC Verifica que la tabla tenga la morfología y los datos completos:
# MAGIC - Todas las columnas presentes
# MAGIC - Subregiones clasificadas (>95% de nodos válidos)
# MAGIC - dist_road_km y pop_density_km2 con cobertura ≥ 95% de los nodos del CSV

# COMMAND ----------

completar = False

COLS_REQUERIDAS = {"subregion_id", "subregion_name", "dist_road_km", "pop_density_km2"}
cols_faltantes  = COLS_REQUERIDAS - set(df_grid.columns)
if cols_faltantes:
    print(f"⚠ Columnas faltantes en la tabla: {cols_faltantes}")
    completar = True

n_valid = len(df_grid[df_grid["is_valid"] == True]) if "is_valid" in df_grid.columns else len(df_grid)

if not completar:
    n_clasif = (df_grid["subregion_id"] != 0).sum()
    pct_clasif = n_clasif / n_valid * 100 if n_valid > 0 else 0
    if pct_clasif < 95:
        print(f"⚠ Subregiones: {n_clasif:,}/{n_valid:,} = {pct_clasif:.1f}% (< 95%)")
        completar = True
    else:
        print(f"✓ Subregiones: {n_clasif:,}/{n_valid:,} = {pct_clasif:.1f}%")

    df_osm_check      = pd.read_csv(PATH_OSM)
    n_osm_csv         = len(df_osm_check)
    n_osm_tabla       = df_grid["dist_road_km"].notna().sum()
    pct_osm           = n_osm_tabla / n_osm_csv * 100 if n_osm_csv > 0 else 0
    if pct_osm < 95:
        print(f"⚠ dist_road_km: {n_osm_tabla:,} en tabla vs {n_osm_csv:,} en CSV = {pct_osm:.1f}% (< 95%)")
        completar = True
    else:
        print(f"✓ dist_road_km: {n_osm_tabla:,}/{n_osm_csv:,} = {pct_osm:.1f}%")

    df_pop_check      = pd.read_csv(PATH_POP)
    n_pop_csv         = len(df_pop_check)
    n_pop_tabla       = df_grid["pop_density_km2"].notna().sum()
    pct_pop           = n_pop_tabla / n_pop_csv * 100 if n_pop_csv > 0 else 0
    if pct_pop < 95:
        print(f"⚠ pop_density_km2: {n_pop_tabla:,} en tabla vs {n_pop_csv:,} en CSV = {pct_pop:.1f}% (< 95%)")
        completar = True
    else:
        print(f"✓ pop_density_km2: {n_pop_tabla:,}/{n_pop_csv:,} = {pct_pop:.1f}%")

if not completar:
    print("\nTabla completa — saliendo.")
    dbutils.notebook.exit("SKIP: tabla ya tiene subregiones + estáticos completos.")

print("\n→ Datos incompletos — re-ejecutando clasificación y merge de estáticos...")

# COMMAND ----------

# MAGIC %md ## 1 · Clasificar subregiones (RESOLVE Ecoregions)

# COMMAND ----------

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

bbox_pampa = box(*BBOX)
gdf_pampa  = gdf_resolve[gdf_resolve.intersects(bbox_pampa)].copy()

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

# MAGIC %md ## 2 · Spatial join

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

# MAGIC %md ## 3 · Merge datos estáticos (OSM + WorldPop)

# COMMAND ----------

print("Cargando archivos estáticos de grid_setup/...")

df_osm = pd.read_csv(PATH_OSM)[["cell_id", "dist_road_km"]]
df_pop = pd.read_csv(PATH_POP)[["cell_id", "pop_density_km2"]]

df_osm["dist_road_km"]    = pd.to_numeric(df_osm["dist_road_km"],    errors="coerce").clip(lower=0)
df_pop["pop_density_km2"] = pd.to_numeric(df_pop["pop_density_km2"], errors="coerce").fillna(0.0)

print(f"OSM:      {len(df_osm):,} nodos | dist media: {df_osm['dist_road_km'].mean():.2f} km")
print(f"WorldPop: {len(df_pop):,} nodos | pop  media: {df_pop['pop_density_km2'].mean():.1f} hab/km²")

# COMMAND ----------

# MAGIC %md ## 4 · Ensamblar tabla final

# COMMAND ----------

cols_base = [c for c in df_grid.columns
             if c not in ["subregion_id", "subregion_name", "dist_road_km", "pop_density_km2"]]

df_final = (
    df_grid[cols_base]
    .merge(df_joined[["cell_id", "subregion_id", "subregion_name"]], on="cell_id", how="left")
    .merge(df_osm, on="cell_id", how="left")
    .merge(df_pop, on="cell_id", how="left")
)

df_final["subregion_id"]    = df_final["subregion_id"].fillna(0).astype(int)
df_final["subregion_name"]  = df_final["subregion_name"].fillna("Otro")
df_final["pop_density_km2"] = df_final["pop_density_km2"].fillna(0.0)

assert len(df_final) == len(df_grid), \
    f"Error: se perdieron nodos ({len(df_final)} vs {len(df_grid)})"

print(f"Tabla final: {len(df_final):,} nodos")

# COMMAND ----------

# MAGIC %md ## 5 · Guardar (overwrite)

# COMMAND ----------

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
           ROUND(AVG(elevation), 0) AS elev_media_m,
           ROUND(AVG(dist_road_km), 2) AS dist_road_media_km,
           ROUND(AVG(pop_density_km2), 1) AS pop_media_hab_km2
    FROM {TABLE}
    WHERE is_valid = true
    GROUP BY subregion_id, subregion_name
    ORDER BY subregion_id
""").show(truncate=False)

nulos = spark.sql(f"""
    SELECT
        COUNT(*) FILTER (WHERE subregion_id = 0) AS sin_subregion,
        COUNT(*) FILTER (WHERE dist_road_km IS NULL) AS sin_osm,
        COUNT(*) FILTER (WHERE pop_density_km2 IS NULL) AS sin_pop,
        COUNT(*) FILTER (WHERE elevation IS NULL) AS sin_topo
    FROM {TABLE} WHERE is_valid = true
""").collect()[0]
print(f"Nodos sin subregión: {nulos.sin_subregion}")
print(f"Nodos sin OSM:       {nulos.sin_osm}")
print(f"Nodos sin WorldPop:  {nulos.sin_pop}")
print(f"Nodos sin topografía: {nulos.sin_topo}")

dbutils.notebook.exit(f"OK: {TABLE} completa — subregiones + estáticos integrados")
