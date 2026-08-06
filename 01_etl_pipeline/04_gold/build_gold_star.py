# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — Esquema Estrella (BI / Dashboard)
# MAGIC
# MAGIC La capa Gold tenía únicamente OBTs orientadas a ML (`training_dataset_v2`,
# MAGIC `forecast_gold_temp`). Este notebook agrega el **modelado dimensional
# MAGIC (Kimball)** que consume el dashboard AI/BI de Databricks:
# MAGIC
# MAGIC | Tabla | Grano | Fuente |
# MAGIC |---|---|---|
# MAGIC | `dim_cell` | 1 fila por celda de grilla | `aux_grid_pampa` + `silver_land_cover` |
# MAGIC | `dim_date` | 1 fila por día calendario | generado (2022 → 2027) |
# MAGIC | `dim_model` | 1 fila por versión de modelo | artefactos de evaluación v4 |
# MAGIC | `fact_fire_detection` | celda × día con focos | `silver_nasa_firms` |
# MAGIC | `fact_weather_daily` | celda × día (obs + forecast) | `silver_openmeteo` |
# MAGIC | `fact_prediction` | celda × fecha forecast × corrida | `cloud_inference_engine` |
# MAGIC | `fact_feature_importance` | feature × modelo | `shap_importance_v4.csv` |
# MAGIC | `fact_baseline_metrics` | baseline × modelo | `baselines_v4.csv` |
# MAGIC
# MAGIC Decisiones de diseño:
# MAGIC - **Clave natural** `cell_id` como PK de `dim_cell` (grilla estable de 2266
# MAGIC   nodos; una surrogate key no aporta nada a este scope y complica los joins).
# MAGIC - `dim_cell` **denormalizada** (subregión y land cover adentro, sin snowflake).
# MAGIC - Las OBTs de ML no se tocan: la estrella es una capa de presentación
# MAGIC   adicional, no un reemplazo (mismo criterio que "no re-entrenar").
# MAGIC - `fact_prediction` NO se escribe acá: la escribe el motor de inferencia en
# MAGIC   el mismo run del job (task anterior). Acá solo se crean las vistas.

# COMMAND ----------

import json
import pandas as pd
from pyspark.sql import functions as F
from pyspark.sql.window import Window
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    force=True
)
logger = logging.getLogger("GOLD_STAR")

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

CATALOG = "fire_risk_project"
GOLD    = f"{CATALOG}.`03_gold`"

TABLE_GRID = f"{CATALOG}.`00_landing`.aux_grid_pampa"
TABLE_LC   = f"{CATALOG}.`02_silver`.silver_land_cover"
TABLE_NASA = f"{CATALOG}.`02_silver`.silver_nasa_firms"
TABLE_OM   = f"{CATALOG}.`02_silver`.silver_openmeteo"

VOL_ARTIFACTS    = f"/Volumes/{CATALOG}/03_gold/training_dataset_v2"
PATH_CALIB       = f"{VOL_ARTIFACTS}/calibrator_v4.json"
PATH_CAL_SUMMARY = f"{VOL_ARTIFACTS}/calibration_summary_v4.json"
PATH_SHAP        = f"{VOL_ARTIFACTS}/shap_importance_v4.csv"
PATH_BASELINES   = f"{VOL_ARTIFACTS}/baselines_v4.csv"

MODEL_VERSION = "v4"

LAND_COVER_DESC = {0: "Otro/Urbano", 1: "Cultivo", 2: "Vegetación Natural"}

# COMMAND ----------

# MAGIC %md ## 1 · dim_cell — dimensión de celdas de grilla

# COMMAND ----------

df_grid = (
    spark.read.table(TABLE_GRID)
    .filter("is_valid = true")
    .select(
        "cell_id", "latitude", "longitude", "grid_row", "grid_col",
        "subregion_id", "subregion_name",
        "elevation", "slope", "aspect",
        "dist_road_km", "pop_density_km2",
    )
)

df_lc_last = (
    spark.read.table(TABLE_LC)
    .withColumn("_rn", F.row_number().over(
        Window.partitionBy("cell_id").orderBy(F.col("year").desc())
    ))
    .filter("_rn = 1")
    .select("cell_id", "land_cover_cat")
)

lc_desc_expr = F.create_map(
    *[x for k, v in LAND_COVER_DESC.items() for x in (F.lit(k), F.lit(v))]
)

dim_cell = (
    df_grid
    .join(df_lc_last, on="cell_id", how="left")
    .fillna({"land_cover_cat": 0})
    .withColumn("land_cover_desc", lc_desc_expr[F.col("land_cover_cat")])
)

dim_cell.write.format("delta").mode("overwrite") \
    .option("overwriteSchema", "true").saveAsTable(f"{GOLD}.dim_cell")

n_cells = dim_cell.count()
logger.info(f"dim_cell: {n_cells:,} celdas")

# COMMAND ----------

# MAGIC %md ## 2 · dim_date — dimensión calendario (estación austral)

# COMMAND ----------

