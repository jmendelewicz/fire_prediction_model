# Databricks notebook source
# MAGIC %md
# MAGIC # ETL Landing - ERA5 via GEE
# MAGIC
# MAGIC Extrae variables climáticas ERA5-Land para los 2,266 nodos de la grilla
# MAGIC pampeana con agregación diaria diferenciada según Van Wagner (1987).
# MAGIC
# MAGIC **Fuente:** ECMWF/ERA5_LAND/HOURLY via Google Earth Engine
# MAGIC **Período:** 2022-01-01 → 2024-12-31
# MAGIC **Idempotente:** saltea meses ya procesados

# COMMAND ----------

# MAGIC %pip install earthengine-api --upgrade --quiet

# COMMAND ----------

# AUDIT fix A-5 (2026-05-16): paths relativos en lugar de hardcoded por usuario.
# MAGIC %run ../../00_setup/00_common_functions/gee_helpers

# COMMAND ----------

# MAGIC %run ../../00_setup/00_common_functions/weather_cleaners

# COMMAND ----------

import os
import time
import logging
import calendar
import pandas as pd
import numpy as np
from datetime import date, timedelta
import ee

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    datefmt='%H:%M:%S', force=True)
logger = logging.getLogger("ETL_ERA5")

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

GEE_PROJECT = "fire-risk-project-19-04"   ### ajustar

TABLE_GRID  = "fire_risk_project.00_landing.aux_grid_pampa"
PATH_ERA5   = "/Volumes/fire_risk_project/00_landing/era5_files"

DATE_START  = date(2022, 1, 1)
DATE_END    = date(2024, 12, 31)
SCALE       = 27000
SLEEP_MESES = 2
HORA_MEDIODIA_UTC = 15

BANDAS_MEDIODIA = {
    "temperature_2m":          "temperature_2m",
    "dewpoint_temperature_2m": "dewpoint_2m",
    "u_component_of_wind_10m": "wind_u_10m",
    "v_component_of_wind_10m": "wind_v_10m",
}
BANDAS_SUMA  = {"total_precipitation_hourly": "precipitation"}
BANDAS_MEDIA = {
    "surface_solar_radiation_downwards": "solar_radiation",
    "volumetric_soil_water_layer_1":     "soil_moisture_0_7cm",
    "volumetric_soil_water_layer_3":     "soil_moisture_28_100cm",
}

# COMMAND ----------

# MAGIC %md ## Inicialización

# COMMAND ----------

inicializar_gee(GEE_PROJECT)

df_grid = spark.table(TABLE_GRID).toPandas()
fc_grid = build_fc_puntos(df_grid)
logger.info(f"Grilla: {len(df_grid):,} nodos")

# COMMAND ----------

# MAGIC %md ## Función de extracción mensual ERA5

# COMMAND ----------

def extraer_mes_era5(year: int, month: int) -> pd.DataFrame:
    """Extrae ERA5-Land para un mes completo sobre todos los nodos."""
    todas_bandas = {**BANDAS_MEDIODIA, **BANDAS_SUMA, **BANDAS_MEDIA}
    last_day     = calendar.monthrange(year, month)[1]
    dias         = pd.date_range(f"{year}-{month:02d}-01", periods=last_day, freq="D")
    resultados   = []

    for dia in dias:
        dia_str  = dia.strftime("%Y-%m-%d")
        dia_next = (dia + timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            col_dia      = ee.ImageCollection("ECMWF/ERA5_LAND/HOURLY").filterDate(dia_str, dia_next)
            img_mediodia = (col_dia
                            .filter(ee.Filter.calendarRange(HORA_MEDIODIA_UTC, HORA_MEDIODIA_UTC, "hour"))
                            .select(list(BANDAS_MEDIODIA.keys())).first())
            img_precip   = col_dia.select(list(BANDAS_SUMA.keys())).sum()
            img_suelo    = col_dia.select(["volumetric_soil_water_layer_1",
                                           "volumetric_soil_water_layer_3"]).mean()
            img_solar    = col_dia.select(["surface_solar_radiation_downwards"]).sum()
            img_dia      = img_mediodia.addBands(img_precip).addBands(img_suelo).addBands(img_solar)

            muestras = img_dia.sampleRegions(
                collection=fc_grid, scale=SCALE, geometries=False, tileScale=4
            ).getInfo()

            for feat in muestras["features"]:
                p    = feat["properties"]
                fila = {"cell_id": p.get("cell_id"), "date": dia_str}
                for band_orig, band_new in todas_bandas.items():
                    fila[band_new] = p.get(band_orig)
                resultados.append(fila)

        except Exception as e:
            logger.warning(f"  Error en {dia_str}: {str(e)[:100]}")
            for _, row in df_grid.iterrows():
                fila = {"cell_id": row.cell_id, "date": dia_str}
                for band_new in todas_bandas.values():
                    fila[band_new] = None
                resultados.append(fila)

    return pd.DataFrame(resultados)

# COMMAND ----------

# MAGIC %md ## Loop de extracción mensual

# COMMAND ----------

meses = []
year, month = DATE_START.year, DATE_START.month
while date(year, month, 1) <= DATE_END:
    meses.append((year, month))
    year, month = siguiente_mes(year, month)

logger.info(f"Meses a procesar: {len(meses)} ({DATE_START} → {DATE_END})")
meses_ok, meses_error = 0, []

for year, month in meses:
    fname = f"era5_{year}_{month:02d}.csv"
    fpath = os.path.join(PATH_ERA5, fname)

    if os.path.exists(fpath):
        logger.info(f"  Ya existe: {fname} — saltando")
        meses_ok += 1
        continue

    logger.info(f"Procesando {year}-{month:02d}...")
    try:
        df_mes = extraer_mes_era5(year, month)
        df_mes = calcular_variables_derivadas_era5(df_mes)   # ← weather_cleaners
        guardar_en_volume(df_mes, PATH_ERA5, fname)           # ← gee_helpers
        meses_ok += 1
    except Exception as e:
        logger.error(f"Error en {year}-{month:02d}: {str(e)[:200]}")
        meses_error.append(f"{year}-{month:02d}")
    time.sleep(SLEEP_MESES)

logger.info(f"Meses procesados: {meses_ok}/{len(meses)}")
if meses_error:
    logger.warning(f"Meses con error: {meses_error}")
else:
    logger.info("Sin errores")

dbutils.notebook.exit(f"OK: {meses_ok}/{len(meses)} meses procesados")
