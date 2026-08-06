# Databricks notebook source
# MAGIC %md
# MAGIC # ETL Landing - MODIS Land Cover via GEE
# MAGIC
# MAGIC Extrae cobertura del suelo (MCD12Q1, anual, clasificación IGBP)
# MAGIC para los nodos válidos de `aux_grid_pampa`.
# MAGIC
# MAGIC **Producto:** MODIS/061/MCD12Q1 — un mapa por año.
# MAGIC **Categorización:**
# MAGIC - 0 = Urbano/Otro (IGBP: 13, 0, 15, 16, 17)
# MAGIC - 1 = Cultivo (IGBP: 12, 14)
# MAGIC - 2 = Vegetación Natural (resto: bosques, pastizales, arbustales)
# MAGIC
# MAGIC **Idempotente:** si el CSV ya contiene el último año disponible, no re-descarga.
# MAGIC
# MAGIC **Output:** `/Volumes/fire_risk_project/00_landing/modis_static/land_cover_2022_2024.csv`

# COMMAND ----------

# MAGIC %pip install earthengine-api --quiet

# COMMAND ----------

# MAGIC %run ../../00_setup/00_common_functions/gee_helpers

# COMMAND ----------

import pandas as pd
import numpy as np
from pyspark.sql import functions as F
import ee
import os
import time
from datetime import datetime

GEE_PROJECT = "fire-risk-project-19-04"

TABLE_GRID  = "fire_risk_project.00_landing.aux_grid_pampa"
PATH_LC     = "/Volumes/fire_risk_project/00_landing/modis_static"
FILENAME    = "land_cover_2022_2024.csv"

YEAR_START  = 2022
YEAR_END    = 2024
SCALE_LC    = 500

LAT_MIN, LAT_MAX = -42.0, -28.0
LON_MIN, LON_MAX = -68.0, -56.0

# COMMAND ----------

# MAGIC %md ## Idempotencia
# MAGIC
# MAGIC Verifica si el CSV ya existe y contiene todos los años del rango.
# MAGIC Si falta algún año, re-descarga completo.

# COMMAND ----------

full_path = f"{PATH_LC}/{FILENAME}"

if os.path.exists(full_path):
    df_existing = pd.read_csv(full_path, usecols=["year"])
    anios_presentes = set(pd.to_numeric(df_existing["year"], errors="coerce").dropna().astype(int))
    anios_requeridos = set(range(YEAR_START, YEAR_END + 1))
    faltantes = anios_requeridos - anios_presentes

    if not faltantes:
        print(f"Land Cover completo: años {sorted(anios_presentes)} ya en {full_path}")
        dbutils.notebook.exit(f"SKIP: {FILENAME} ya tiene años {YEAR_START}-{YEAR_END}.")
    else:
        print(f"Faltan años: {sorted(faltantes)} — re-descargando todo")
else:
    print(f"Archivo no existe — descargando {YEAR_START}-{YEAR_END}")

# COMMAND ----------

# MAGIC %md ## Inicialización

# COMMAND ----------

inicializar_gee(GEE_PROJECT)

df_grid = (
    spark.table(TABLE_GRID)
    .filter(F.col("is_valid").cast("string") == "true")
    .select("cell_id", "latitude", "longitude")
    .toPandas()
)

REGION    = ee.Geometry.Rectangle([LON_MIN, LAT_MIN, LON_MAX, LAT_MAX])
fc_puntos = build_fc_puntos(df_grid)

print(f"Nodos: {len(df_grid):,}")

# COMMAND ----------

# MAGIC %md ## Extracción Land Cover (MCD12Q1 — anual)

# COMMAND ----------

IGBP_MAP = {
    1: 2,
    2: 2,
    3: 2,
    4: 2,
    5: 2,
    6: 2,
    7: 2,
    8: 2,
    9: 2,
    10: 2,
    11: 2,
    12: 1,
    13: 0,
    14: 1,
    15: 0,
    16: 0,
    17: 0,
    0: 0,
}

dfs_lc = []

for year in range(YEAR_START, YEAR_END + 1):
    print(f"\n--- Año {year} ---")
    try:
        img = (
            ee.ImageCollection("MODIS/061/MCD12Q1")
            .filterDate(f"{year}-01-01", f"{year}-12-31")
            .filterBounds(REGION)
            .first()
            .select(["LC_Type1"])
        )

        sampled = img.reduceRegions(
            collection=fc_puntos,
            reducer=ee.Reducer.mode(),
            scale=SCALE_LC,
            tileScale=4
        )

        rows = [
            {
                "cell_id": f["properties"]["cell_id"],
                "fecha": f"{year}-01-01",
                "year": year,
                "land_cover_type": int(f["properties"].get("mode", 0)),
            }
            for f in sampled.getInfo()["features"]
        ]

        df_year = pd.DataFrame(rows)
        df_year["land_cover_cat"] = df_year["land_cover_type"].map(IGBP_MAP).fillna(0).astype(int)

        dfs_lc.append(df_year)
        print(f"  {len(df_year):,} nodos | distribución: {df_year['land_cover_cat'].value_counts().to_dict()}")

    except Exception as e:
        print(f"  ERROR año {year}: {e}")
    time.sleep(1)

# COMMAND ----------

# MAGIC %md ## Guardar CSV

# COMMAND ----------

df_lc = pd.concat(dfs_lc, ignore_index=True)

print(f"\nTotal: {len(df_lc):,} filas")
print(f"Años:  {sorted(df_lc['year'].unique())}")
print(f"Nodos: {df_lc['cell_id'].nunique():,}")
print(f"\nDistribución por año y categoría:")
print(df_lc.groupby(["year", "land_cover_cat"]).size().unstack(fill_value=0))

os.makedirs(PATH_LC, exist_ok=True)
guardar_en_volume(df_lc, PATH_LC, FILENAME)

print(f"\nGuardado: {full_path}")
dbutils.notebook.exit(f"OK: {FILENAME} — {len(df_lc):,} filas, años {YEAR_START}-{YEAR_END}")
