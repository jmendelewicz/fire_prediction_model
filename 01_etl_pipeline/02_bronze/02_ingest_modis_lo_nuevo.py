# Databricks notebook source
# MAGIC %md
# MAGIC # GEE Extract — Land Cover (MODIS MCD12Q1)

# COMMAND ----------

# MAGIC %pip install earthengine-api --quiet

# COMMAND ----------

import ee
import pandas as pd
import time
from pyspark.sql import functions as F

CATALOG        = "fire_risk_project"
SCHEMA_LANDING = "00_landing"
GEE_PROJECT    = "fire-risk-project-19-04"
PATH_OUTPUT    = "/Volumes/fire_risk_project/00_landing/modis_static/land_cover_2022_2024.csv"

ee.Initialize(project=GEE_PROJECT)
print("✅ GEE inicializado")

# COMMAND ----------

# ── 2. Leer grilla y convertir a pandas ──────────────────────────────────────
df_grid = (
    spark.table(f"{CATALOG}.{SCHEMA_LANDING}.aux_grid_pampa")
    .filter(F.col("is_valid").cast("string") == "true")
    .select("cell_id", "latitude", "longitude")
    .toPandas()   # conversión a pandas ANTES de iterar
)

print(f"Nodos válidos: {len(df_grid):,}")
display(df_grid.head(3))

# COMMAND ----------

# ── 3. Construir FeatureCollection GEE ───────────────────────────────────────
features_gee = [
    ee.Feature(
        ee.Geometry.Point([float(row.longitude), float(row.latitude)]),
        {"cell_id": str(row.cell_id)}
    )
    for row in df_grid.itertuples()   # itertuples es más rápido que iterrows
]

grilla = ee.FeatureCollection(features_gee)
print(f"FeatureCollection GEE: {grilla.size().getInfo():,} features")

# COMMAND ----------

# ── 4. Función de extracción por año ─────────────────────────────────────────
def extraer_land_cover(anio: int, grilla: ee.FeatureCollection) -> ee.FeatureCollection:
    imagen = (
        ee.ImageCollection("MODIS/061/MCD12Q1")
        .filterDate(f"{anio}-01-01", f"{anio}-12-31")
        .first()
        .select("LC_Type1")
    )

    muestreado = imagen.reduceRegions(
        collection=grilla,
        reducer=ee.Reducer.first(),
        scale=500,
        tileScale=4
    )

    def agregar_flags(feat):
        lc = ee.Number(feat.get("first")).int()

        is_cropland    = lc.eq(12).Or(lc.eq(14))
        is_natural_veg = lc.gte(1).And(lc.lte(11))
        is_forest      = lc.gte(1).And(lc.lte(5))
        land_cover_cat = ee.Algorithms.If(
            is_cropland, 1,
            ee.Algorithms.If(is_natural_veg, 2, 0)
        )

        return feat.set({
            "anio":            anio,
            "fecha":           f"{anio}-01-01",
            "land_cover_type": lc,
            "is_cropland":     is_cropland.toInt(),
            "is_natural_veg":  is_natural_veg.toInt(),
            "is_forest":       is_forest.toInt(),
            "land_cover_cat":  land_cover_cat,
        })

    return muestreado.map(agregar_flags)

# COMMAND ----------

# ── 5. Extracción y descarga año por año ─────────────────────────────────────
AÑOS        = [2022, 2023, 2024]
COLS_EXPORT = ["cell_id", "fecha", "anio", "land_cover_type",
               "is_cropland", "is_natural_veg", "is_forest", "land_cover_cat"]
dfs = []

