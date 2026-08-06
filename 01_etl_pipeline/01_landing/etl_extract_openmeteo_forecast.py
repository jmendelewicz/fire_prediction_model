# Databricks notebook source
# MAGIC %md
# MAGIC # ETL Landing - Open Meteo Forecast (4 días)
# MAGIC
# MAGIC Extrae los 4 días de pronóstico para los 2,266 nodos.
# MAGIC Compatible con Databricks Serverless — sin dependencias externas.
# MAGIC Idempotente: si el archivo de hoy ya existe, omite la extracción.

# COMMAND ----------

# MAGIC %run ../../00_setup/00_common_functions/openmeteo_client

# COMMAND ----------

# MAGIC %run ../../00_setup/00_common_functions/weather_cleaners

# COMMAND ----------

import os
import pandas as pd

CATALOG       = "fire_risk_project"
TABLE_GRID    = f"{CATALOG}.`00_landing`.aux_grid_pampa"
PATH_OUT      = f"/Volumes/{CATALOG}/00_landing/open_meteo_forecast/forecast"
FORECAST_DAYS = 4
BATCH_SIZE    = 100

today    = pd.Timestamp.now().strftime("%Y%m%d")
filename = f"forecast_{today}.csv"
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

df_hourly = run_batched_extraction(
    df_grid       = df_grid,
    forecast_days = FORECAST_DAYS,
    past_days     = 0,
    batch_size    = BATCH_SIZE,
    sleep_between = 10,
)

df_daily = aggregate_hourly_to_daily(df_hourly)
df_daily = clip_climate_df(df_daily)
df_daily["date"] = pd.to_datetime(df_daily["date"])

print(f"Filas: {len(df_daily):,} | Nodos: {df_daily['cell_id'].nunique()} | Fechas: {df_daily['date'].nunique()}")

# COMMAND ----------

# MAGIC %md ## Guardar

# COMMAND ----------

os.makedirs(PATH_OUT, exist_ok=True)
df_daily.to_csv(filepath, index=False)
print(f"Guardado: {filepath}")

dbutils.notebook.exit(
    f"OK: {filename} | {len(df_daily):,} filas | "
    f"{df_daily['cell_id'].nunique()} nodos | "
    f"{df_daily['date'].nunique()} días"
)
