# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Bronze — ERA5 GEE
# MAGIC
# MAGIC Ingesta los 36 CSV de ERA5 al catálogo Delta usando Auto Loader.
# MAGIC
# MAGIC **Input:**  /Volumes/fire_risk_project/00_landing/era5_files/*.csv
# MAGIC **Output:** fire_risk_project.01_bronze.bronze_era5

# COMMAND ----------

from pyspark.sql.functions import col, current_timestamp
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    force=True
)
logger = logging.getLogger("BRONZE_ERA5")

# COMMAND ----------

# MAGIC %md ## 1 · Parámetros

# COMMAND ----------

RUTA_ORIGEN  = "/Volumes/fire_risk_project/00_landing/era5_files_v2"
NOMBRE_TABLA = "fire_risk_project.01_bronze.bronze_era5_v2"
CHECKPOINT   = "/Volumes/fire_risk_project/00_landing/era5_files_v2/_checkpoint_era5_v2"
RUTA_SCHEMA  = "/Volumes/fire_risk_project/00_landing/era5_files_v2/_schema_era5_v2"
FORMATO      = "csv"

# COMMAND ----------

# MAGIC %md ## 2 · Limpiar checkpoint previo (si existe)
# MAGIC
# MAGIC Necesario si se re-ejecuta desde cero. Si es la primera vez, no hace nada.

# COMMAND ----------

try:
    dbutils.fs.rm(CHECKPOINT, recurse=True)
    logger.info(f"Checkpoint eliminado: {CHECKPOINT}")
except Exception:
    logger.info("No habia checkpoint previo.")

try:
    dbutils.fs.rm(RUTA_SCHEMA, recurse=True)
    logger.info(f"Schema location eliminado: {RUTA_SCHEMA}")
except Exception:
    logger.info("No habia schema location previo.")

# COMMAND ----------

# MAGIC %md ## 3 · Función de ingesta

# COMMAND ----------

def procesar_a_bronze(ruta_origen, nombre_tabla, checkpoint, ruta_schema, formato):
    logger.info(f"Inicio: {nombre_tabla}")
    logger.info(f"  Origen:     {ruta_origen}")
    logger.info(f"  Checkpoint: {checkpoint}")

    try:
        # Lectura con Auto Loader
        df_raw = (
            spark.readStream
            .format("cloudFiles")
            .option("cloudFiles.format", formato)
            .option("pathGlobFilter", f"*.{formato}")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "rescue")
            .option("cloudFiles.schemaLocation", ruta_schema)
            .option("header", "true")
            .option("multiline", "true")
            .load(ruta_origen)
        )

        # Agregar metadatos de ingesta
        df_con_metadata = (
            df_raw
            .withColumn("ingestion_timestamp", current_timestamp())
            .withColumn("source_filename", col("_metadata.file_path"))
        )

        # Escritura a tabla Delta
        query = (
            df_con_metadata.writeStream
            .format("delta")
            .outputMode("append")
            .option("checkpointLocation", checkpoint)
            .option("mergeSchema", "true")
            .trigger(availableNow=True)
            .table(nombre_tabla)
        )

        query.awaitTermination()
        logger.info(f"Terminado: {nombre_tabla}")

    except Exception as e:
        logger.error(f"Error en {nombre_tabla}: {e}")
        logger.warning("Tabla salteada")

logger.info("Funcion de ingesta preparada")

# COMMAND ----------

# MAGIC %md ## 4 · Ejecutar ingesta

# COMMAND ----------

procesar_a_bronze(
    ruta_origen  = RUTA_ORIGEN,
    nombre_tabla = NOMBRE_TABLA,
    checkpoint   = CHECKPOINT,
    ruta_schema  = RUTA_SCHEMA,
    formato      = FORMATO
)

# COMMAND ----------

# MAGIC %md ## 5 · Verificación

# COMMAND ----------

df_bronze = spark.read.table(NOMBRE_TABLA)

print(f"Total registros:  {df_bronze.count():,}")
print(f"Total columnas:   {len(df_bronze.columns)}")
print(f"\nColumnas: {df_bronze.columns}")

print("\nDistribucion por año:")
df_bronze.selectExpr("substring(date, 1, 4) as anio") \
    .groupBy("anio").count().orderBy("anio").show()

print("Nulos por columna:")
from pyspark.sql.functions import col, sum as spark_sum, round as spark_round
total = df_bronze.count()
nulos = df_bronze.select([
    spark_round(spark_sum(col(c).isNull().cast("int")) / total * 100, 2)
    .alias(c) for c in df_bronze.columns
    if c not in ["ingestion_timestamp", "source_filename", "_rescued_data"]
])
nulos.show(truncate=False)

print("\nPrimeras filas:")
df_bronze.show(3, truncate=False)


%sql
SELECT * FROM fire_risk_project.`01_bronze`.bronze_era5 LIMIT 10;