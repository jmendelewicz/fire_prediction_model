# Databricks notebook source
# MAGIC %md
# MAGIC # Silver - Open-Meteo (Seed + Forecast)
# MAGIC
# MAGIC Construye `silver_openmeteo` uniendo el seed histórico (35 días) con el
# MAGIC forecast diario (4 días). Aplica clips, enrichment desde `aux_grid_pampa`,
# MAGIC calcula el FWI secuencial completo y genera features temporales.
# MAGIC
# MAGIC **Flujo diario (Job2):**
# MAGIC 1. `extract_openmeteo_seed` → `bronze_openmeteo_seed` (MERGE, ventana deslizante 35d)
# MAGIC 2. `extract_openmeteo_forecast` → `bronze_openmeteo_forecast` (sobreescribe completo)
# MAGIC 3. **Este script** → `silver_openmeteo` (sobreescribe: seed 35d + forecast 4d)
# MAGIC
# MAGIC **Resultado:** 39 filas por nodo (35 históricas + 4 forecast), is_forecast=True
# MAGIC para los días futuros. El FWI se calcula con continuidad histórica completa.
# MAGIC
# MAGIC **Nota NDVI:** se usa el último valor disponible en `ndvi_silver` (forward-fill
# MAGIC ya aplicado en transform_modis). Para los días de forecast se propaga el
# MAGIC último valor histórico conocido.

# COMMAND ----------

import pandas as pd
import numpy as np
import logging
from pyspark.sql import functions as F

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    force=True
)
logger = logging.getLogger("SILVER_OPENMETEO")

# COMMAND ----------

# Fix A-5 (2026-05-16): path relativo.
# MAGIC %run ../../00_setup/00_common_functions/fwi_calculator

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

CATALOG = "fire_risk_project"

TABLE_SEED     = f"{CATALOG}.01_bronze.bronze_openmeteo_seed"
TABLE_FORECAST = f"{CATALOG}.01_bronze.bronze_openmeteo_forecast"
TABLE_GRID     = f"{CATALOG}.00_landing.aux_grid_pampa"
TABLE_NDVI     = f"{CATALOG}.02_silver.silver_ndvi"
TABLE_LC       = f"{CATALOG}.02_silver.silver_land_cover"
TABLE_OUTPUT   = f"{CATALOG}.02_silver.silver_openmeteo"

# Fecha de corte: hoy es el primer día de forecast
FECHA_CORTE = pd.Timestamp.now().normalize().strftime("%Y-%m-%d")

# Valores iniciales FWI - Van Wagner (1987)
FFMC_INIT = 85.0
DMC_INIT  = 6.0
DC_INIT   = 15.0

# COMMAND ----------

# MAGIC %md ## 1 · Cargar y unir seed + forecast

# COMMAND ----------

logger.info(f"Fecha de corte: {FECHA_CORTE} (días anteriores = histórico, >= = forecast)")

# Seed histórico: 35 días anteriores a hoy
df_seed = (
    spark.read.table(TABLE_SEED)
    .filter(F.col("date") < FECHA_CORTE)
    .withColumn("is_forecast", F.lit(False))
)

# Forecast: 4 días a partir de hoy (incluye hoy con datos actualizados)
df_fc = (
    spark.read.table(TABLE_FORECAST)
    .filter(F.col("date") >= FECHA_CORTE)
    .withColumn("is_forecast", F.lit(True))
)

# Unión: 39 filas por nodo
df_combined = df_seed.unionByName(df_fc)

logger.info(f"Seed: {df_seed.count():,} filas | Forecast: {df_fc.count():,} filas")

# COMMAND ----------

# MAGIC %md ## 2 · Enrichment desde aux_grid_pampa

# COMMAND ----------

df_grid = (
    spark.read.table(TABLE_GRID)
    .filter("is_valid = true")
    .select(
        "cell_id", "subregion_id", "subregion_name",
        "elevation", "slope", "aspect",
        "dist_road_km", "pop_density_km2"
    )
)

df_combined = df_combined.join(df_grid, on="cell_id", how="left")

# COMMAND ----------

# MAGIC %md ## 3 · NDVI más reciente (forward-fill desde ndvi_silver)

# COMMAND ----------

