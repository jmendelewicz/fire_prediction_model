# Databricks notebook source
# MAGIC %md
# MAGIC # Motor de Inferencia en la Nube — Modelo V4
# MAGIC
# MAGIC Genera predicciones de riesgo de incendio para los próximos 4 días
# MAGIC usando el modelo XGBoost v4 (canónico, sin leakage, con features
# MAGIC espaciales).
# MAGIC
# MAGIC **Inputs:**
# MAGIC - `03_gold.forecast_gold_temp` — 4 días × 2266 nodos × 42 features
# MAGIC   (alineado al training por `build_gold_forecast.py`)
# MAGIC - `xgboost_v4.json` — modelo entrenado (subido al volumen)
# MAGIC - `feature_cols_v4.pkl` — orden exacto de las features (XGBoost es
# MAGIC   column-position-sensitive)
# MAGIC
# MAGIC **Output:**
# MAGIC - `/Volumes/fire_risk_project/03_gold/outputs/predictions_ui.json`
# MAGIC   (consumido por el frontend)
# MAGIC
# MAGIC **Fix C-3/C-6 (2026-05-16):**
# MAGIC La versión legacy en `02_ml_model/legacy/model_v2/cloud_inference_engine.py`
# MAGIC consumía CSVs crudos de landing y hardcodeaba mocks para features
# MAGIC faltantes (`solar_radiation=200`, etc.). Esta versión consume la
# MAGIC tabla `forecast_gold_temp` que ya tiene TODAS las features alineadas
# MAGIC al training.

# COMMAND ----------

import pandas as pd
import numpy as np
import xgboost as xgb
import json
import pickle
from datetime import datetime

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

CATALOG = "fire_risk_project"

TABLE_FORECAST  = f"{CATALOG}.03_gold.forecast_gold_temp"
PATH_MODEL      = f"/Volumes/{CATALOG}/03_gold/training_dataset_v2/xgboost_v4.json"
PATH_FEATS      = f"/Volumes/{CATALOG}/03_gold/training_dataset_v2/feature_cols_v4.pkl"
PATH_METRICS    = f"/Volumes/{CATALOG}/03_gold/training_dataset_v2/metricas_v4.csv"
PATH_CALIBRATOR = f"/Volumes/{CATALOG}/03_gold/training_dataset_v2/calibrator_v4.json"
PATH_OUTPUT     = f"/Volumes/{CATALOG}/03_gold/outputs/predictions_ui.json"

# COMMAND ----------

# MAGIC %md ## 1 · Cargar modelo y feature schema

# COMMAND ----------

print(f"Cargando modelo: {PATH_MODEL}")
clf = xgb.XGBClassifier()
clf.load_model(PATH_MODEL)

print(f"Cargando feature schema: {PATH_FEATS}")
with open(PATH_FEATS, "rb") as f:
    feature_cols = pickle.load(f)
print(f"Modelo espera {len(feature_cols)} features.")

print(f"Cargando calibrador: {PATH_CALIBRATOR}")
try:
    with open(PATH_CALIBRATOR, "r") as f:
        calibrator = json.load(f)
    cal_method = calibrator["method"]
    threshold = float(calibrator["f2_threshold_calibrated"])
    print(f"Calibrador: {cal_method} | threshold F2 calibrado: {threshold:.4f} | "
          f"ECE {calibrator['ece_test_raw']:.3f} → {calibrator['ece_test_calibrated']:.3f}")
except Exception as e:
    calibrator = None
    try:
        df_metrics = pd.read_csv(PATH_METRICS, index_col=0)
        threshold  = float(df_metrics.loc["best_f2_threshold", "valor"])
    except Exception:
        threshold = 0.5
    print(f"WARNING: sin calibrador ({e}). Usando prob cruda, threshold {threshold:.3f}.")

