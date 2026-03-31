# Databricks notebook source
# MAGIC %md
# MAGIC Gold Training - Parte 2: Rolling + Delta + Export
# MAGIC
# MAGIC Lee el checkpoint CSV de la Parte 1, calcula las features de ventana
# MAGIC temporal en Spark y guarda la tabla Delta final con las 35 columnas
# MAGIC del modelo.
# MAGIC
# MAGIC **Features calculadas aquí:**
# MAGIC - `calendario_agricola` — flag cultivo × mes de cosecha/quema
# MAGIC - `spi_90d` — índice de precipitación estandarizado 90 días
# MAGIC - `fwi_roll14`, `fwi_roll30` — rolling mean FWI
# MAGIC - `temperature_2m_roll30`, `wind_speed_10m_roll30` — rolling mean climático
# MAGIC

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
logger = logging.getLogger("GOLD_P2")

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

PATH_CHECKPOINT = "/Volumes/fire_risk_project/03_gold/training_dataset_v2/gold_checkpoint.csv"
PATH_CSV_EXPORT = "/Volumes/fire_risk_project/03_gold/training_dataset_v2/training_dataset_v2.csv"
TABLE_OUTPUT    = "fire_risk_project.03_gold.training_dataset_v2"

FINAL_COLS = [
    "cell_id", "fecha_join",
    "fire_occurred",
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

# MAGIC %md ## Leer checkpoint

# COMMAND ----------

logger.info(f"Leyendo checkpoint: {PATH_CHECKPOINT}")

df = (
    spark.read
    .option("header", "true")
    .option("inferSchema", "true")
    .csv(PATH_CHECKPOINT)
    .withColumn("cell_id",    F.col("cell_id").cast("string"))
    .withColumn("fecha_join", F.to_date("fecha_join"))
)

logger.info(f"Checkpoint: {df.count():,} filas, {len(df.columns)} columnas")

# COMMAND ----------

# MAGIC %md ## Features de ventana temporal

# COMMAND ----------

# Window por nodo, ordenado por fecha
w = (
    Window.partitionBy("cell_id")
    .orderBy(F.unix_date(F.col("fecha_join")))
)

# Calendario agrícola
df = df.withColumn(
    "calendario_agricola",
    F.when(
        (F.col("land_cover_cat") == 1) &
        F.month("fecha_join").isin([2, 3, 4, 11, 12]),
        F.lit(1)
    ).otherwise(F.lit(0)).cast(IntegerType())
)

# SPI-90d — índice de precipitación estandarizado (90 días)
# No es el SPI estándar (requiere 30 años climatología) pero captura
# anomalías relativas de sequía/humedad dentro del período disponible.
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

# Rolling means
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

logger.info("Features de ventana calculadas.")

# COMMAND ----------

# MAGIC %md ## Selección final y validación

# COMMAND ----------

# Verificar que todas las columnas existen
missing = [c for c in FINAL_COLS if c not in df.columns]
if missing:
    raise ValueError(f"Columnas faltantes: {missing}")

df_final = df.select(FINAL_COLS).orderBy("cell_id", "fecha_join")

total = df_final.count()
logger.info(f"Dataset final: {total:,} filas × {len(FINAL_COLS)} columnas")

# Verificar nulos
null_exprs = [
    F.count(F.when(F.col(c).isNull(), c)).alias(c)
    for c in FINAL_COLS if c not in ["cell_id", "fecha_join"]
]
null_row = df_final.select(null_exprs).collect()[0].asDict()
nulos    = {k: v for k, v in null_row.items() if v > 0}
if nulos:
    logger.warning(f"Columnas con nulos: {nulos}")
else:
    logger.info("Sin nulos en ninguna feature")

# COMMAND ----------

# MAGIC %md ## Guardar tabla Delta

# COMMAND ----------

(
    df_final.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(TABLE_OUTPUT)
)

spark.sql(f"OPTIMIZE {TABLE_OUTPUT} ZORDER BY (cell_id, fecha_join)")
logger.info(f"Tabla Delta guardada y optimizada: {TABLE_OUTPUT}")

# COMMAND ----------

# MAGIC %md ## Exportar CSV para entrenamiento local

# COMMAND ----------

(
    spark.table(TABLE_OUTPUT)
    .coalesce(1)
    .write
    .mode("overwrite")
    .option("header", "true")
    .csv(PATH_CSV_EXPORT)
)

logger.info(f"CSV exportado: {PATH_CSV_EXPORT}")

# COMMAND ----------

# MAGIC %md ## Revisiòn

# COMMAND ----------

df_check = spark.read.table(TABLE_OUTPUT)
total    = df_check.count()

print(f"REVISIÓN — {TABLE_OUTPUT}")
print(f"Filas: {total:,}")
print(f"Columnas: {len(df_check.columns)} (esperado: {len(FINAL_COLS)})")
print(f"Nodos únicos: {df_check.select('cell_id').distinct().count():,}")

fechas = df_check.agg(
    F.min("fecha_join").alias("desde"),
    F.max("fecha_join").alias("hasta")
).collect()[0]
print(f"Fechas: {fechas['desde']} a {fechas['hasta']}")

print("\nDistribución target:")
df_check.groupBy("fire_occurred").count() \
    .withColumn("pct", F.round(F.col("count") / total * 100, 2)) \
    .orderBy("fire_occurred").show()

print("Distribución por año:")
df_check.groupBy(F.year("fecha_join").alias("anio")).agg(
    F.count("*").alias("registros"),
    F.sum("fire_occurred").alias("incendios"),
    F.round(F.mean("fwi"), 2).alias("fwi_medio")
).orderBy("anio").show()

print("FWI por subregión:")
df_check.groupBy("subregion_id").agg(
    F.round(F.mean("fwi"), 2).alias("fwi_medio"),
    F.round(F.max("fwi"),  2).alias("fwi_max"),
    F.sum("fire_occurred").alias("incendios")
).orderBy("subregion_id").show()

print(f"\nDataset listo para XGBoost. Fin del proceso ETL. Fin de arquitectura Medallion")
