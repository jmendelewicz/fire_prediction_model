# Databricks notebook source
# MAGIC %md
# MAGIC # Motor de Inferencia en la Nube
# MAGIC
# MAGIC Este script realiza la predicción de riesgo de incendios para los próximos 7 días utilizando el modelo XGBoost entrenado.
# MAGIC
# MAGIC **Pasos:**
# MAGIC 1. Carga del modelo XGBoost (JSON).
2. Unificación de datos (Forecast + Estáticos + NDVI).
3. Cálculo de FWI (Simplificado o Delta para el forecast).
4. Inferencia probabilística.
5. Exportación a JSON para el Frontend.

# COMMAND ----------

import pandas as pd
import numpy as np
import xgboost as xgb
import json
from datetime import datetime

# CONFIGURACIÓN
CATALOG = "fire_risk_project"
MODEL_PATH = f"/Volumes/{CATALOG}/03_gold/training_dataset_v2/xgboost_v2.json"
TABLE_STATIC = f"{CATALOG}.02_silver.static_features_silver"
TABLE_NDVI = f"{CATALOG}.02_silver.ndvi_silver"
PATH_FORECAST = f"/Volumes/{CATALOG}/00_landing/open_meteo_forecast"
OUTPUT_JSON = f"/Volumes/{CATALOG}/03_gold/predictions_ui.json"

# COMMAND ----------

# MAGIC %md ## 1 · Cargar Datos

# COMMAND ----------

# Cargar el último forecast
files = dbutils.fs.ls(PATH_FORECAST)
latest_forecast = sorted([f.path for f in files if "forecast_" in f.name])[-1]
print(f"Cargando forecast: {latest_forecast}")

df_forecast = pd.read_csv(latest_forecast.replace("dbfs:", "/dbfs"))
df_static = spark.table(TABLE_STATIC).toPandas()
# Tomamos el último NDVI disponible por nodo
df_ndvi = spark.sql(f"SELECT cell_id, ndvi FROM {TABLE_NDVI} WHERE fecha = (SELECT MAX(fecha) FROM {TABLE_NDVI})").toPandas()

# Join de features
df_gold = df_forecast.merge(df_static, on="cell_id", how="left")
df_gold = df_gold.merge(df_ndvi, on="cell_id", how="left")

# COMMAND ----------

# MAGIC %md ## 2 · Feature Engineering (FWI y otros)

# COMMAND ----------

# Para el forecast, simplificamos o usamos el último FWI conocido como base
# Aquí podrías cargar el Gold Checkpoint para tener el acumulado del FWI
# Por ahora definimos las features que el modelo v2 espera:
feature_cols = [
    'temperature_2m', 'relative_humidity', 'precipitation', 'wind_speed_10m',
    'vpd_kpa', 'solar_radiation', 'soil_moisture_0_7cm', 'soil_moisture_28_100cm',
    'ndvi', 'elevation', 'slope', 'aspect', 'dist_road_km', 'pop_density_km2',
    'land_cover_cat'
]

# Calculamos VPD si no viene en el forecast
def calculate_vpd(temp, rh):
    es = 0.6108 * np.exp((17.27 * temp) / (temp + 237.3))
    ea = es * (rh / 100)
    return es - ea

df_gold['vpd_kpa'] = calculate_vpd(df_gold['temperature_2m'], df_gold['relative_humidity'])

# Mock de columnas faltantes en forecast (pueden ser constantes o promedios)
df_gold['solar_radiation'] = 200.0  # W/m2 promedio
df_gold['soil_moisture_0_7cm'] = 0.3
df_gold['soil_moisture_28_100cm'] = 0.3
df_gold['land_cover_cat'] = 2 # Por defecto Veg. Natural si no cruza

# COMMAND ----------

# MAGIC %md ## 3 · Inferencia

# COMMAND ----------

# Cargar Modelo
clf = xgb.XGBClassifier()
clf.load_model(MODEL_PATH.replace("dbfs:", "/dbfs"))

# Ejecutar Predicción
X = df_gold[feature_cols]
df_gold['risk_prob'] = clf.predict_proba(X)[:, 1]
df_gold['risk_level'] = (df_gold['risk_prob'] * 100).round(1)

# COMMAND ----------

# MAGIC %md ## 4 · Exportar para UI

# COMMAND ----------

# Formatear JSON: { cell_id: { date: prob, ... }, ... }
output = []
for cell_id, group in df_gold.groupby('cell_id'):
    preds = {}
    for _, row in group.iterrows():
        preds[str(row['date'])] = row['risk_level']
    
    output.append({
        "cell_id": str(cell_id),
        "predictions": preds
    })

with open(OUTPUT_JSON.replace("dbfs:", "/dbfs"), 'w') as f:
    json.dump(output, f)

print(f"Predicciones exportadas a: {OUTPUT_JSON}")
