# Databricks notebook source
# MAGIC %md
# MAGIC # ETL Landing - Open Meteo Seed (Histórico 35 días)
# MAGIC
# MAGIC **Ejecutar UNA SOLA VEZ** antes de la primera inferencia,
# MAGIC o para resetear el historial del pipeline diario.
# MAGIC
# MAGIC Descarga 35 días históricos y los guarda en la tabla Delta `forecast_seed`.

# COMMAND ----------

# Fix A-5 (2026-05-16): paths relativos.
# MAGIC %run ../../00_setup/00_common_functions/openmeteo_client

# COMMAND ----------

# MAGIC %run ../../00_setup/00_common_functions/weather_cleaners

# COMMAND ----------

import pandas as pd
from pyspark.sql import functions as F
import pandas as pd
import os

CATALOG    = "fire_risk_project"
TABLE_GRID = f"{CATALOG}.`00_landing`.aux_grid_pampa"
PATH_OUT      = f"/Volumes/{CATALOG}/00_landing/open_meteo_forecast/seed"
PAST_DAYS  = 35
BATCH_SIZE = 100

filename = f"seed.csv"
filepath = f"{PATH_OUT}/{filename}"

# COMMAND ----------

# MAGIC %md ## Idempotencia

# COMMAND ----------

if os.path.exists(filepath):
    print(f"Archivo ya existe: {filepath} — omitiendo extracción.")
    dbutils.notebook.exit(f"SKIP: {filename} ya existe.")

print(f"Iniciando extracción → {filename}")

# COMMAND ----------

# MAGIC %md ## Extracción

# COMMAND ----------

df_grid = (
    spark.table(TABLE_GRID)
    .filter("is_valid = true")
    .select("cell_id", "latitude", "longitude")
    .toPandas()
)
print(f"Nodos: {len(df_grid)}")

df_hourly = run_batched_extraction(         # ← openmeteo_client
    df_grid       = df_grid,
    forecast_days = 0,
    past_days     = PAST_DAYS,
    batch_size    = BATCH_SIZE,
    sleep_between = 10,
)

df_seed = aggregate_hourly_to_daily(df_hourly)   # ← weather_cleaners
df_seed = clip_climate_df(df_seed)               # ← weather_cleaners
df_seed["date"] = pd.to_datetime(df_seed["date"])

print(f"Seed: {len(df_seed):,} filas | {df_seed['cell_id'].nunique()} nodos | {df_seed['date'].nunique()} días")
print(f"Rango: {df_seed['date'].min().date()} → {df_seed['date'].max().date()}")

# COMMAND ----------

# MAGIC %md ## Guardar

# COMMAND ----------

os.makedirs(PATH_OUT, exist_ok=True)
df_seed.to_csv(filepath, index=False)
print(f"Guardado: {filepath}")

dbutils.notebook.exit(
    f"OK: {filename} | {len(df_seed):,} filas | "
    f"{df_seed['cell_id'].nunique()} nodos | "
    f"{df_seed['date'].nunique()} días"
)
