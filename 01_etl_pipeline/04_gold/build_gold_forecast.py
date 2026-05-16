# Databricks notebook source
# MAGIC %md
# MAGIC # Gold Forecast — Pipeline Diario (Job2)
# MAGIC
# MAGIC Toma `silver_openmeteo` (39 días por nodo: 35 histórico + 4 forecast)
# MAGIC y produce `forecast_gold_temp` con **el mismo feature set que ve el
# MAGIC modelo v4 entrenado** — alineación train ↔ serve estricta.
# MAGIC
# MAGIC **Input:**  `02_silver.silver_openmeteo`  (ya tiene FWI calculado)
# MAGIC **Input:**  `02_silver.silver_nasa_firms` (para `fire_vecinos_3d`)
# MAGIC **Input:**  `ndvi_means_per_cell_v4.csv`  (medias por celda fitted on train — para `ndvi_anomaly`)
# MAGIC **Output:** `03_gold.forecast_gold_temp`   (solo días de forecast, is_forecast=True)
# MAGIC
# MAGIC **AUDIT fix C-3/C-4/C-5 (2026-05-16):** la versión anterior producía
# MAGIC 34 features. El modelo v4 entrenado consume 42 features (35 base + 4
# MAGIC interacciones + 3 espaciales). Esta versión computa las 8 features
# MAGIC faltantes con la misma lógica que `02_ml_model/model_v4/train_model_v4.py`:
# MAGIC
# MAGIC - **Espaciales (queen contiguity, ±0.25°)**:
# MAGIC   * `fwi_vecinos_mean` — media del FWI de los vecinos en el mismo día
# MAGIC   * `fwi_vecinos_max`  — máximo del FWI de los vecinos
# MAGIC   * `fire_vecinos_3d`  — 1 si algún vecino tuvo fuego en los últimos 3 días
# MAGIC      (consulta `silver_nasa_firms`; siempre 0 para días estrictamente futuros)
# MAGIC - **Interacciones** (mismo cálculo que `add_features` del training):
# MAGIC   * `fwi_x_vpd`, `temp_x_dry`, `wind_x_fwi`
# MAGIC - **Anomalía NDVI**:
# MAGIC   * `ndvi_anomaly = ndvi - ndvi_mean_per_cell` (mean leída del CSV
# MAGIC      persistido por el training — train↔serve consistency).

# COMMAND ----------

import logging
import os
import pandas as pd
import numpy as np
from pyspark.sql import functions as F
from pyspark.sql import Window
from pyspark.sql.types import IntegerType, DoubleType, StringType, StructType, StructField

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
TABLE_NASA   = f"{CATALOG}.02_silver.silver_nasa_firms"
TABLE_OUTPUT = f"{CATALOG}.03_gold.forecast_gold_temp"

# Path al CSV de medias NDVI por celda que persiste el training (v4).
# Si no existe, el script igualmente corre pero ndvi_anomaly = 0 (warning).
PATH_NDVI_MEANS = "/Volumes/fire_risk_project/03_gold/training_dataset_v2/ndvi_means_per_cell_v4.csv"

# Resolución de grilla para vecinos (queen contiguity ±0.25°)
GRID_RES = 0.25

# Las 42 features que ve el modelo v4 entrenado.
# Orden DEBE coincidir con feature_cols_v4.pkl (XGBoost es position-sensitive).
FINAL_COLS = [
    "cell_id", "date",
    # Estáticas
    "subregion_id", "elevation", "slope", "aspect",
    "dist_road_km", "land_cover_cat", "pop_density_km2",
    # Estacionalidad
    "mes_sin", "mes_cos", "dia_sin", "dia_cos", "calendario_agricola",
    # Climáticas
    "temperature_2m", "relative_humidity", "wind_speed_10m",
    "precipitation", "solar_radiation",
    "soil_moisture_0_7cm", "soil_moisture_28_100cm",
    "ndvi", "vpd_kpa",
    # FWI
    "ffmc", "dmc", "bui", "isi", "fwi",
    # Ventanas temporales
    "dias_secos", "spi_90d",
    "fwi_roll14", "fwi_roll30",
    "temperature_2m_roll30", "wind_speed_10m_roll30",
    # Espaciales (AUDIT C-5 fix)
    "fwi_vecinos_mean", "fwi_vecinos_max", "fire_vecinos_3d",
    # Interacciones (AUDIT C-3 fix — same as add_features in train_model_v4.py)
    "fwi_x_vpd", "temp_x_dry", "wind_x_fwi",
    # NDVI anomaly (AUDIT C-1 fix con persistencia de medias del training)
    "ndvi_anomaly",
]

# COMMAND ----------

# MAGIC %md ## 1 · Leer silver_openmeteo (seed 35d + forecast 4d)

# COMMAND ----------

df = spark.read.table(TABLE_INPUT)

logger.info(f"Silver Openmeteo: {df.count():,} filas")
logger.info(f"  Histórico: {df.filter(~F.col('is_forecast')).count():,}")
logger.info(f"  Forecast:  {df.filter( F.col('is_forecast')).count():,}")