def apply_calibration(raw_prob, calibrator):
    if calibrator is None or calibrator.get("method") != "platt":
        return raw_prob
    eps = 1e-6
    p = np.clip(raw_prob, eps, 1 - eps)
    logit = np.log(p / (1 - p))
    a = calibrator["platt"]["a"]
    b = calibrator["platt"]["b"]
    return 1.0 / (1.0 + np.exp(-(a * logit + b)))

# COMMAND ----------

# MAGIC %md ## 2 · Leer forecast_gold_temp

# COMMAND ----------

df_gold = spark.read.table(TABLE_FORECAST).toPandas()
print(f"Forecast loaded: {len(df_gold):,} filas × {df_gold.shape[1]} columnas")
print(f"Fechas: {df_gold['date'].min()} → {df_gold['date'].max()}")
print(f"Nodos:  {df_gold['cell_id'].nunique():,}")

missing = [c for c in feature_cols if c not in df_gold.columns]
extra   = [c for c in df_gold.columns if c not in feature_cols + ["cell_id", "date"]]
if missing:
    raise RuntimeError(
        f"Faltan features en forecast_gold_temp que el modelo necesita: {missing}. "
        "Verificar que build_gold_forecast.py corrió completo."
    )
if extra:
    print(f"Features extra en forecast_gold_temp (ignoradas): {extra}")

# COMMAND ----------

# MAGIC %md ## 3 · Inferencia

# COMMAND ----------

X = df_gold[feature_cols].copy()

for c in ("subregion_id", "land_cover_cat"):
    if c in X.columns:
        X[c] = X[c].astype("int64").astype("category")

raw_prob               = clf.predict_proba(X)[:, 1]
df_gold["risk_prob_raw"] = raw_prob
df_gold["risk_prob"]   = apply_calibration(raw_prob, calibrator)
df_gold["risk_alert"]  = (df_gold["risk_prob"] >= threshold).astype(int)
df_gold["risk_level"]  = (df_gold["risk_prob"] * 100).round(1)

df_gold["risk_percentile"] = (
    df_gold.groupby("date")["risk_prob"].rank(pct=True) * 100
).round(1)

PCT_BINS   = [0, 50, 80, 95, 100]
PCT_LABELS = ["Bajo", "Moderado", "Alto (relativo)", "Muy alto (relativo)"]
df_gold["risk_category"] = pd.cut(
    df_gold["risk_percentile"], bins=PCT_BINS,
    labels=PCT_LABELS, include_lowest=True
).astype(str)

print(f"\nDistribución de risk_prob (calibrada):")
print(df_gold["risk_prob"].describe())
print(f"   (cruda media {raw_prob.mean():.3f} → calibrada media {df_gold['risk_prob'].mean():.3f})")
print(f"\nAlertas (prob calibrada ≥ {threshold:.4f}): {df_gold['risk_alert'].sum():,} de {len(df_gold):,}")

# COMMAND ----------

# MAGIC %md ## 4 · fact_prediction — hecho del esquema estrella (BI)
# MAGIC
# MAGIC Grano: celda × fecha de forecast × corrida (`run_date`). Idempotente:
# MAGIC re-correr el mismo día reemplaza la corrida del día, no duplica.
# MAGIC El dashboard consume `vw_predictions_latest` (creada por `build_gold_star`).

# COMMAND ----------

TABLE_FACT_PRED = f"{CATALOG}.`03_gold`.fact_prediction"

run_date     = datetime.utcnow().date()
generated_at = datetime.utcnow()

fact_pred = df_gold[[
    "cell_id", "date", "risk_prob_raw", "risk_prob", "risk_level",
    "risk_percentile", "risk_category", "risk_alert",
]].copy()
fact_pred = fact_pred.rename(columns={"date": "forecast_date", "risk_alert": "alert_flag"})
fact_pred["forecast_date"] = pd.to_datetime(fact_pred["forecast_date"]).dt.date
fact_pred["run_date"]      = run_date
fact_pred["horizon_days"]  = fact_pred["forecast_date"].map(
    lambda d: (d - run_date).days + 1
)
fact_pred["model_version"] = "v4"
fact_pred["threshold"]     = float(threshold)
fact_pred["generated_at"]  = generated_at

