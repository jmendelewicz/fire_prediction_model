# Databricks notebook source
# MAGIC %md
# MAGIC # Gold Forecast — Pipeline Diario (Job2)
# MAGIC
# MAGIC Toma `silver_openmeteo` (39 días por nodo: 35 histórico + 4 forecast)
# MAGIC y genera la tabla `forecast_gold_temp` con las mismas 35 features del modelo.
# MAGIC Esta tabla se sobreescribe completamente en cada ejecución diaria.
# MAGIC
# MAGIC **Input:**  `02_silver.silver_openmeteo`  (ya tiene FWI calculado)
# MAGIC **Output:** `03_gold.forecast_gold_temp`   (solo días de forecast, is_forecast=True)
# MAGIC
# MAGIC El modelo XGBoost (model_v2) consume `forecast_gold_temp` para producir
# MAGIC predicciones de riesgo de incendio para los próximos 4 días.

# COMMAND ----------

import logging
from pyspark.sql import functions as F
from pyspark.sql import Window
from pyspark.sql.types import IntegerType

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    force=True
)
logger = logging.getLogger("GOLD_FORECAST")

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

CATALOG = "fire_risk_project"

TABLE_INPUT  = f"{CATALOG}.02_silver.silver_openmeteo"
TABLE_OUTPUT = f"{CATALOG}.03_gold.forecast_gold_temp"

# Fecha de corte: hoy (igual que en transform_openmeteo.py)
FECHA_CORTE = F.current_date()

FINAL_COLS = [
    "cell_id", "date",
    "subregion_id", "elevation", "slope", "aspect",
    "dist_road_km", "land_cover_cat", "pop_density_km2",
    "mes_sin", "mes_cos", "dia_sin", "dia_cos", "calendario_agricola",
    "temperature_2m", "relative_humidity", "wind_speed_10m",
    "precipitation", "solar_radiation",
    "soil_moisture_0_7cm", "soil_moisture_28_100cm",
    "ndvi", "vpd_kpa",
    "ffmc", "dmc", "bui", "isi", "fwi",
    "dias_secos", "spi_90d",
    "fwi_roll14", "fwi_roll30",
    "temperature_2m_roll30", "wind_speed_10m_roll30",
]

# COMMAND ----------

# MAGIC %md ## 1 · Leer silver_openmeteo (completo: seed + forecast)

# COMMAND ----------

df = spark.read.table(TABLE_INPUT)

logger.info(f"Silver Openmeteo: {df.count():,} filas")
logger.info(f"  Histórico: {df.filter(~F.col('is_forecast')).count():,}")
logger.info(f"  Forecast:  {df.filter( F.col('is_forecast')).count():,}")

# COMMAND ----------

# MAGIC %md ## 2 · Features temporales (rolling y estacionalidad)
# MAGIC
# MAGIC Se calculan sobre la ventana completa (seed + forecast), luego se filtra
# MAGIC solo el segmento de forecast para guardar. Esto garantiza que el rolling
# MAGIC tiene historia suficiente (no arrancan con pocos días).

# COMMAND ----------

import numpy as np

# Columna de fecha como unix_date para Window
df = df.withColumn("date_col", F.to_date("date"))

w = (
    Window.partitionBy("cell_id")
    .orderBy(F.unix_date(F.col("date_col")))
)

# Estacionalidad circular
df = (
    df
    .withColumn("mes_sin", F.sin(2 * np.pi * F.month("date_col") / 12))
    .withColumn("mes_cos", F.cos(2 * np.pi * F.month("date_col") / 12))
    .withColumn("dia_sin", F.sin(2 * np.pi * F.dayofyear("date_col") / 365))
    .withColumn("dia_cos", F.cos(2 * np.pi * F.dayofyear("date_col") / 365))
)

# Calendario agrícola
df = df.withColumn(
    "calendario_agricola",
    F.when(
        (F.col("land_cover_cat") == 1) &
        F.month("date_col").isin([2, 3, 4, 11, 12]),
        F.lit(1)
    ).otherwise(F.lit(0)).cast(IntegerType())
)

# SPI-90d: Standardized Precipitation Index, ventana 90 días
w_spi = w.rowsBetween(-89, 0)
df = (
    df
    .withColumn("_p_mean", F.mean("precipitation").over(w_spi))
    .withColumn("_p_std",  F.stddev("precipitation").over(w_spi))
    .withColumn(
        "spi_90d",
        F.when(F.col("_p_std") > 0,
               (F.col("precipitation") - F.col("_p_mean")) / F.col("_p_std"))
         .otherwise(F.lit(0.0))
    )
    .drop("_p_mean", "_p_std")
)

# Rolling means FWI y clima
df = (
    df
    .withColumn("fwi_roll14",
        F.mean("fwi").over(w.rowsBetween(-13, 0)))
    .withColumn("fwi_roll30",
        F.mean("fwi").over(w.rowsBetween(-29, 0)))
    .withColumn("temperature_2m_roll30",
        F.mean("temperature_2m").over(w.rowsBetween(-29, 0)))
    .withColumn("wind_speed_10m_roll30",
        F.mean("wind_speed_10m").over(w.rowsBetween(-29, 0)))
)

logger.info("Features temporales calculadas.")

# COMMAND ----------

# MAGIC %md ## 3 · Filtrar solo días de forecast

# COMMAND ----------

df_forecast = df.filter(F.col("is_forecast") == True)

logger.info(f"Días de forecast (salida): {df_forecast.count():,} filas")

# COMMAND ----------

# MAGIC %md ## 4 · Validar columnas y guardar

# COMMAND ----------

# Verificar que todas las columnas existen
missing = [c for c in FINAL_COLS if c not in df_forecast.columns]
if missing:
    raise ValueError(f"Columnas faltantes en forecast_gold_temp: {missing}")

df_final = df_forecast.select(FINAL_COLS).orderBy("cell_id", "date")

# Sobreescribir completo — se actualiza diariamente
(
    df_final.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(TABLE_OUTPUT)
)

logger.info(f"Tabla guardada: {TABLE_OUTPUT}")

# COMMAND ----------

# MAGIC %md ## Verificación

# COMMAND ----------

df_check = spark.read.table(TABLE_OUTPUT)
total    = df_check.count()

print(f"REVISIÓN — {TABLE_OUTPUT}")
print(f"Filas:         {total:,}  (esperado: ~{2266 * 4:,} = 2266 nodos × 4 días)")
print(f"Columnas:      {len(df_check.columns)} / {len(FINAL_COLS)} esperadas")
print(f"Nodos únicos:  {df_check.select('cell_id').distinct().count():,}")

fechas = df_check.agg(
    F.min("date").alias("desde"),
    F.max("date").alias("hasta")
).collect()[0]
print(f"Fechas forecast: {fechas['desde']} → {fechas['hasta']}")

print("\nFWI promedio por día:")
df_check.groupBy("date").agg(
    F.round(F.mean("fwi"), 2).alias("fwi_medio"),
    F.round(F.max("fwi"),  2).alias("fwi_max"),
    F.count("*").alias("nodos")
).orderBy("date").show(10)

dbutils.notebook.exit(f"OK: forecast_gold_temp — {total:,} filas | {fechas['desde']} → {fechas['hasta']}")