dim_date = spark.sql("""
    SELECT
        d                                        AS date,
        year(d)                                  AS year,
        month(d)                                 AS month,
        CASE month(d)
            WHEN 1 THEN 'Enero'      WHEN 2  THEN 'Febrero' WHEN 3  THEN 'Marzo'
            WHEN 4 THEN 'Abril'      WHEN 5  THEN 'Mayo'    WHEN 6  THEN 'Junio'
            WHEN 7 THEN 'Julio'      WHEN 8  THEN 'Agosto'  WHEN 9  THEN 'Septiembre'
            WHEN 10 THEN 'Octubre'   WHEN 11 THEN 'Noviembre' ELSE 'Diciembre'
        END                                      AS month_name,
        day(d)                                   AS day,
        dayofyear(d)                             AS day_of_year,
        weekofyear(d)                            AS week_of_year,
        CASE
            WHEN month(d) IN (12, 1, 2) THEN 'Verano'
            WHEN month(d) IN (3, 4, 5)  THEN 'Otoño'
            WHEN month(d) IN (6, 7, 8)  THEN 'Invierno'
            ELSE 'Primavera'
        END                                      AS season_austral,
        -- Temporada de fuego pampeana (jul-dic): mismo período sobre el que
        -- se evaluó el modelo y se calibró el threshold operativo F2.
        month(d) BETWEEN 7 AND 12                AS is_fire_season
    FROM (
        SELECT explode(sequence(DATE'2022-01-01', DATE'2027-12-31', INTERVAL 1 DAY)) AS d
    )
""")

dim_date.write.format("delta").mode("overwrite") \
    .option("overwriteSchema", "true").saveAsTable(f"{GOLD}.dim_date")

logger.info(f"dim_date: {dim_date.count():,} días (2022 → 2027)")

# COMMAND ----------

# MAGIC %md ## 3 · dim_model — registro del modelo (métricas reales de evaluación)

# COMMAND ----------

with open(PATH_CAL_SUMMARY) as f:
    cal = json.load(f)

df_base = pd.read_csv(PATH_BASELINES)
full_row = df_base[df_base["baseline"] == "FULL_MODEL_v4"].iloc[0]

dim_model_pd = pd.DataFrame([{
    "model_version":      MODEL_VERSION,
    "algorithm":          "XGBoost (hist) + SSA hyperparam search",
    "calibration_method": cal["selected"],
    "auc_test":           float(full_row["auc"]),
    "ap_test":            float(full_row["ap"]),
    "ece_raw":            float(cal["ece_test_raw"]),
    "ece_calibrated":     float(cal["ece_test_calibrated"]),
    "threshold_f2":       float(cal["f2_threshold_calibrated"]),
    "threshold_f1":       float(cal["f1_threshold_calibrated"]),
    "precision_f2":       float(cal["metrics_f2"]["precision"]),
    "recall_f2":          float(cal["metrics_f2"]["recall"]),
    "train_period":       "2022-01 → 2024-06 (split temporal)",
    "test_period":        "2024-07 → 2024-12 (temporada de fuego)",
    "notes": (
        "Probabilidades calibradas (Platt). El modelo es principalmente un "
        "prior geográfico-estacional: las features estáticas dominan (SHAP); "
        "el FWI aporta valor interpretativo pero es redundante con el clima crudo."
    ),
}])

spark.createDataFrame(dim_model_pd).write.format("delta").mode("overwrite") \
    .option("overwriteSchema", "true").saveAsTable(f"{GOLD}.dim_model")

logger.info(f"dim_model: {MODEL_VERSION} (AUC {full_row['auc']:.4f}, AP {full_row['ap']:.4f})")

# COMMAND ----------

# MAGIC %md ## 4 · fact_fire_detection — focos históricos VIIRS por celda/día

# COMMAND ----------

fact_fire = (
    spark.read.table(TABLE_NASA)
    .filter(F.col("type") == 0)
    .groupBy(
        F.col("cell_id"),
        F.col("acq_date").alias("date"),
    )
    .agg(
        F.count("*").alias("n_detections"),
        F.round(F.avg("frp"), 2).alias("frp_mean"),
        F.round(F.max("frp"), 2).alias("frp_max"),
        F.max(F.col("daynight") == "D").alias("any_daytime"),
    )
)

fact_fire.write.format("delta").mode("overwrite") \
    .option("overwriteSchema", "true").saveAsTable(f"{GOLD}.fact_fire_detection")

logger.info(f"fact_fire_detection: {fact_fire.count():,} celda-días con fuego")

# COMMAND ----------

# MAGIC %md ## 5 · fact_weather_daily — clima + FWI (ventana 35d obs + 4d forecast)

# COMMAND ----------

fact_weather = (
    spark.read.table(TABLE_OM)
    .select(
        "cell_id", "date", "is_forecast",
        "temperature_2m", "relative_humidity", "wind_speed_10m",
        "precipitation", "vpd_kpa", "solar_radiation",
        "ffmc", "dmc", "isi", "bui", "fwi",
        "dias_secos",
    )
)

