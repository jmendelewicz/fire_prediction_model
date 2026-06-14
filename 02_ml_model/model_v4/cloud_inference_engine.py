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

# Calibrador (fix R1) — el modelo se entrena con datos balanceados 1:1 mientras
# la realidad es ~3% de fuego, así que sus probabilidades crudas están infladas
# (ECE test ~0.28). El calibrador (Platt) las mapea a probabilidades reales
# (ECE ~0.006) sin alterar el ranking (AUC/AP intactos). Imprescindible para que
# `risk_level` signifique una probabilidad de verdad en el dashboard/API.
print(f"Cargando calibrador: {PATH_CALIBRATOR}")
try:
    with open(PATH_CALIBRATOR, "r") as f:
        calibrator = json.load(f)
    cal_method = calibrator["method"]
    # El threshold operativo se recalcula sobre las probabilidades CALIBRADAS.
    threshold = float(calibrator["f2_threshold_calibrated"])
    print(f"Calibrador: {cal_method} | threshold F2 calibrado: {threshold:.4f} | "
          f"ECE {calibrator['ece_test_raw']:.3f} → {calibrator['ece_test_calibrated']:.3f}")
except Exception as e:
    calibrator = None
    # Fallback: threshold F2 sobre prob cruda (legacy, no calibrado).
    try:
        df_metrics = pd.read_csv(PATH_METRICS, index_col=0)
        threshold  = float(df_metrics.loc["best_f2_threshold", "valor"])
    except Exception:
        threshold = 0.5
    print(f"WARNING: sin calibrador ({e}). Usando prob cruda, threshold {threshold:.3f}.")


def apply_calibration(raw_prob, calibrator):
    """
    Mapea probabilidad cruda → calibrada (fix R1). Aplica Platt manualmente
    desde los coeficientes en JSON — sin dependencia de la versión de sklearn
    en el cluster (evita fallos de unpickle en serving).
        calibrated = sigmoid(a * logit(raw) + b)
    """
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

# Verificar alineación schema
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

# Re-castear categóricas como en el training (train_model_v4.prepare_xy)
if "subregion_id" in X.columns:
    X["subregion_id"] = X["subregion_id"].astype("category")
if "land_cover_cat" in X.columns:
    X["land_cover_cat"] = X["land_cover_cat"].astype("category")

raw_prob               = clf.predict_proba(X)[:, 1]
df_gold["risk_prob_raw"] = raw_prob
df_gold["risk_prob"]   = apply_calibration(raw_prob, calibrator)   # calibrada (R1)
df_gold["risk_alert"]  = (df_gold["risk_prob"] >= threshold).astype(int)
# Nivel de riesgo para visualización (0-100) — sobre la prob CALIBRADA
df_gold["risk_level"]  = (df_gold["risk_prob"] * 100).round(1)

print(f"\nDistribución de risk_prob (calibrada):")
print(df_gold["risk_prob"].describe())
print(f"   (cruda media {raw_prob.mean():.3f} → calibrada media {df_gold['risk_prob'].mean():.3f})")
print(f"\nAlertas (prob calibrada ≥ {threshold:.4f}): {df_gold['risk_alert'].sum():,} de {len(df_gold):,}")

# COMMAND ----------

# MAGIC %md ## 4 · Exportar JSON para el frontend
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

# Detalle clima/FWI por nodo y día para el dashboard (el front lo muestra en
# NodeDetail). El static (lat/lon/subregión/elevación) lo aporta la grilla en el
# front (aux_grid_pampa.csv), no se duplica acá. `dc` no es feature del v4
# (el modelo usa ffmc/dmc/isi/bui/fwi), por eso no se incluye.
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
            "risk_level": float(row["risk_level"]),
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

# Asegurar que el directorio existe
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
