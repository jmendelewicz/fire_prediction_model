# Databricks notebook source
# MAGIC %md
# MAGIC # Ingesta Landing a Bronze
# MAGIC
# MAGIC Ingesta incremental de todas las fuentes de datos crudos a tablas Delta
# MAGIC usando Databricks Auto Loader (Structured Streaming).
# MAGIC
# MAGIC **Fuentes:** ERA5, NASA FIRMS, MODIS NDVI, MODIS Land Cover, Open-Meteo.
# MAGIC Población y distancia a rutas son features estáticas en `aux_grid_pampa`.

# COMMAND ----------

# MAGIC %run /Workspace/Users/jmendelewicz02@gmail.com/fire_prediction_model/00_setup/00_common_functions/ingest_function

# COMMAND ----------

import os
import shutil

# COMMAND ----------

# MAGIC %md ## Ingesta training

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

# ── Rutas Landing ─────────────────────────────────────────────────────────────
PATH_NASA           = "/Volumes/fire_risk_project/00_landing/nasa_files"
PATH_ERA5           = "/Volumes/fire_risk_project/00_landing/era5_files"
PATH_NDVI           = "/Volumes/fire_risk_project/00_landing/modis_ndvi"
PATH_LC             = "/Volumes/fire_risk_project/00_landing/modis_static"
PATH_METEO_SEED     = "/Volumes/fire_risk_project/00_landing/open_meteo_forecast/seed"
PATH_METEO_FORECAST = "/Volumes/fire_risk_project/00_landing/open_meteo_forecast/forecast"

# ── Tablas Bronze ─────────────────────────────────────────────────────────────
TABLE_NASA       = "fire_risk_project.01_bronze.bronze_nasa_firms"
TABLE_ERA5       = "fire_risk_project.01_bronze.bronze_era5"
TABLE_NDVI       = "fire_risk_project.01_bronze.bronze_modis_ndvi"
TABLE_LC         = "fire_risk_project.01_bronze.bronze_land_cover"
TABLE_METEO_SEED = "fire_risk_project.01_bronze.bronze_openmeteo_seed"
TABLE_METEO_FC   = "fire_risk_project.01_bronze.bronze_openmeteo_forecast"

# ── Checkpoints y schemas — dentro del volumen de procesamiento ───────────────
BASE_PROC = "/Volumes/fire_risk_project/01_bronze/vol_procesamiento"

CP_NASA  = f"{BASE_PROC}/nasa/checkpoint"
CP_ERA5  = f"{BASE_PROC}/era5/checkpoint"
CP_NDVI  = f"{BASE_PROC}/ndvi/checkpoint"
CP_LC    = f"{BASE_PROC}/lc/checkpoint"

SCH_NASA  = f"{BASE_PROC}/nasa/schema"
SCH_ERA5  = f"{BASE_PROC}/era5/schema"
SCH_NDVI  = f"{BASE_PROC}/ndvi/schema"
SCH_LC    = f"{BASE_PROC}/lc/schema"

# COMMAND ----------

# MAGIC %md ## Idempotencia

# COMMAND ----------

def necesita_ingestar(nombre_tabla, checkpoint_path):
    try:
        tiene_datos = spark.table(nombre_tabla).count() > 0
        tiene_checkpoint = os.path.exists(checkpoint_path)
        # Solo saltear si tiene datos Y tiene checkpoint consistente
        return not (tiene_datos and tiene_checkpoint)
    except Exception:
        return True

nasa_needs_ingest = necesita_ingestar(TABLE_NASA, CP_NASA)
era5_needs_ingest = necesita_ingestar(TABLE_ERA5, CP_ERA5)
ndvi_needs_ingest = necesita_ingestar(TABLE_NDVI, CP_NDVI)
lc_needs_ingest   = necesita_ingestar(TABLE_LC,   CP_LC)

# COMMAND ----------

# MAGIC %md ## NASA FIRMS — focos de incendio VIIRS

# COMMAND ----------

if nasa_needs_ingest:
    if os.path.exists(CP_NASA):
        shutil.rmtree(CP_NASA)
        print("Checkpoint NASA borrado -> Auto Loader reprocesará desde cero.")
        
    procesar_a_bronze(
        ruta_origen   = PATH_NASA,
        nombre_tabla  = TABLE_NASA,
        checkpoint    = CP_NASA,
        ruta_schema   = SCH_NASA,
        formato       = "csv",
    )
else:
    print("Tabla NASA ya existe y está consistente — saltando.")

# COMMAND ----------

# MAGIC %md ## ERA5-Land — variables climáticas

# COMMAND ----------

if era5_needs_ingest:
    if os.path.exists(CP_ERA5):
        shutil.rmtree(CP_ERA5)
        print("Checkpoint ERA5 borrado -> Auto Loader reprocesará desde cero.")
        
    procesar_a_bronze(
        ruta_origen   = PATH_ERA5,
        nombre_tabla  = TABLE_ERA5,
        checkpoint    = CP_ERA5,
        ruta_schema   = SCH_ERA5,
        formato       = "csv",
    )
else:
    print("Tabla ERA5 ya existe y está consistente — saltando.")

# COMMAND ----------

# MAGIC %md ## MODIS NDVI

# COMMAND ----------

if ndvi_needs_ingest:
    if os.path.exists(CP_NDVI):
        shutil.rmtree(CP_NDVI)
        print("Checkpoint NDVI borrado -> Auto Loader reprocesará desde cero.")
        
    procesar_a_bronze(
        ruta_origen   = PATH_NDVI,
        nombre_tabla  = TABLE_NDVI,
        checkpoint    = CP_NDVI,
        ruta_schema   = SCH_NDVI,
        formato       = "csv",
    )