# COMMAND ----------

# MAGIC %md ## 2 · Features temporales (estacionalidad + rolling sobre la ventana 39d)
# MAGIC
# MAGIC Calcular sobre la ventana completa para que los rolling tengan historia
# MAGIC suficiente, después filtrar solo los 4 días de forecast para guardar.

# COMMAND ----------

df = df.withColumn("date_col", F.to_date("date"))

w = (
    Window.partitionBy("cell_id")
    .orderBy(F.unix_date(F.col("date_col")))
)

df = (
    df
    .withColumn("mes_sin", F.sin(2 * np.pi * F.month("date_col") / 12))
    .withColumn("mes_cos", F.cos(2 * np.pi * F.month("date_col") / 12))
    .withColumn("dia_sin", F.sin(2 * np.pi * F.dayofyear("date_col") / 365))
    .withColumn("dia_cos", F.cos(2 * np.pi * F.dayofyear("date_col") / 365))
)

df = df.withColumn(
    "calendario_agricola",
    F.when(
        (F.col("land_cover_cat") == 1) &
        F.month("date_col").isin([2, 3, 4, 11, 12]),
        F.lit(1)
    ).otherwise(F.lit(0)).cast(IntegerType())
)

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

# MAGIC %md ## 3 · Features espaciales (vecinos queen contiguity)
# MAGIC
# MAGIC Self-join sobre `silver_openmeteo` con offset ±0.25° en lat/lon.
# MAGIC Idéntico al cálculo de `save_gold.py` para `training_dataset_v2`.

# COMMAND ----------

df = (
    df
    .withColumn("_lat", F.split("cell_id", "_").getItem(0).cast("double"))
    .withColumn("_lon", F.split("cell_id", "_").getItem(1).cast("double"))
)

from pyspark.sql.functions import abs as spark_abs

df_neighbors = (
    df.alias("a")
    .join(
        df.select(
            F.col("cell_id").alias("_n_cell"),
            F.col("date_col").alias("_n_date"),
            F.col("fwi").alias("_n_fwi"),
            F.col("_lat").alias("_n_lat"),
            F.col("_lon").alias("_n_lon"),
        ).alias("b"),
        on=[
            F.col("a.date_col") == F.col("b._n_date"),
            F.col("a.cell_id") != F.col("b._n_cell"),
            spark_abs(F.col("a._lat") - F.col("b._n_lat")) <= GRID_RES + 0.001,
            spark_abs(F.col("a._lon") - F.col("b._n_lon")) <= GRID_RES + 0.001,
        ],
        how="left"
    )
    .groupBy("a.cell_id", "a.date_col")
    .agg(
        F.mean("_n_fwi").alias("fwi_vecinos_mean"),
        F.max("_n_fwi").alias("fwi_vecinos_max"),
    )
)

df = df.join(df_neighbors, on=["cell_id", "date_col"], how="left")

# Para celdas de borde sin vecinos válidos, usar el FWI propio (consistente
# con train_model_v4.add_spatial_features fillna)
df = (
    df
    .withColumn("fwi_vecinos_mean", F.coalesce(F.col("fwi_vecinos_mean"), F.col("fwi")))
    .withColumn("fwi_vecinos_max",  F.coalesce(F.col("fwi_vecinos_max"),  F.col("fwi")))
)

logger.info("Features espaciales FWI calculadas.")

# COMMAND ----------

# MAGIC %md ## 4 · `fire_vecinos_3d` — query a silver_nasa_firms
# MAGIC
# MAGIC Para cada (cell_id, date_col) marca 1 si **algún vecino** tuvo fuego en
# MAGIC los 3 días previos (estricto pasado). En días estrictamente futuros
# MAGIC (forecast >= hoy) la NASA aún no observó fuegos, así que el valor es 0
# MAGIC para esos días — limitación inherente al modelo, equivalente a tratar
# MAGIC el feature como missing (XGBoost lo mapea a su rama "missing").

# COMMAND ----------

# Universo de fuegos relevantes: últimos 38 días (35 seed + 3 lookback)
# Suficiente para cubrir los lookbacks de cada fila de silver_openmeteo
fecha_lookback = F.date_sub(F.current_date(), 40)

df_fires = (
    spark.read.table(TABLE_NASA)
    .filter(F.col("fecha_join") >= fecha_lookback)
    .filter(F.col("type") == 0)   # solo vegetación, igual que en build_gold
    .select(
        F.col("cell_id").alias("_fire_cell"),
        F.col("fecha_join").alias("_fire_date"),
    )
    .distinct()
)

