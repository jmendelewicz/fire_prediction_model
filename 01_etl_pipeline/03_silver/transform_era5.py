# Databricks notebook source
# MAGIC %md
# MAGIC # Silver - ERA5 GEE
# MAGIC
# MAGIC Limpieza y normalización de los datos climáticos ERA5-Land.
# MAGIC Los datos llegan planos desde Bronze: una fila por (cell_id, date).
# MAGIC
# MAGIC **Transformaciones:**
# MAGIC - Cast de tipos
# MAGIC - Clip precipitation ≥ 0 (artefacto de suma flotante en GEE)
# MAGIC - Clip relative_humidity [0, 100]
# MAGIC - Clip vpd_kpa ≥ 0
# MAGIC - fecha_join para joins con FIRMS
# MAGIC - Join con aux_grid_pampa para subregion, topografía, dist_road_km y pop_density_km2
# MAGIC - ZORDER por cell_id y fecha_join

# COMMAND ----------

from pyspark.sql.functions import (
    col, to_date, greatest, least, lit
)
from pyspark.sql.types import DoubleType
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    force=True
)
logger = logging.getLogger("SILVER_ERA5")

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

TABLE_BRONZE = "fire_risk_project.01_bronze.bronze_era5"
TABLE_SILVER = "fire_risk_project.02_silver.silver_era5"
TABLE_GRID   = "fire_risk_project.00_landing.aux_grid_pampa"

# COMMAND ----------

# MAGIC %md ## 1 · Cargar datos

# COMMAND ----------

df_bronze = spark.read.table(TABLE_BRONZE)
df_grid   = spark.read.table(TABLE_GRID).select(
    "cell_id", "subregion_id", "subregion_name",
    "elevation", "slope", "aspect",
    "dist_road_km", "pop_density_km2"   # features estáticas incorporadas a la grilla
)

logger.info(f"Bronze ERA5: {df_bronze.count():,} registros")
logger.info(f"Grilla:      {df_grid.count():,} nodos")

# COMMAND ----------

# MAGIC %md ## Transformaciones

# COMMAND ----------

df_silver = (
    df_bronze

    # A. Cast de tipos
    .withColumn("temperature_2m",         col("temperature_2m").cast(DoubleType()))
    .withColumn("relative_humidity",      col("relative_humidity").cast(DoubleType()))
    .withColumn("precipitation",          col("precipitation").cast(DoubleType()))
    .withColumn("wind_speed_10m",         col("wind_speed_10m").cast(DoubleType()))
    .withColumn("wind_direction_10m",     col("wind_direction_10m").cast(DoubleType()))
    .withColumn("vpd_kpa",                col("vpd_kpa").cast(DoubleType()))
    .withColumn("solar_radiation",        col("solar_radiation").cast(DoubleType()))
    .withColumn("soil_moisture_0_7cm",    col("soil_moisture_0_7cm").cast(DoubleType()))
    .withColumn("soil_moisture_28_100cm", col("soil_moisture_28_100cm").cast(DoubleType()))

    # B. Clip de valores físicamente imposibles
    # Precipitación: GEE puede generar -0.00 por aritmética flotante
    .withColumn("precipitation",     greatest(col("precipitation"),    lit(0.0)))
    # Humedad relativa: debe estar en [0, 100]
    .withColumn("relative_humidity", least(greatest(col("relative_humidity"), lit(0.0)), lit(100.0)))
    # VPD: no puede ser negativo
    .withColumn("vpd_kpa",           greatest(col("vpd_kpa"),          lit(0.0)))

    # C. Fecha para join con FIRMS en Gold
    .withColumn("fecha_join", to_date(col("date")))

    # D. Drop columnas de ingesta
    .drop("source_filename", "ingestion_timestamp", "_rescued_data")
)

# E. Join con grilla para subregion y topografía
df_silver = df_silver.join(df_grid, on="cell_id", how="left")

logger.info(f"Silver ERA5: {df_silver.count():,} registros")

# COMMAND ----------

# MAGIC %md ## Selección de columnas

# COMMAND ----------

df_silver = df_silver.select(
    # Claves
    col("cell_id"),
    col("date"),
    col("fecha_join"),
    # Variables climáticas
    col("temperature_2m"),
    col("relative_humidity"),
    col("precipitation"),
    col("wind_speed_10m"),
    col("wind_direction_10m"),
    col("vpd_kpa"),
    col("solar_radiation"),
    col("soil_moisture_0_7cm"),
    col("soil_moisture_28_100cm"),
    # Subregión y topografía desde aux_grid_pampa
    col("subregion_id"),
    col("subregion_name"),
    col("elevation"),
    col("slope"),
    col("aspect"),
    # Features estáticas de infraestructura/población (desde aux_grid_pampa)
    col("dist_road_km"),
    col("pop_density_km2"),
)

# COMMAND ----------

# MAGIC %md ## Guardado y optimización

# COMMAND ----------

(
    df_silver.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(TABLE_SILVER)
)

spark.sql(f"OPTIMIZE {TABLE_SILVER} ZORDER BY (cell_id, fecha_join)")

logger.info(f"Tabla guardada y optimizada: {TABLE_SILVER}")

# COMMAND ----------

# MAGIC %md ## Auditoría

# COMMAND ----------

import pyspark.sql.functions as F

df_check  = spark.read.table(TABLE_SILVER)
total     = df_check.count()
N_NODOS   = 2266
ESPERADO  = N_NODOS * (365 + 365 + 366)   # 2022 + 2023 + 2024 (bisiesto)

print(f"Registros:  {total:,}  (esperado: {ESPERADO:,}, diff: {total - ESPERADO:+,})")
print(f"Nodos únicos: {df_check.select('cell_id').distinct().count():,} / {N_NODOS}")

fechas = df_check.agg(F.min("fecha_join").alias("desde"), F.max("fecha_join").alias("hasta")).collect()[0]
print(f"Fechas: {fechas['desde']} → {fechas['hasta']}")

feature_cols = [
    "temperature_2m", "relative_humidity", "precipitation",
    "wind_speed_10m", "vpd_kpa", "solar_radiation",
    "soil_moisture_0_7cm", "soil_moisture_28_100cm"
]
null_exprs = [F.count(F.when(F.col(c).isNull(), c)).alias(c) for c in feature_cols]
nulls = df_check.select(null_exprs).collect()[0]

print("\nNulos por variable:")
for c in feature_cols:
    pct    = nulls[c] / total * 100
    status = "Correct" if pct == 0 else ("Hay Errores" if pct < 5 else "Error global")
    print(f"  {c:<30} {nulls[c]:6,}  ({pct:.2f}%)  {status}")

print("\nDistribución por año:")
df_check.groupBy(F.year("fecha_join").alias("anio")).count().orderBy("anio").show()
