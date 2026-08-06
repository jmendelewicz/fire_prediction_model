# Databricks notebook source
# MAGIC %md
# MAGIC # ETL Landing - Open Meteo Seed (Histórico 35 días)
# MAGIC
# MAGIC Descarga los últimos 35 días de historia meteorológica y los guarda como
# MAGIC `seed.csv` en Landing. **Corre a diario como parte del Job2**: la ventana
# MAGIC deslizante de bronze (MERGE + DELETE > 35 días) solo funciona si este
# MAGIC extract aporta días nuevos cada día.
# MAGIC
# MAGIC *Fix 2026-07-09:* la versión anterior se salteaba si `seed.csv` ya
# MAGIC existía ("una sola vez"). Con eso, el MERGE diario nunca agregaba días
# MAGIC nuevos y el DELETE de la ventana iba vaciando el seed hasta dejarlo en
# MAGIC cero (~35 días después del setup), dejando al FWI sin spin-up histórico.

# COMMAND ----------

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

# MAGIC %md ## Extracción diaria
# MAGIC
# MAGIC Se sobreescribe `seed.csv` en cada corrida (idempotente: correr dos veces
# MAGIC el mismo día produce el mismo archivo). El costo es una pasada por la
# MAGIC API gratuita de Open-Meteo (~23 lotes de 100 nodos).

# COMMAND ----------

print(f"Iniciando extracción (últimos {PAST_DAYS} días) → {filename}")

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
    forecast_days = 0,
    past_days     = PAST_DAYS,
    batch_size    = BATCH_SIZE,
    sleep_between = 10,
)

df_seed = aggregate_hourly_to_daily(df_hourly)
df_seed = clip_climate_df(df_seed)
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