# Cross-join: para cada fila (a) buscar si ALGÚN vecino tuvo fuego
# en los 3 días previos a a.date_col. Misma lógica que el training:
# fire_vecinos_3d = 1 si existe (n, F) con n ∈ neighbors(a.cell_id)
# y F ∈ {a.date_col - 1, a.date_col - 2, a.date_col - 3}.
df_with_fire = (
    df.alias("a")
    .join(
        df_fires.alias("f"),
        on=[
            F.col("a._lat").isNotNull(),   # filas con coords parseadas
            spark_abs(F.col("a._lat") - F.split(F.col("f._fire_cell"), "_").getItem(0).cast("double")) <= GRID_RES + 0.001,
            spark_abs(F.col("a._lon") - F.split(F.col("f._fire_cell"), "_").getItem(1).cast("double")) <= GRID_RES + 0.001,
            F.col("a.cell_id") != F.col("f._fire_cell"),   # estrictamente vecino, no la celda misma
            F.col("f._fire_date") >= F.date_sub(F.col("a.date_col"), 3),
            F.col("f._fire_date") <  F.col("a.date_col"),    # estricto pasado
        ],
        how="left"
    )
    .groupBy("a.cell_id", "a.date_col")
    .agg(F.max(F.when(F.col("f._fire_cell").isNotNull(), 1).otherwise(0)).alias("fire_vecinos_3d"))
)

df = df.join(df_with_fire, on=["cell_id", "date_col"], how="left")
df = df.withColumn("fire_vecinos_3d",
                    F.coalesce(F.col("fire_vecinos_3d"), F.lit(0)).cast(IntegerType()))
df = df.drop("_lat", "_lon")

logger.info("Feature fire_vecinos_3d calculada (consultando silver_nasa_firms).")

# COMMAND ----------

# MAGIC %md ## 5 · `ndvi_anomaly` — usar medias persistidas por el training
# MAGIC
# MAGIC `ndvi_means_per_cell_v4.csv` lo genera `train_model_v4.py` y se sube
# MAGIC al volumen `/Volumes/.../03_gold/training_dataset_v2/` después de cada
# MAGIC re-entrenamiento. Garantiza que serving usa LA MISMA media por celda
# MAGIC que el modelo vio en training — cierra el train↔serve skew de C-1.

# COMMAND ----------

if os.path.exists(PATH_NDVI_MEANS):
    logger.info(f"Cargando ndvi means desde: {PATH_NDVI_MEANS}")
    df_ndvi_means = (
        spark.read
        .option("header", "true")
        .option("inferSchema", "true")
        .csv(PATH_NDVI_MEANS)
        .withColumn("cell_id", F.col("cell_id").cast(StringType()))
        .withColumn("ndvi_mean", F.col("ndvi_mean").cast(DoubleType()))
    )
    df = df.join(df_ndvi_means, on="cell_id", how="left")

    # Fallback para celdas no presentes en el CSV (improbable, defensivo)
    df = df.withColumn(
        "ndvi_anomaly",
        F.col("ndvi") - F.coalesce(F.col("ndvi_mean"), F.lit(0.0))
    ).drop("ndvi_mean")
else:
    logger.warning(
        f"NO se encontró {PATH_NDVI_MEANS}. "
        "Se computa ndvi_anomaly=0 — el modelo va a perder señal en esta feature. "
        "Subir el CSV generado por train_model_v4.py al volumen para resolver."
    )
    df = df.withColumn("ndvi_anomaly", F.lit(0.0).cast(DoubleType()))

# COMMAND ----------

# MAGIC %md ## 6 · Interacciones (mismo cálculo que train_model_v4.add_features)

# COMMAND ----------

df = (
    df
    .withColumn("fwi_x_vpd",  F.col("fwi") * F.col("vpd_kpa"))
    .withColumn("temp_x_dry", F.col("temperature_2m") * F.col("dias_secos"))
    .withColumn("wind_x_fwi", F.col("wind_speed_10m") * F.col("fwi"))
)

logger.info("Interacciones calculadas.")

# COMMAND ----------

# MAGIC %md ## 7 · Filtrar a forecast (4 días) y validar

# COMMAND ----------

df_forecast = df.filter(F.col("is_forecast") == True)
logger.info(f"Días de forecast (salida): {df_forecast.count():,} filas")

# Renombrar date_col → date para coincidir con el schema esperado
df_forecast = df_forecast.withColumn("date", F.col("date_col").cast(StringType()))

missing = [c for c in FINAL_COLS if c not in df_forecast.columns]
if missing:
    raise ValueError(f"Columnas faltantes en forecast_gold_temp: {missing}")

df_final = df_forecast.select(FINAL_COLS).orderBy("cell_id", "date")

# COMMAND ----------

# MAGIC %md ## 8 · Guardar

# COMMAND ----------

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
    F.round(F.mean("fwi_vecinos_mean"), 2).alias("fwi_vec_medio"),
    F.sum("fire_vecinos_3d").alias("celdas_con_fuego_vecino"),
    F.count("*").alias("nodos")
).orderBy("date").show(10)

print("\nDistribución de fire_vecinos_3d:")
df_check.groupBy("fire_vecinos_3d").count().orderBy("fire_vecinos_3d").show()

dbutils.notebook.exit(f"OK: forecast_gold_temp — {total:,} filas | {fechas['desde']} → {fechas['hasta']}")
