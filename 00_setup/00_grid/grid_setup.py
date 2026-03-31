# Databricks notebook source
# MAGIC %md
# MAGIC # Grid Setup — Grilla Maestra Región Pampeana
# MAGIC
# MAGIC Genera `fire_risk_project.00_landing.aux_grid_pampa` con todos los
# MAGIC features estáticos. Subregiones se completan en `subregion_classification`.
# MAGIC
# MAGIC **Prerequisito:** correr `etl_download_static_data` primero para
# MAGIC asegurar que los archivos OSM y WorldPop estén en `grid_setup/`.
# MAGIC
# MAGIC **Ejecutar UNA SOLA VEZ** — verifica si la tabla ya existe.

# COMMAND ----------

# MAGIC %pip install geopandas pyogrio pyproj shapely earthengine-api --quiet

# COMMAND ----------

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, Polygon, shape, box
import urllib.request, os, zipfile, time, json, ee
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

OUTPUT_TABLE    = "fire_risk_project.00_landing.aux_grid_pampa"
GEE_PROJECT     = "fire-risk-project-19-04"   ###
PATH_GRID_SETUP = "/Volumes/fire_risk_project/00_landing/grid_setup"
PATH_OSM        = f"{PATH_GRID_SETUP}/osm_road_distance.csv"
PATH_POP        = f"{PATH_GRID_SETUP}/population_density.csv"

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

REGIONES_FALLBACK = [
    (1, "Pampa Humeda",    (-63.0, -39.5, -56.5, -30.0)),
    (3, "Delta/Litoral",   (-60.5, -35.0, -57.5, -28.0)),
    (4, "Monte",           (-68.5, -42.0, -65.0, -34.0)),
    (5, "Espinal",         (-67.0, -40.0, -62.0, -30.0)),
    (6, "Chaco Seco",      (-65.0, -30.0, -60.0, -28.0)),
    (7, "Chaco Humedo",    (-61.0, -30.0, -56.5, -28.0)),
    (9, "Patagonia norte", (-68.5, -42.5, -62.0, -37.5)),
]

TMP_DIR = "/tmp/fire_grid"
os.makedirs(TMP_DIR, exist_ok=True)

# COMMAND ----------

# MAGIC %md ## 0 · Verificación de idempotencia

# COMMAND ----------

# Verificar archivos estáticos
if not os.path.exists(PATH_OSM) or not os.path.exists(PATH_POP):
    raise FileNotFoundError(
        "Archivos estáticos no encontrados en grid_setup/.\n"
        "Correr etl_download_static_data primero."
    )

# Verificar si la tabla ya está completa
try:
    existing = spark.table(OUTPUT_TABLE)
    n_valid  = existing.filter("is_valid = true").count()
    has_osm  = existing.filter("dist_road_km IS NOT NULL").count()
    has_pop  = existing.filter("pop_density_km2 IS NOT NULL").count()

    if n_valid > 0 and has_osm > 0 and has_pop > 0:
        print(f"Tabla ya existe y está completa ({n_valid:,} nodos válidos) — saliendo.")
        dbutils.notebook.exit("SKIP: aux_grid_pampa ya existe y está completa.")
    else:
        print("Tabla existe pero incompleta — regenerando.")
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

# MAGIC %md ## 4 · Subregiones

# COMMAND ----------

bbox_poly  = box(LON_MIN - 1, LAT_MIN - 1, LON_MAX + 1, LAT_MAX + 1)
RESOLVE_OK = False

RESOLVE_URLS = [
    (
        "https://data-gis.unep-wcmc.org/server/rest/services/"
        "Bio-geographicalRegions/Resolve_Ecoregions/FeatureServer/0/query"
        f"?geometry={LON_MIN-1},{LAT_MIN-1},{LON_MAX+1},{LAT_MAX+1}"
        "&geometryType=esriGeometryEnvelope&spatialRel=esriSpatialRelIntersects"
        "&outFields=ECO_NAME,BIOME_NAME&returnGeometry=true&f=geojson"
    ),
    "https://raw.githubusercontent.com/jmendelewicz/ecoregions-data/main/ecoregions_southamerica.geojson",
]

geojson_data = None
for url in RESOLVE_URLS:
    try:
        print(f"Intentando RESOLVE: {url[:70]}...")
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        features_region = [
            f for f in data.get("features", [])
            if shape(f["geometry"]).intersects(bbox_poly)
        ]
        if features_region:
            geojson_data = {"type": "FeatureCollection", "features": features_region}
            print(f"{len(features_region)} ecorregiones descargadas")
            RESOLVE_OK = True
            break
    except Exception as e:
        print(f"Error: {e}")