for c, nd in [("risk_prob_raw", 6), ("risk_prob", 6),
              ("risk_level", 1), ("risk_percentile", 1)]:
    fact_pred[c] = fact_pred[c].astype("float64").round(nd)

sdf_pred = spark.createDataFrame(fact_pred)

if spark.catalog.tableExists(TABLE_FACT_PRED):
    spark.sql(f"DELETE FROM {TABLE_FACT_PRED} WHERE run_date = DATE('{run_date}')")
    sdf_pred.write.format("delta").mode("append").saveAsTable(TABLE_FACT_PRED)
else:
    sdf_pred.write.format("delta").mode("overwrite").saveAsTable(TABLE_FACT_PRED)
    spark.sql(
        f"COMMENT ON TABLE {TABLE_FACT_PRED} IS "
        "'Hecho: predicciones de riesgo por celda × fecha forecast × corrida. "
        "risk_prob calibrada (Platt); risk_percentile = ranking relativo del día. "
        "Escrita por cloud_inference_engine.'"
    )

print(f"fact_prediction: {len(fact_pred):,} filas (run_date={run_date})")

# COMMAND ----------

# MAGIC %md ## 5 · Exportar JSON para el frontend
# MAGIC
# MAGIC Schema:
# MAGIC ```json
# MAGIC {
# MAGIC   "generated_at": "2026-05-16T12:34:56Z",
# MAGIC   "model_version": "v4",
# MAGIC   "threshold": 0.42,
# MAGIC   "nodes": [
# MAGIC     {
# MAGIC       "cell_id": "-34.2500_-63.0000",
# MAGIC       "predictions": {
# MAGIC         "2026-05-17": {"risk_level": 78.3, "alert": 1},
# MAGIC         "2026-05-18": {"risk_level": 65.1, "alert": 1},
# MAGIC         ...
# MAGIC       }
# MAGIC     },
# MAGIC     ...
# MAGIC   ]
# MAGIC }
# MAGIC ```

# COMMAND ----------

DETAIL = {
    "temp":   "temperature_2m",
    "hum":    "relative_humidity",
    "wind":   "wind_speed_10m",
    "precip": "precipitation",
    "fwi":    "fwi",
    "ffmc":   "ffmc",
    "dmc":    "dmc",
    "isi":    "isi",
    "bui":    "bui",
}
DETAIL = {k: v for k, v in DETAIL.items() if v in df_gold.columns}

nodes = []
for cell_id, group in df_gold.groupby("cell_id"):
    preds = {}
    for _, row in group.iterrows():
        rec = {
            "risk_level": round(float(row["risk_level"]), 1),
            "risk_pct":   round(float(row["risk_percentile"]), 1),
            "risk_cat":   str(row["risk_category"]),
            "alert":      int(row["risk_alert"]),
        }
        for short, col in DETAIL.items():
            v = row[col]
            rec[short] = round(float(v), 2) if pd.notnull(v) else None
        preds[str(row["date"])] = rec
    nodes.append({"cell_id": str(cell_id), "predictions": preds})

output = {
    "generated_at":  datetime.utcnow().isoformat() + "Z",
    "model_version": "v4",
    "calibration":   (calibrator["method"] if calibrator else "none"),
    "threshold":     threshold,
    "n_nodes":       len(nodes),
    "fechas":        sorted(df_gold["date"].astype(str).unique().tolist()),
    "nodes":         nodes,
}

import os
os.makedirs(os.path.dirname(PATH_OUTPUT), exist_ok=True)

with open(PATH_OUTPUT, "w") as f:
    json.dump(output, f)

print(f"Predicciones exportadas a: {PATH_OUTPUT}")
print(f"  Tamaño: {os.path.getsize(PATH_OUTPUT)/1024:.1f} KB")

dbutils.notebook.exit(
    f"OK: predictions_ui.json | {len(nodes):,} nodos | "
    f"{len(output['fechas'])} días | {df_gold['risk_alert'].sum():,} alertas"
)