fact_weather.write.format("delta").mode("overwrite") \
    .option("overwriteSchema", "true").saveAsTable(f"{GOLD}.fact_weather_daily")

logger.info(f"fact_weather_daily: {fact_weather.count():,} filas")

# COMMAND ----------

# MAGIC %md ## 6 · fact_feature_importance + fact_baseline_metrics

# COMMAND ----------

df_shap = pd.read_csv(PATH_SHAP)
df_shap["model_version"] = MODEL_VERSION
df_shap["shap_rank"] = df_shap["shap_mean_abs"].rank(ascending=False).astype(int)

spark.createDataFrame(df_shap).write.format("delta").mode("overwrite") \
    .option("overwriteSchema", "true").saveAsTable(f"{GOLD}.fact_feature_importance")

df_base["model_version"] = MODEL_VERSION
spark.createDataFrame(df_base).write.format("delta").mode("overwrite") \
    .option("overwriteSchema", "true").saveAsTable(f"{GOLD}.fact_baseline_metrics")

logger.info(f"fact_feature_importance: {len(df_shap)} features | "
            f"fact_baseline_metrics: {len(df_base)} baselines")

# COMMAND ----------

# MAGIC %md ## 7 · Vistas de consumo para el dashboard

# COMMAND ----------

spark.sql(f"""
    CREATE OR REPLACE VIEW {GOLD}.vw_predictions_latest AS
    SELECT
        p.*,
        c.latitude, c.longitude,
        c.subregion_id, c.subregion_name,
        c.land_cover_desc, c.elevation,
        d.season_austral, d.is_fire_season, d.month_name
    FROM {GOLD}.fact_prediction p
    JOIN {GOLD}.dim_cell c USING (cell_id)
    JOIN {GOLD}.dim_date d ON d.date = p.forecast_date
    WHERE p.run_date = (SELECT MAX(run_date) FROM {GOLD}.fact_prediction)
""")

spark.sql(f"""
    CREATE OR REPLACE VIEW {GOLD}.vw_fire_history AS
    SELECT
        f.*,
        c.latitude, c.longitude, c.subregion_name, c.land_cover_desc,
        d.year, d.month, d.month_name, d.season_austral, d.is_fire_season
    FROM {GOLD}.fact_fire_detection f
    JOIN {GOLD}.dim_cell c USING (cell_id)
    JOIN {GOLD}.dim_date d USING (date)
""")

logger.info("Vistas creadas: vw_predictions_latest, vw_fire_history")

# COMMAND ----------

# MAGIC %md ## 8 · Comentarios de catálogo + validación de integridad referencial

# COMMAND ----------

COMMENTS = {
    "dim_cell":                "Dimensión de celdas de la grilla pampeana 0.25° (PK natural cell_id). Denormalizada: subregión, topografía y land cover.",
    "dim_date":                "Dimensión calendario 2022-2027 con estación austral y temporada de fuego (jul-dic).",
    "dim_model":               "Registro de versiones del modelo con métricas de test reales (AUC/AP/ECE/thresholds).",
    "fact_fire_detection":     "Hecho: focos VIIRS de vegetación agregados por celda × día (2022-2024). Fuente: silver_nasa_firms.",
    "fact_weather_daily":      "Hecho: clima + FWI por celda × día, ventana 35d observada + 4d forecast. Fuente: silver_openmeteo.",
    "fact_feature_importance": "Hecho: importancia de features del modelo (SHAP mean |value| y gain) por versión.",
    "fact_baseline_metrics":   "Hecho: AUC/AP de baselines (prevalencia, persistencia, FWI-solo) vs modelo completo.",
}
for t, comment in COMMENTS.items():
    spark.sql(f"COMMENT ON TABLE {GOLD}.{t} IS '{comment}'")

orphans_fire = (
    spark.table(f"{GOLD}.fact_fire_detection")
    .join(spark.table(f"{GOLD}.dim_cell"), on="cell_id", how="left_anti").count()
)
orphans_weather = (
    spark.table(f"{GOLD}.fact_weather_daily")
    .join(spark.table(f"{GOLD}.dim_cell"), on="cell_id", how="left_anti").count()
)
assert orphans_fire == 0, f"fact_fire_detection: {orphans_fire} celdas fuera de dim_cell"
assert orphans_weather == 0, f"fact_weather_daily: {orphans_weather} celdas fuera de dim_cell"

n_weather_dates_off = (
    spark.table(f"{GOLD}.fact_weather_daily")
    .join(spark.table(f"{GOLD}.dim_date"), on="date", how="left_anti").count()
)
assert n_weather_dates_off == 0, "fact_weather_daily con fechas fuera de dim_date"

logger.info("Integridad referencial OK (0 huérfanos dims ↔ facts).")

dbutils.notebook.exit(
    f"OK gold_star: dim_cell={n_cells} | fire={fact_fire.count():,} | "
    f"weather={fact_weather.count():,} filas"
)
