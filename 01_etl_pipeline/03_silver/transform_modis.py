# Databricks notebook source
# MAGIC %md
# MAGIC # Silver - MODIS (NDVI + Land Cover)
# MAGIC
# MAGIC Procesa las dos fuentes MODIS desde los CSV de Landing.
# MAGIC
# MAGIC **NDVI (MOD13A2):**
# MAGIC - Compuesto 16 días → forward-fill diario para cubrir todo el período.
# MAGIC - Nulos iniciales imputados con mediana global.
# MAGIC
# MAGIC **Land Cover (MCD12Q1):**
# MAGIC - Anual (2022-2024). Ingesta desde Bronze (`bronze_land_cover`).
# MAGIC - Categorías: 0=Otro/Urbano, 1=Cultivo, 2=Vegetación Natural.
# MAGIC - **Provider-agnostic:** las transformaciones normalizan los datos
# MAGIC
# MAGIC **Nota:** dist_road_km y pop_density_km2 ya están en aux_grid_pampa y se propagan
# MAGIC vía silver_era5. No se procesan aquí.

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql import Window
from pyspark.sql.types import DoubleType, IntegerType, StringType
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    force=True
)
logger = logging.getLogger("SILVER_MODIS")

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

CATALOG = "fire_risk_project"

PATH_NDVI  = f"/Volumes/{CATALOG}/00_landing/modis_ndvi/ndvi_2022_2024.csv"
TABLE_LC_BRONZE = f"{CATALOG}.01_bronze.bronze_land_cover"

TABLE_NDVI = f"{CATALOG}.02_silver.silver_ndvi"
TABLE_LC   = f"{CATALOG}.02_silver.silver_land_cover"

FECHA_FIN = "2024-12-31"

# COMMAND ----------

# MAGIC %md ## NDVI (MOD13A2 — compuesto 16 días → forward-fill diario)

# COMMAND ----------

logger.info("Procesando NDVI")

df_ndvi_raw = (
    spark.read
    .option("header", "true")
    .option("inferSchema", "true")
    .csv(PATH_NDVI)
)

df_ndvi = (
    df_ndvi_raw
    .withColumn("cell_id", F.col("cell_id").cast(StringType()))
    .withColumn("fecha",   F.to_date(F.col("fecha"), "yyyy-MM-dd"))
    .withColumn("ndvi",    F.col("ndvi").cast(DoubleType()))
    .withColumn("ndvi", F.when(F.col("ndvi").between(-1.0, 1.0), F.col("ndvi")))
    .dropDuplicates(["cell_id", "fecha"])
)

logger.info(f"NDVI raw: {df_ndvi.count():,} filas | "
            f"nulos: {df_ndvi.filter(F.col('ndvi').isNull()).count():,}")

# COMMAND ----------

# MAGIC %md ## Generación del calendario diario + forward-fill

# COMMAND ----------

fecha_min = df_ndvi.agg(F.min("fecha")).collect()[0][0]

date_seq = spark.sql(f"""
    SELECT explode(sequence(DATE('{fecha_min}'), DATE('{FECHA_FIN}'), INTERVAL 1 DAY)) AS fecha
""")
cells    = df_ndvi.select("cell_id").distinct()
calendar = cells.crossJoin(date_seq)

df_joined = calendar.join(df_ndvi, on=["cell_id", "fecha"], how="left")

w_ffill = (
    Window
    .partitionBy("cell_id")
    .orderBy("fecha")
    .rowsBetween(Window.unboundedPreceding, 0)
)

df_ffill = df_joined.withColumn(
    "ndvi", F.last(F.col("ndvi"), ignorenulls=True).over(w_ffill)
)

ndvi_median   = df_ffill.approxQuantile("ndvi", [0.5], 0.01)[0]
df_ndvi_final = (
    df_ffill
    .fillna({"ndvi": ndvi_median})
    .select("cell_id", "fecha", "ndvi")
    .withColumn("_processed_at", F.current_timestamp())
    .orderBy("cell_id", "fecha")
)

logger.info(f"NDVI final: {df_ndvi_final.count():,} filas")

# COMMAND ----------

(
    df_ndvi_final.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(TABLE_NDVI)
)
logger.info(f"Guardado: {TABLE_NDVI}")

# COMMAND ----------

# MAGIC %md ## Land Cover (MCD12Q1 — anual)
# MAGIC
# MAGIC Lee desde Bronze (`bronze_land_cover`) — no directamente del CSV.
# MAGIC Transformaciones provider-agnostic: si se cambia el proveedor de datos
# MAGIC (ej. de MODIS a Copernicus), solo hace falta adaptar el extract + ingest.
# MAGIC Las transformaciones aquí normalizan al schema Silver esperado.

# COMMAND ----------

logger.info("Procesando Land Cover desde Bronze")

df_lc_raw = spark.read.table(TABLE_LC_BRONZE)

df_lc = (
    df_lc_raw
    .withColumn("cell_id",         F.col("cell_id").cast(StringType()))
    .withColumn("year",            F.col("year").cast(IntegerType()))
    .withColumn("land_cover_type", F.col("land_cover_type").cast(IntegerType()))
    .withColumn("land_cover_cat",  F.col("land_cover_cat").cast(IntegerType()))
    .withColumn("land_cover_cat",
        F.when(F.col("land_cover_cat").isin(0, 1, 2), F.col("land_cover_cat"))
         .otherwise(0)
    )
    .fillna({"land_cover_cat": 0})
    .select("cell_id", "year", "land_cover_type", "land_cover_cat")
    .dropDuplicates(["cell_id", "year"])
    .withColumn("_processed_at", F.current_timestamp())
)

logger.info(f"Land Cover: {df_lc.count():,} registros")
df_lc.groupBy("land_cover_cat").count().orderBy("land_cover_cat").show()

(
    df_lc.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(TABLE_LC)
)
logger.info(f"Guardado: {TABLE_LC}")