for anio in AÑOS:
    print(f"\n{'─'*50}\nExtrayendo land cover {anio}...")

    try:
        fc = extraer_land_cover(anio, grilla)
        n  = fc.size().getInfo()
        print(f"  Features en GEE: {n:,}")
    except Exception as e:
        if "not found" in str(e).lower() or "no images" in str(e).lower():
            print(f"  ⚠️  {anio} no disponible — usando {anio-1} como proxy")
            df_proxy          = dfs[-1].copy()
            df_proxy["anio"]  = anio
            df_proxy["fecha"] = f"{anio}-01-01"
            dfs.append(df_proxy)
            continue
        raise e

    features = []
    offset, batch = 0, 2000

    while True:
        batch_info = fc.toList(batch, offset).getInfo()
        if not batch_info:
            break
        for f in batch_info:
            props = f.get("properties", {})
            features.append({col: props.get(col) for col in COLS_EXPORT})
        offset += len(batch_info)
        print(f"  Descargados: {offset:,} / {n:,}")
        time.sleep(0.3)
        if len(batch_info) < batch:
            break

    df_anio = pd.DataFrame(features)
    print(f"  Distribución land_cover_cat:\n{df_anio['land_cover_cat'].value_counts().to_string()}")
    dfs.append(df_anio)

# COMMAND ----------

# ── 6. Consolidar, tipado y QA ───────────────────────────────────────────────
df_final = pd.concat(dfs, ignore_index=True)

for col in ["land_cover_type", "is_cropland", "is_natural_veg", "is_forest", "land_cover_cat"]:
    df_final[col] = pd.to_numeric(df_final[col], errors="coerce").astype("Int64")
df_final["anio"] = df_final["anio"].astype(int)

print(f"Shape final: {df_final.shape}")
print(f"\nDistribución land_cover_cat:\n{df_final['land_cover_cat'].value_counts().sort_index()}")
print(f"\nDistribución land_cover_type:\n{df_final['land_cover_type'].value_counts().sort_index()}")

assert df_final["land_cover_cat"].max() > 0, "❌ land_cover_cat sigue en 0 — revisar extracción"
print("\n✅ QA pasado")

# COMMAND ----------

# ── 7. Guardar ───────────────────────────────────────────────────────────────
df_final.to_csv(PATH_OUTPUT, index=False)
print(f"✅ Guardado en: {PATH_OUTPUT}")
print("Próximo paso: re-correr silver_land_cover.py")




# Databricks notebook source
# MAGIC %md
# MAGIC # GEE Extract — Population Density (WorldPop 100m)

# COMMAND ----------

# MAGIC %pip install earthengine-api --quiet

# COMMAND ----------

import ee
import pandas as pd
import math
import time
from pyspark.sql import functions as F

CATALOG        = "fire_risk_project"
SCHEMA_LANDING = "00_landing"
GEE_PROJECT    = "fire-risk-project-19-04"
PATH_OUTPUT    = f"/Volumes/fire_risk_project/00_landing/modis_static/population_density.csv"
DELTA          = 0.125   # mitad del tamaño de celda (0.25° / 2)

ee.Initialize(project=GEE_PROJECT)
print("✅ GEE inicializado")

# COMMAND ----------

# ── 2. Leer grilla y convertir a pandas ──────────────────────────────────────
df_grid = (
    spark.table(f"{CATALOG}.{SCHEMA_LANDING}.aux_grid_pampa")
    .filter(F.col("is_valid").cast("string") == "true")
    .select("cell_id", "latitude", "longitude")
    .toPandas()   # conversión a pandas ANTES de iterar
)

print(f"Nodos válidos: {len(df_grid):,}")
display(df_grid.head(3))

# COMMAND ----------

# ── 3. Construir FeatureCollection con POLÍGONOS de 0.25° ────────────────────
# Clave: polígonos en lugar de puntos para que el reducer sume todos los
# píxeles de 100m dentro de cada celda de ~27km × 27km
features_gee = [
    ee.Feature(
        ee.Geometry.Rectangle([
            float(row.longitude) - DELTA,
            float(row.latitude)  - DELTA,
            float(row.longitude) + DELTA,
            float(row.latitude)  + DELTA,
        ]),
        {"cell_id": str(row.cell_id)}
    )
    for row in df_grid.itertuples()
]

grilla_poligonos = ee.FeatureCollection(features_gee)
print(f"FeatureCollection GEE (polígonos): {grilla_poligonos.size().getInfo():,} features")