if RESOLVE_OK:
    rows = []
    for feat in geojson_data["features"]:
        eco_name   = feat["properties"].get("ECO_NAME", "")
        sid, sname = RESOLVE_MAP.get(eco_name, (0, "Otro"))
        try:
            geom = shape(feat["geometry"])
            if geom.is_valid:
                rows.append({"subregion_id": sid, "subregion_name": sname, "geometry": geom})
        except Exception:
            pass
    gdf_regiones = (
        gpd.GeoDataFrame(rows, crs="EPSG:4326")
        .dissolve(by=["subregion_id", "subregion_name"])
        .reset_index()
    )
else:
    print("Usando polígonos de respaldo")
    def _bbox_poly(lon_min, lat_min, lon_max, lat_max):
        return Polygon([
            (lon_min, lat_min), (lon_max, lat_min),
            (lon_max, lat_max), (lon_min, lat_max),
            (lon_min, lat_min),
        ])
    gdf_regiones = gpd.GeoDataFrame(
        [{"subregion_id": sid, "subregion_name": nombre,
          "geometry": _bbox_poly(*bbox)}
         for sid, nombre, bbox in REGIONES_FALLBACK],
        crs="EPSG:4326"
    )

gdf_regiones_validas = gdf_regiones[gdf_regiones["subregion_id"] != 0].copy()

df_joined = gpd.sjoin(
    df_valid[["cell_id", "latitude", "longitude", "geometry", "is_valid"]],
    gdf_regiones_validas[["subregion_id", "subregion_name", "geometry"]],
    how="left", predicate="within"
)
df_joined = (
    df_joined
    .sort_values("subregion_id")
    .drop_duplicates(subset="cell_id", keep="first")
)
df_joined["subregion_id"]   = df_joined["subregion_id"].fillna(0).astype(int)
df_joined["subregion_name"] = df_joined["subregion_name"].fillna("Otro")

pct_otro = (df_joined["subregion_name"] == "Otro").mean() * 100
print(f"Sin clasificar: {pct_otro:.1f}%  — si >5% correr subregion_classification con ZIP local")

# COMMAND ----------

# MAGIC %md ## 5 · Features topográficos via GEE

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

print(f"Extrayendo topografía para {len(df_joined):,} nodos...")
df_topo = extraer_topografia_gee(df_joined[["cell_id", "latitude", "longitude"]])
print(f"Topografía: {len(df_topo):,} registros | nulos: {df_topo.isnull().sum().sum()}")

# COMMAND ----------

# MAGIC %md ## 6 · Distancia a rutas y densidad poblacional

# COMMAND ----------

df_osm = pd.read_csv(PATH_OSM)[["cell_id", "dist_road_km"]]
df_pop = pd.read_csv(PATH_POP)[["cell_id", "pop_density_km2"]]

df_osm["dist_road_km"]    = pd.to_numeric(df_osm["dist_road_km"],    errors="coerce").clip(lower=0)
df_pop["pop_density_km2"] = pd.to_numeric(df_pop["pop_density_km2"], errors="coerce").fillna(0.0)

print(f"OSM:     {len(df_osm):,} nodos | dist media: {df_osm['dist_road_km'].mean():.2f} km")
print(f"WorldPop:{len(df_pop):,} nodos | pop  media: {df_pop['pop_density_km2'].mean():.1f} hab/km²")

# COMMAND ----------

# MAGIC %md ## 7 · Ensamble y guardado

# COMMAND ----------

df_grid = df_joined[
    ["cell_id", "latitude", "longitude", "is_valid", "subregion_id", "subregion_name"]
].merge(df_topo, on="cell_id", how="left") \
 .merge(df_osm,  on="cell_id", how="left") \
 .merge(df_pop,  on="cell_id", how="left")

for col in ["elevation", "slope", "aspect", "dist_road_km", "pop_density_km2"]:
    df_grid[col] = pd.to_numeric(df_grid[col], errors="coerce")

df_grid["grid_row"] = ((df_grid["latitude"]  - LAT_MIN) / STEP).round().astype(int)
df_grid["grid_col"] = ((df_grid["longitude"] - LON_MIN) / STEP).round().astype(int)

COLS_FINAL = [
    "cell_id", "latitude", "longitude",
    "grid_row", "grid_col",
    "subregion_id", "subregion_name",
    "elevation", "slope", "aspect",
    "dist_road_km", "pop_density_km2",
    "is_valid",
]
df_grid = df_grid[COLS_FINAL].copy()

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

# MAGIC %md ## 8 · Verificación

# COMMAND ----------

spark.sql(f"""
    SELECT subregion_id, subregion_name,
           COUNT(*) AS nodos,
           ROUND(AVG(elevation), 0) AS elev_media_m,
           ROUND(AVG(dist_road_km), 2) AS dist_road_media_km,
           ROUND(AVG(pop_density_km2), 1) AS pop_media_hab_km2
    FROM {OUTPUT_TABLE}
    GROUP BY subregion_id, subregion_name
    ORDER BY subregion_id
""").show(truncate=False)

print("Siguiente paso: correr subregion_classification para completar subregion_id/name")
dbutils.notebook.exit(f"OK: {OUTPUT_TABLE} generada con {len(df_grid):,} nodos")
