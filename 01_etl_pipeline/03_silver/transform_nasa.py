# Databricks notebook source
# MAGIC %md
# MAGIC # Silver - NASA FIRMS
# MAGIC
# MAGIC Limpieza y normalización de los focos VIIRS para la región pampeana.
# MAGIC
# MAGIC **Transformaciones:**
# MAGIC - Cast de tipos
# MAGIC - Filtro confidence n/h (estándar industria)
# MAGIC - Normalización timestamp UTC
# MAGIC - Deduplicación
# MAGIC - Asignación cell_id (grilla 0.25°)
# MAGIC - Filtro bounding box pampeano
# MAGIC - Inner join contra aux_grid_pampa (garantiza cell_id válido)

# COMMAND ----------

from pyspark.sql.functions import (
    col, round, concat, format_number, lit,
    expr, to_timestamp, hour
)
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    force=True
)
logger = logging.getLogger("SILVER_NASA")

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

TABLE_BRONZE = "fire_risk_project.01_bronze.bronze_nasa_firms"
TABLE_SILVER = "fire_risk_project.02_silver.silver_nasa_firms"
TABLE_GRID   = "fire_risk_project.00_landing.aux_grid_pampa"

LAT_MIN, LAT_MAX = -42.0, -28.0
LON_MIN, LON_MAX = -68.0, -56.0
STEP             = 0.25

# COMMAND ----------

# MAGIC %md ## Cargar datos

# COMMAND ----------

df_bronze = spark.read.table(TABLE_BRONZE)
df_grid   = spark.read.table(TABLE_GRID).select("cell_id")

logger.info(f"Bronze NASA: {df_bronze.count():,} registros")

# COMMAND ----------

# MAGIC %md ## Transformaciones

# COMMAND ----------

df_silver = (
    df_bronze

    .withColumn("latitude",  col("latitude").cast("double"))
    .withColumn("longitude", col("longitude").cast("double"))

    .filter(col("confidence").isin(["n", "h"]))

    .withColumn("time_str", expr("lpad(cast(acq_time as string), 4, '0')"))

    .withColumn("timestamp_incendio", to_timestamp(
        expr("concat(cast(acq_date as string), ' ', "
             "substr(time_str, 1, 2), ':', "
             "substr(time_str, 3, 2), ':00')"),
        "yyyy-MM-dd HH:mm:ss"
    ))

    .dropDuplicates(["acq_date", "acq_time", "latitude", "longitude"])

    .withColumn("grid_lat", round(round(col("latitude")  / STEP) * STEP, 4))
    .withColumn("grid_lon", round(round(col("longitude") / STEP) * STEP, 4))
    .withColumn("cell_id", concat(
        format_number(col("grid_lat"), 4), lit("_"),
        format_number(col("grid_lon"), 4)
    ))

    .withColumn("fecha_join", col("acq_date"))
    .withColumn("hora_join",  hour(col("timestamp_incendio")))

    .filter(
        (col("grid_lat") >= LAT_MIN) & (col("grid_lat") <= LAT_MAX) &
        (col("grid_lon") >= LON_MIN) & (col("grid_lon") <= LON_MAX)
    )
)

df_silver = df_silver.join(df_grid, on="cell_id", how="inner")

from pyspark.sql.functions import current_timestamp
df_silver = df_silver.withColumn("_processed_at", current_timestamp())

logger.info(f"Silver NASA: {df_silver.count():,} registros")

# COMMAND ----------

# MAGIC %md ## Guardado

# COMMAND ----------

(
    df_silver.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(TABLE_SILVER)
)

logger.info(f"Tabla guardada: {TABLE_SILVER}")

# COMMAND ----------

# MAGIC %md ## Verificación

# COMMAND ----------

df_check = spark.read.table(TABLE_SILVER)

print(f"Total focos silver: {df_check.count():,}")

print("\nDistribución por año:")
df_check.groupBy(expr("year(acq_date)").alias("anio")) \
    .count().orderBy("anio").show()

print("Distribución por confidence:")
df_check.groupBy("confidence").count().orderBy("confidence").show()

print("Rango coordenadas:")
df_check.selectExpr(
    "min(latitude)  as lat_min",
    "max(latitude)  as lat_max",
    "min(longitude) as lon_min",
    "max(longitude) as lon_max"
).show()

n_sin_nodo = df_check.join(spark.read.table(TABLE_GRID), on="cell_id", how="left_anti").count()
print(f"Focos sin nodo válido: {n_sin_nodo}  {'Correcto' if n_sin_nodo == 0 else 'Revisar'}")