else:
    print("Tabla NDVI ya existe y está consistente — saltando.")

# COMMAND ----------

# MAGIC %md ## MODIS Land Cover (MCD12Q1)
# MAGIC
# MAGIC Cobertura del suelo anual. CSV multi-año en `modis_static/`.
# MAGIC Fluye a Silver (`transform_modis.py`) donde se normaliza.

# COMMAND ----------

if lc_needs_ingest:
    if os.path.exists(CP_LC):
        shutil.rmtree(CP_LC)
        print("Checkpoint LC borrado -> Auto Loader reprocesará desde cero.")
        
    procesar_a_bronze(
        ruta_origen   = PATH_LC,
        nombre_tabla  = TABLE_LC,
        checkpoint    = CP_LC,
        ruta_schema   = SCH_LC,
        formato       = "csv",
    )
else:
    print("Tabla Land Cover ya existe y está consistente — saltando.")

# COMMAND ----------

# MAGIC %md ## 5 · Verificación

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT 'NASA FIRMS'  AS fuente, COUNT(*) AS registros FROM fire_risk_project.01_bronze.bronze_nasa_firms
# MAGIC UNION ALL
# MAGIC SELECT 'ERA5'        AS fuente, COUNT(*) AS registros FROM fire_risk_project.01_bronze.bronze_era5
# MAGIC UNION ALL
# MAGIC SELECT 'MODIS NDVI'  AS fuente, COUNT(*) AS registros FROM fire_risk_project.01_bronze.bronze_modis_ndvi
# MAGIC UNION ALL
# MAGIC SELECT 'Land Cover'  AS fuente, COUNT(*) AS registros FROM fire_risk_project.01_bronze.bronze_land_cover;

# COMMAND ----------

# Verificar metadatos de ingesta en cada tabla
for tabla in [TABLE_NASA, TABLE_ERA5, TABLE_NDVI, TABLE_LC]:
    print(f"\n{tabla}:")
    spark.sql(f"""
        SELECT source_filename, ingestion_timestamp
        FROM {tabla}
        ORDER BY ingestion_timestamp DESC
        LIMIT 2
    """).show(truncate=False)

# COMMAND ----------

# MAGIC %md ## Open-Meteo Seed — ventana deslizante 35 días
# MAGIC
# MAGIC El seed se actualiza diariamente con MERGE: agrega los 35 días más
# MAGIC recientes y elimina los registros con más de 35 días de antigüedad.
# MAGIC Esto garantiza que el seed siempre tenga exactamente el historial
# MAGIC necesario para calcular el FWI en el pipeline de forecast.

# COMMAND ----------

import pandas as pd
import os

fecha_corte_seed = (pd.Timestamp.now() - pd.Timedelta(days=35)).strftime("%Y-%m-%d")

# Leer CSV del seed (extracción más reciente)
seed_files = sorted([
    f for f in os.listdir(PATH_METEO_SEED)
    if f.endswith(".csv")
]) if os.path.exists(PATH_METEO_SEED) else []

if seed_files:
    # Tomar el último archivo seed disponible
    seed_path = f"{PATH_METEO_SEED}/{seed_files[-1]}"
    df_seed   = spark.read.option("header", "true").option("inferSchema", "true").csv(seed_path)
    df_seed   = df_seed.withColumnRenamed("date", "date_col") if "date" not in df_seed.columns else df_seed
    df_seed   = df_seed.withColumn("date", F.to_date("date"))

    # MERGE en bronze_openmeteo_seed: actualiza existentes + inserta nuevos
    df_seed.createOrReplaceTempView("v_seed_new")
    spark.sql(f"""
        MERGE INTO {TABLE_METEO_SEED} AS tgt
        USING v_seed_new AS src
            ON tgt.cell_id = src.cell_id AND tgt.date = src.date
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)

    # Eliminar registros más antiguos que 35 días
    spark.sql(f"""
        DELETE FROM {TABLE_METEO_SEED}
        WHERE date < DATE('{fecha_corte_seed}')
    """)

    n_seed = spark.read.table(TABLE_METEO_SEED).count()
    print(f"Seed actualizado: {n_seed:,} registros (ventana 35d desde {fecha_corte_seed})")
else:
    print("No se encontró archivo de seed — correr etl_extract_openmeteo_seed primero.")

# COMMAND ----------

# MAGIC %md ## Open-Meteo Forecast — sobreescritura diaria
# MAGIC
# MAGIC El forecast se sobreescribe completamente cada día con el nuevo pronóstico.
# MAGIC Se toma el archivo más reciente del volumen de forecast.

# COMMAND ----------

fc_files = sorted([
    f for f in os.listdir(PATH_METEO_FORECAST)
    if f.startswith("forecast_") and f.endswith(".csv")
]) if os.path.exists(PATH_METEO_FORECAST) else []

if fc_files:
    fc_latest = f"{PATH_METEO_FORECAST}/{fc_files[-1]}"
    df_fc     = spark.read.option("header", "true").option("inferSchema", "true").csv(fc_latest)
    df_fc     = df_fc.withColumn("date", F.to_date("date"))

    # Sobreescribir completo — el forecast de hoy reemplaza al de ayer
    (
        df_fc.write
        .format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(TABLE_METEO_FC)
    )
    print(f"Forecast actualizado: {df_fc.count():,} filas | archivo: {fc_files[-1]}")
else:
    print("No se encontró archivo de forecast — correr etl_extract_openmeteo_forecast primero.")

