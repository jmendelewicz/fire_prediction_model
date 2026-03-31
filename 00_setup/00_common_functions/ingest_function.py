# Databricks notebook source
# MAGIC %md
# MAGIC #00a - Common Functions utilizadas en el proceso de ETL. 
# MAGIC
# MAGIC Se importa desde otros notebooks con: %run ./00_ingest_function
# MAGIC
# MAGIC **Funciones disponibles:**
# MAGIC 1. `procesar_a_bronze()` — ingesta CSV o JSON desde volumen Landing a tabla Delta Bronze

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
    formato: str,   # "csv" o "json"
) -> None:
    """
    Ingesta incremental desde Landing a Bronze usando Databricks Auto Loader.

    Lee archivos CSV o JSON desde un volumen de Unity Catalog, agrega
    metadatos de ingesta (timestamp + path de origen) y escribe en una
    tabla Delta en modo append.

    Args:
        ruta_origen:   Path del volumen landing  (ej. /Volumes/catalog/schema/vol)
        nombre_tabla:  Tabla Delta destino        (ej. catalog.schema.tabla)
        checkpoint:    Path para el checkpoint de Structured Streaming
        ruta_schema:   Path donde Auto Loader persiste el schema inferido
        formato:       Formato del archivo fuente: "csv" o "json"
    """
    logger.info(f"Inicio ingesta → {nombre_tabla} | origen: {ruta_origen}")

    try:
        df_raw = (
            spark.readStream
            .format("cloudFiles")
            .option("cloudFiles.format", formato)
            .option("pathGlobFilter", f"*.{formato}")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "rescue")
            .option("cloudFiles.schemaLocation", ruta_schema)
            .option("header", "true")       # aplica para CSV
            .option("multiline", "true")    # aplica para JSON
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
        logger.info(f"Completado: {nombre_tabla}")

    except Exception as e:
        logger.error(f"Error en {nombre_tabla}: {e}")
        logger.warning("Tabla salteada, continuar con la siguiente")

# COMMAND ----------

logger.info("funciones cargads")
