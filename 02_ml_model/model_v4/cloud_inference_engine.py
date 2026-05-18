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

TABLE_FORECAST = f"{CATALOG}.03_gold.forecast_gold_temp"
PATH_MODEL     = f"/Volumes/{CATALOG}/03_gold/training_dataset_v2/xgboost_v4.json"
PATH_FEATS     = f"/Volumes/{CATALOG}/03_gold/training_dataset_v2/feature_cols_v4.pkl"
PATH_METRICS   = f"/Volumes/{CATALOG}/03_gold/training_dataset_v2/metricas_v4.csv"
PATH_OUTPUT    = f"/Volumes/{CATALOG}/03_gold/outputs/predictions_ui.json"

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

# Threshold operativo — lee del metricas CSV el F2-optimal (más recall, menos FN).
# Decisión defendible: en alerta temprana de incendios, FN > FP (un incendio
# no detectado cuesta más que un falso positivo en una celda lejana).
try:
    df_metrics = pd.read_csv(PATH_METRICS, index_col=0)
    threshold  = float(df_metrics.loc["best_f2_threshold", "valor"])
    print(f"Threshold operativo (F2-optimal): {threshold:.3f}")
except Exception as e:
    threshold = 0.5
    print(f"WARNING: no se pudo leer best_f2_threshold ({e}). Usando 0.5 como fallback.")

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

df_gold["risk_prob"]  = clf.predict_proba(X)[:, 1]
df_gold["risk_alert"] = (df_gold["risk_prob"] >= threshold).astype(int)
# Nivel de riesgo para visualización (0-100)
df_gold["risk_level"] = (df_gold["risk_prob"] * 100).round(1)

print(f"\nDistribución de risk_prob:")
print(df_gold["risk_prob"].describe())
print(f"\nAlertas (prob ≥ {threshold:.3f}): {df_gold['risk_alert'].sum():,} de {len(df_gold):,}")

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

nodes = []
for cell_id, group in df_gold.groupby("cell_id"):
    preds = {}
    for _, row in group.iterrows():
        preds[str(row["date"])] = {
            "risk_level": float(row["risk_level"]),
            "alert":      int(row["risk_alert"]),
        }
    nodes.append({"cell_id": str(cell_id), "predictions": preds})

output = {
    "generated_at":  datetime.utcnow().isoformat() + "Z",
    "model_version": "v4",
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
