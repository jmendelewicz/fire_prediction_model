# Databricks notebook source
# MAGIC %md
# MAGIC # ETL Landing - MODIS NDVI via GEE
# MAGIC
# MAGIC Extrae NDVI (MOD13A2, compuesto 16 días) para los 2,266 nodos.
# MAGIC **Solo NDVI** — Land Cover se descarga en `grid_download_static_data` (setup).
# MAGIC
# MAGIC **Idempotente:** si el archivo ya existe, no vuelve a descargar.

# COMMAND ----------

# MAGIC %pip install earthengine-api --quiet

# COMMAND ----------

# MAGIC %run /Workspace/Users/jmendelewicz02@gmail.com/fire_prediction_model/00_setup/00_common_functions/gee_helpers

# COMMAND ----------

import pandas as pd
import numpy as np
from pyspark.sql import functions as F
import ee
import os
import time
from datetime import datetime

GEE_PROJECT = "fire-risk-project-19-04"   ###

TABLE_GRID  = "fire_risk_project.00_landing.aux_grid_pampa"
PATH_NDVI   = "/Volumes/fire_risk_project/00_landing/modis_ndvi"

START_DATE  = "2022-01-01"
END_DATE    = "2024-12-31"
SCALE       = 5000
LAT_MIN, LAT_MAX = -42.0, -28.0
LON_MIN, LON_MAX = -68.0, -56.0

# COMMAND ----------

# MAGIC %md ## Idempotencia

# COMMAND ----------

ndvi_exists = os.path.exists(f"{PATH_NDVI}/ndvi_2022_2024.csv")

if ndvi_exists:
    print("Archivo NDVI ya existe — saliendo.")
    dbutils.notebook.exit("SKIP: ndvi_2022_2024.csv ya descargado.")

print("NDVI: FALTA — iniciando extracción")

# COMMAND ----------

# MAGIC %md ## Inicialización

# COMMAND ----------

inicializar_gee(GEE_PROJECT)   # ← gee_helpers

df_grid = (
    spark.table(TABLE_GRID)
    .filter(F.col("is_valid").cast("string") == "true")
    .select("cell_id", "latitude", "longitude")
    .toPandas()
)

REGION    = ee.Geometry.Rectangle([LON_MIN, LAT_MIN, LON_MAX, LAT_MAX])
fc_puntos = build_fc_puntos(df_grid)   # ← gee_helpers

print(f"Nodos: {len(df_grid):,}")

# COMMAND ----------

# MAGIC %md ## Extracción NDVI (MOD13A2 — compuesto 16 días)

# COMMAND ----------

col_ndvi = (ee.ImageCollection("MODIS/061/MOD13A2")
            .filterDate(START_DATE, END_DATE)
            .filterBounds(REGION)
            .select(["NDVI"]))
n        = col_ndvi.size().getInfo()
img_list = col_ndvi.toList(n)
fechas   = [
    datetime.utcfromtimestamp(t / 1000).strftime("%Y-%m-%d")
    for t in col_ndvi.aggregate_array("system:time_start").getInfo()
]
print(f"Imágenes NDVI: {n}")

dfs_ndvi = []
for i in range(n):
    try:
        img     = ee.Image(img_list.get(i)).multiply(0.0001)
        sampled = img.select(["NDVI"]).reduceRegions(
            collection=fc_puntos,
            reducer=ee.Reducer.mean(),
            scale=SCALE,
            tileScale=4
        )
        rows = [
            {"cell_id": f["properties"]["cell_id"],
             "fecha":   fechas[i],
             "ndvi":    f["properties"].get("mean")}
            for f in sampled.getInfo()["features"]
        ]
        df = pd.DataFrame(rows)
        df["ndvi"] = df["ndvi"].clip(-1, 1)
        dfs_ndvi.append(df)
        if i % 5 == 0:
            print(f"  [{i+1}/{n}] {fechas[i]}")
    except Exception as e:
        print(f"  ERROR {fechas[i]}: {e}")
    time.sleep(0.5)

df_ndvi = pd.concat(dfs_ndvi, ignore_index=True)
guardar_en_volume(df_ndvi, PATH_NDVI, "ndvi_2022_2024.csv")   # ← gee_helpers

print(f"NDVI: {len(df_ndvi):,} filas | {df_ndvi['cell_id'].nunique():,} nodos | {n} compuestos")
dbutils.notebook.exit(f"OK: ndvi_2022_2024.csv — {len(df_ndvi):,} filas")