# COMMAND ----------

# ── 4. WorldPop — suma de población por celda ────────────────────────────────
worldpop = (
    ee.ImageCollection("WorldPop/GP/100m/pop")
    .filterDate("2020-01-01", "2020-12-31")
    .filter(ee.Filter.eq("country", "ARG"))
    .mosaic()
    .select("population")
)

# Verificación rápida — si la media es 0, hay problema con la capa
bounds = ee.Geometry.Rectangle([-68, -42, -57, -28])
stats  = worldpop.reduceRegion(
    reducer=ee.Reducer.mean(),
    geometry=bounds,
    scale=5000,
    maxPixels=1e8
).getInfo()
print(f"WorldPop media en la región: {stats}")

# Si WorldPop/GP/100m/pop no tiene cobertura ARG, descomentar alternativa GPWv4:
# worldpop = (
#     ee.Image("CIESIN/GPWv411/GPW_Population_Density/gpw_v4_population_density_rev11_2020_30_sec")
#     .select("population_density")
# )

# COMMAND ----------

# ── 5. Reducción: suma por celda + cálculo de densidad ───────────────────────
def calcular_densidad(feat):
    pop_total = ee.Number(feat.get("sum")).max(0)
    area_km2  = feat.geometry().area().divide(1e6)
    densidad  = pop_total.divide(area_km2)
    log_pop   = densidad.add(1).log()
    return feat.set({
        "fecha":           "2020-01-01",
        "pop_total":       pop_total.round().toInt(),
        "pop_density_km2": densidad,
        "log_pop_density": log_pop,
    })

pop_por_celda = worldpop.reduceRegions(
    collection=grilla_poligonos,
    reducer=ee.Reducer.sum(),
    scale=100,
    tileScale=8
).map(calcular_densidad)

# COMMAND ----------

# ── 6. Descarga a pandas ─────────────────────────────────────────────────────
COLS_EXPORT = ["cell_id", "fecha", "pop_total", "pop_density_km2", "log_pop_density"]

n_total = pop_por_celda.size().getInfo()
print(f"Total celdas a descargar: {n_total:,}")

features = []
offset, batch = 0, 500   # batches pequeños — los polígonos pesan más

while True:
    batch_info = pop_por_celda.toList(batch, offset).getInfo()
    if not batch_info:
        break
    for f in batch_info:
        props = f.get("properties", {})
        features.append({col: props.get(col) for col in COLS_EXPORT})
    offset += len(batch_info)
    print(f"  Descargados: {offset:,} / {n_total:,}")
    time.sleep(0.3)
    if len(batch_info) < batch:
        break

# COMMAND ----------

# ── 7. Consolidar, tipado y QA ───────────────────────────────────────────────
df = pd.DataFrame(features)

df["pop_total"]       = pd.to_numeric(df["pop_total"],       errors="coerce").fillna(0).astype(int)
df["pop_density_km2"] = pd.to_numeric(df["pop_density_km2"], errors="coerce").fillna(0.0)
df["log_pop_density"] = df["pop_density_km2"].apply(lambda x: math.log(x + 1) if x >= 0 else 0.0)

print(f"Shape final: {df.shape}")
print(f"\nEstadísticas:")
print(df[["pop_total", "pop_density_km2"]].describe())
n_nonzero = (df["pop_density_km2"] > 0).sum()
print(f"\nNodos con pop > 0: {n_nonzero:,} de {len(df):,} ({100*n_nonzero/len(df):.1f}%)")

assert n_nonzero > 0, "❌ pop_density sigue en 0 — revisar extracción GEE"
print("✅ QA pasado")

# COMMAND ----------

# ── 8. Guardar en el volumen landing ─────────────────────────────────────────
df.to_csv(PATH_OUTPUT, index=False)
print(f"✅ Guardado en: {PATH_OUTPUT}")
print("Próximo paso: re-correr silver_static_features.py para regenerar la tabla Silver")