# Tomar el último NDVI disponible por nodo (el ndvi_silver ya tiene forward-fill)
df_ndvi_last = (
    spark.read.table(TABLE_NDVI)
    .filter(F.col("fecha") <= F.lit(FECHA_CORTE))
    .groupBy("cell_id")
    .agg(F.last("ndvi", ignorenulls=True).alias("ndvi"))
)

df_combined = df_combined.join(df_ndvi_last, on="cell_id", how="left")

# COMMAND ----------

# MAGIC %md ## 4 · Land Cover — último año disponible

# COMMAND ----------

df_lc_last = (
    spark.read.table(TABLE_LC)
    .groupBy("cell_id")
    .agg(F.last("land_cover_cat", ignorenulls=True).alias("land_cover_cat"))
)

df_combined = df_combined.join(df_lc_last, on="cell_id", how="left")
df_combined = df_combined.fillna({"land_cover_cat": 0, "ndvi": 0.0})

logger.info(f"Dataset enriquecido: {df_combined.count():,} filas")

# COMMAND ----------

# MAGIC %md ## 5 · FWI secuencial por nodo en Pandas

# COMMAND ----------

logger.info("Convirtiendo a Pandas para FWI...")
df_pd = df_combined.toPandas()
df_pd["date"]       = pd.to_datetime(df_pd["date"])
df_pd["mes"]        = df_pd["date"].dt.month
df_pd["dia_anio"]   = df_pd["date"].dt.dayofyear
df_pd = df_pd.sort_values(["cell_id", "date"]).reset_index(drop=True)

logger.info(f"Pandas: {len(df_pd):,} filas, {df_pd['cell_id'].nunique():,} nodos")

# Calcular FWI usando el módulo fwi_calculator (definido como %run arriba)
nodos      = df_pd["cell_id"].unique()
resultados = []

for nodo in nodos:
    df_nodo = df_pd[df_pd["cell_id"] == nodo].copy()
    resultados.append(calcular_fwi_serie(df_nodo, FFMC_INIT, DMC_INIT, DC_INIT))

df_fwi = pd.concat(resultados, ignore_index=True)
logger.info(f"FWI calculado: {len(df_fwi):,} filas")

# COMMAND ----------

# MAGIC %md ## 6 · Features temporales (días secos)

# COMMAND ----------

def dias_sin_lluvia(serie: pd.Series) -> pd.Series:
    resultado, contador = [], 0
    for v in serie:
        contador = contador + 1 if v <= 0.1 else 0
        resultado.append(contador)
    return pd.Series(resultado, index=serie.index)

df_fwi["dias_secos"] = (
    df_fwi.groupby("cell_id")["precipitation"]
    .transform(dias_sin_lluvia)
)

logger.info("Features temporales calculadas.")

# COMMAND ----------

# MAGIC %md ## 7 · Guardar silver_openmeteo

# COMMAND ----------

COLS_SILVER = [
    "cell_id", "date", "is_forecast",
    "temperature_2m", "relative_humidity", "wind_speed_10m", "wind_direction_10m",
    "vpd_kpa", "precipitation", "solar_radiation",
    "soil_moisture_0_7cm", "soil_moisture_28_100cm",
    "subregion_id", "subregion_name", "elevation", "slope", "aspect",
    "dist_road_km", "pop_density_km2",
    "land_cover_cat", "ndvi",
    "mes", "dia_anio",
    "ffmc", "dmc", "isi", "bui", "fwi",
    "dias_secos",
]

df_out = df_fwi[[c for c in COLS_SILVER if c in df_fwi.columns]].copy()

sdf = spark.createDataFrame(df_out)

(
    sdf.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(TABLE_OUTPUT)
)

logger.info(f"Guardado: {TABLE_OUTPUT}  ({len(df_out):,} filas)")
logger.info(f"  Histórico (is_forecast=False): {(~df_out['is_forecast']).sum():,}")
logger.info(f"  Forecast  (is_forecast=True):  {df_out['is_forecast'].sum():,}")

# COMMAND ----------

# MAGIC %md ## Verificación

# COMMAND ----------

df_check = spark.read.table(TABLE_OUTPUT)
df_check.groupBy("is_forecast").count().show()
df_check.selectExpr(
    "min(date) as fecha_min",
    "max(date) as fecha_max",
    "count(distinct cell_id) as nodos"
).show()
