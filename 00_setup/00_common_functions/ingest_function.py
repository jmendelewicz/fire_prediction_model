# Databricks notebook source
# MAGIC %md # Bronze Ingest Function

# COMMAND ----------

from pyspark.sql.functions import current_timestamp, col
import logging

# COMMAND ----------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    datefmt='%H:%M:%S',
    force=True
)
logger = logging.getLogger("ETL_BRONZE")

# COMMAND ----------

def procesar_a_bronze(
    ruta_origen: str,
    nombre_tabla: str,
    checkpoint: str,
    ruta_schema: str,
    formato: str,
) -> None:
    logger.info(f"INGEST START TABLE {nombre_tabla} SOURCE {ruta_origen}")

    try:
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

        df_con_metadata = (
            df_raw
            .withColumn("ingestion_timestamp", current_timestamp())
            .withColumn("source_filename", col("_metadata.file_path"))
        )

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
        logger.info(f"INGEST DONE TABLE {nombre_tabla}")

    except Exception as e:
        logger.error(f"INGEST FAILED TABLE {nombre_tabla} REASON {e}")
        logger.warning("TABLE SKIPPED MOVING TO NEXT ONE")

# COMMAND ----------

logger.info("BRONZE INGEST FUNCTION LOADED")
