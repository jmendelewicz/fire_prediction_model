# Databricks notebook source
# MAGIC %md
# MAGIC # Gold Training - Parte 1: Joins + FWI
# MAGIC
# MAGIC Une todas las fuentes Silver, calcula el sistema FWI canadiense
# MAGIC (Van Wagner 1987) por nodo en Pandas, y guarda un checkpoint CSV.
# MAGIC
# MAGIC **Inputs:**
# MAGIC - `02_silver.silver_era5`      — clima mediodía + topo + subregion + dist_road + pop_density
# MAGIC - `02_silver.silver_nasa_firms` — focos VIIRS → target fire_occurred
# MAGIC - `02_silver.ndvi_silver`       — NDVI diario (forward-filled)
# MAGIC - `02_silver.land_cover_silver` — cobertura anual
# MAGIC
# MAGIC **Output:** checkpoint CSV en volumen → input de Parte 2

# COMMAND ----------

import pandas as pd
import numpy as np
import logging
from pyspark.sql import functions as F

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    force=True
)
logger = logging.getLogger("GOLD_P1")

# COMMAND ----------

# MAGIC %run /Workspace/Users/jmendelewicz02@gmail.com/fire_prediction_model/00_setup/00_common_functions/fwi_calculator

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

TABLE_ERA5   = "fire_risk_project.02_silver.silver_era5"
TABLE_NASA   = "fire_risk_project.02_silver.silver_nasa_firms"
TABLE_NDVI   = "fire_risk_project.02_silver.ndvi_silver"
TABLE_LC     = "fire_risk_project.02_silver.land_cover_silver"
# Nota: static_features_silver fue eliminada.
# dist_road_km y pop_density_km2 ahora se propagan desde aux_grid_pampa vía silver_era5.

PATH_CHECKPOINT = "/Volumes/fire_risk_project/03_gold/training_dataset_v2/gold_checkpoint.csv"

DATE_START = "2022-01-01"
DATE_END   = "2024-12-31"

# Valores iniciales FWI - Van Wagner (1987)
FFMC_INIT = 85.0
DMC_INIT  = 6.0
DC_INIT   = 15.0

# COMMAND ----------

# MAGIC %md ## Cargar y unificar fuentes en Spark

# COMMAND ----------

# ERA5 base - clima + topo + subregion + features estáticas
df_era5 = (
    spark.read.table(TABLE_ERA5)
    .filter(F.col("fecha_join").between(DATE_START, DATE_END))
    .select(
        "cell_id", "fecha_join",
        "temperature_2m", "relative_humidity", "precipitation",
        "wind_speed_10m", "vpd_kpa", "solar_radiation",
        "soil_moisture_0_7cm", "soil_moisture_28_100cm",
        "subregion_id", "elevation", "slope", "aspect",
        "dist_road_km", "pop_density_km2",   # desde aux_grid_pampa vía silver_era5
    )
)

# Target: fire_occurred por (cell_id, fecha)
df_fire = (
    spark.read.table(TABLE_NASA)
    .filter(F.col("fecha_join").between(DATE_START, DATE_END))
    .filter(F.col("type") == 0)   # solo vegetación
    .groupBy("cell_id", "fecha_join")
    .agg(F.lit(1).alias("fire_occurred"))
)

# NDVI diario 
df_ndvi = (
    spark.read.table(TABLE_NDVI)
    .withColumnRenamed("fecha", "fecha_join")
    .select("cell_id", "fecha_join", "ndvi")
)

# Land Cover anual
df_lc = (
    spark.read.table(TABLE_LC)
    .select("cell_id", "year", "land_cover_cat")
)

logger.info(f"ERA5:    {df_era5.count():,} registros")
logger.info(f"Fire:    {df_fire.count():,} eventos")

# COMMAND ----------

# MAGIC %md ## 2 · Joins

# COMMAND ----------

df = (
    df_era5
    # Target
    .join(df_fire, on=["cell_id", "fecha_join"], how="left")
    .withColumn("fire_occurred", F.coalesce(F.col("fire_occurred"), F.lit(0)))
    # NDVI diario
    .join(df_ndvi, on=["cell_id", "fecha_join"], how="left")
    # Land Cover por año
    .withColumn("year", F.year("fecha_join"))
    .join(df_lc, on=["cell_id", "year"], how="left")
    .fillna({"land_cover_cat": 0, "pop_density_km2": 0.0})
    .drop("year")
)

logger.info(f"Dataset unificado: {df.count():,} filas")

# COMMAND ----------

# MAGIC %md ## Convertir a Pandas para FWI secuencial

# COMMAND ----------

logger.info("Convirtiendo a Pandas...")
df_pd = df.toPandas()
df_pd["fecha_join"] = pd.to_datetime(df_pd["fecha_join"])
df_pd["mes"]        = df_pd["fecha_join"].dt.month
df_pd["dia_anio"]   = df_pd["fecha_join"].dt.dayofyear
df_pd = df_pd.sort_values(["cell_id", "fecha_join"]).reset_index(drop=True)

logger.info(f"Pandas: {len(df_pd):,} filas, {df_pd['cell_id'].nunique():,} nodos")

# COMMAND ----------

# MAGIC %md ## Funciones FWI (Van Wagner & Pickett (1985), Van Wagner (1987))

# COMMAND ----------

def calcular_ffmc(temp, rh, wind, rain, ffmc_prev):
    """
    Fine Fuel Moisture Code — humedad de combustibles finos (hojas, pasto seco).
    Responde rápido a cambios meteorológicos (horas).
    Inputs: temp (°C), rh (%), wind (km/h), rain (mm/24h), ffmc_prev
    """
    mo = 147.2 * (101 - ffmc_prev) / (59.5 + ffmc_prev)
    if rain > 0.5:
        rf = rain - 0.5
        mo = mo + 42.5 * rf * np.exp(-100 / (251 - mo)) * (1 - np.exp(-6.93 / rf))
        if mo > 150:
            mo += 0.0015 * (mo - 150)**2 * rf**0.5
        mo = min(mo, 250)
    ed = (0.942 * rh**0.679 + 11 * np.exp((rh - 100) / 10)
          + 0.18 * (21.1 - temp) * (1 - np.exp(-0.115 * rh)))
    ew = (0.618 * rh**0.753 + 10 * np.exp((rh - 100) / 10)
          + 0.18 * (21.1 - temp) * (1 - np.exp(-0.115 * rh)))
    if mo > ed:
        ko = 0.424 * (1 - (rh / 100)**1.7) + 0.0694 * wind**0.5 * (1 - (rh / 100)**8)
        kd = ko * 0.581 * np.exp(0.0365 * temp)
        m  = ed + (mo - ed) * 10**(-kd)
    elif mo < ew:
        kl = 0.424 * (1 - ((100 - rh) / 100)**1.7) + 0.0694 * wind**0.5 * (1 - ((100 - rh) / 100)**8)
        kw = kl * 0.581 * np.exp(0.0365 * temp)
        m  = ew - (ew - mo) * 10**(-kw)
    else:
        m = mo
    return max(0.0, min(101.0, 59.5 * (250 - m) / (147.2 + m)))


def calcular_dmc(temp, rh, rain, dmc_prev, mes):
    """
    Duff Moisture Code — humedad de capas orgánicas (5-10 cm).
    Responde en días.
    Tabla DL ajustada para Hemisferio Sur (~35°S, Pampa).
    Fuente: Van Wagner (1987) tabla original NH desplazada 6 meses.
    Para ~35°S: dic-feb=verano (DL alto), jun-ago=invierno (DL negativo).
    """
    if rain > 1.5:
        re = 0.92 * rain - 1.27
        mo = 20 + np.exp(5.6348 - dmc_prev / 43.43)
        if dmc_prev <= 33:
            b = 100 / (0.5 + 0.3 * dmc_prev)
        elif dmc_prev <= 65:
            b = 14 - 1.3 * np.log(dmc_prev)
        else:
            b = 6.2 * np.log(dmc_prev) - 17.2
        mr       = mo + 1000 * re / (48.77 + b * re)
        pr       = 244.72 - 43.43 * np.log(mr - 20)
        dmc_prev = max(pr, 0)
    # Hemisferio Sur ~35°S: valores positivos en verano austral (dic-ene-feb)
    DL = [6.4, 5.0, 2.4, 0.4, -1.6, -1.6, -1.6, -1.6, -1.1, 0.9, 3.8, 5.8]
    #      ene  feb  mar  abr   may   jun   jul   ago   sep  oct  nov  dic
    if temp < -1.1:
        return max(dmc_prev, 0.001)
    k = 1.894 * (temp + 1.1) * (100 - rh) * DL[mes - 1] * 1e-6
    return max(0.001, dmc_prev + 100 * k)


def calcular_dc(temp, rain, dc_prev, mes):
    """
    Drought Code — humedad profunda del suelo (>20 cm).
    Responde en semanas/meses. Indicador de sequía prolongada.
    Tabla LF ajustada para Hemisferio Sur (~35°S, Pampa).
    Fuente: Van Wagner (1987) tabla original NH desplazada 6 meses.
    """
    if rain > 2.8:
        rd      = 0.83 * rain - 1.27
        qo      = 800 * np.exp(-dc_prev / 400)
        qr      = max(qo + 3.937 * rd, 0.001)
        dr      = 400 * np.log(800 / qr)
        dc_prev = max(dr, 0)
    # Hemisferio Sur ~35°S: valores positivos en verano austral (dic-ene-feb)
    LF = [6.4, 5.0, 2.4, 0.4, -1.6, -1.6, -1.6, -1.6, -1.1, 0.9, 3.8, 5.8]
    #      ene  feb  mar  abr   may   jun   jul   ago   sep  oct  nov  dic
    if temp < -2.8:
        return max(dc_prev, 0.001)
    v = max(0.36 * (temp + 2.8) + LF[mes - 1], 0)
    return max(0.001, dc_prev + 0.5 * v)


def calcular_isi(wind, ffmc):
    """Initial Spread Index — velocidad potencial de propagación."""
    fm = 147.2 * (101 - ffmc) / (59.5 + ffmc)
    ff = 19.115 * np.exp(-0.1386 * fm) * (1 + fm**5.31 / 4.93e7)
    return 0.208 * ff * np.exp(0.05039 * wind)


def calcular_bui(dmc, dc):
    """Buildup Index — total de combustible disponible."""
    denom = dmc + 0.4 * dc
    if denom == 0:
        return 0.0
    if dmc <= 0.4 * dc:
        return 0.8 * dmc * dc / denom
    return dmc - (1 - 0.8 * dc / denom) * (0.92 + (0.0114 * dmc)**1.7)


def calcular_fwi(isi, bui):
    """Fire Weather Index — intensidad potencial del incendio."""
    fd = 0.626 * bui**0.809 + 2 if bui <= 80 else 1000 / (25 + 108.64 * np.exp(-0.023 * bui))
    b  = 0.1 * isi * fd
    return np.exp(2.72 * (0.434 * np.log(b))**0.647) if b > 1 else b


def calcular_fwi_serie(df_nodo: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula la serie temporal completa de FWI para un nodo.
    El cálculo es secuencial — cada día depende del estado del día anterior.

    Args:
        df_nodo: DataFrame ordenado por fecha para un único cell_id

    Returns:
        DataFrame con columnas ffmc, dmc, isi, bui, fwi agregadas
    """
    n             = len(df_nodo)
    ffmc_arr      = np.full(n, np.nan)
    dmc_arr       = np.full(n, np.nan)
    isi_arr       = np.full(n, np.nan)
    bui_arr       = np.full(n, np.nan)
    fwi_arr       = np.full(n, np.nan)
    ffmc_prev     = FFMC_INIT
    dmc_prev      = DMC_INIT
    dc_prev       = DC_INIT

    for i, row in enumerate(df_nodo.itertuples()):
        wind_kmh  = row.wind_speed_10m * 3.6   # ERA5 en m/s → FWI usa km/h
        ffmc_i    = calcular_ffmc(row.temperature_2m, row.relative_humidity,
                                  wind_kmh, row.precipitation, ffmc_prev)
        dmc_i     = calcular_dmc(row.temperature_2m, row.relative_humidity,
                                 row.precipitation, dmc_prev, row.mes)
        dc_i      = calcular_dc(row.temperature_2m, row.precipitation, dc_prev, row.mes)
        isi_i     = calcular_isi(wind_kmh, ffmc_i)
        bui_i     = calcular_bui(dmc_i, dc_i)
        fwi_i     = calcular_fwi(isi_i, bui_i)

        ffmc_arr[i] = round(ffmc_i, 2)
        dmc_arr[i]  = round(dmc_i,  2)
        isi_arr[i]  = round(isi_i,  2)
        bui_arr[i]  = round(bui_i,  2)
        fwi_arr[i]  = round(fwi_i,  2)

        ffmc_prev = ffmc_i
        dmc_prev  = dmc_i
        dc_prev   = dc_i

    df_out         = df_nodo.copy()
    df_out["ffmc"] = ffmc_arr
    df_out["dmc"]  = dmc_arr
    df_out["isi"]  = isi_arr
    df_out["bui"]  = bui_arr
    df_out["fwi"]  = fwi_arr
    return df_out

# COMMAND ----------

# MAGIC %md ## 5 · Calcular FWI por nodo

# COMMAND ----------

logger.info("Calculando FWI por nodo...")
inicio    = pd.Timestamp.now()
nodos     = df_pd["cell_id"].unique()
total     = len(nodos)
resultados = []

for i, nodo in enumerate(nodos):
    df_nodo = df_pd[df_pd["cell_id"] == nodo].copy()
    resultados.append(calcular_fwi_serie(df_nodo))
    if (i + 1) % 200 == 0 or (i + 1) == total:
        elapsed = (pd.Timestamp.now() - inicio).seconds / 60
        logger.info(f"  [{i+1}/{total}] {elapsed:.1f} min")

df_fwi = pd.concat(resultados, ignore_index=True)
logger.info(f"FWI calculado: {len(df_fwi):,} filas")

# COMMAND ----------

# MAGIC %md ## 6 · Estacionalidad y días secos

# COMMAND ----------

# Codificación circular — captura periodicidad sin discontinuidad dic/ene
df_fwi["mes_sin"]  = np.sin(2 * np.pi * df_fwi["mes"] / 12)
df_fwi["mes_cos"]  = np.cos(2 * np.pi * df_fwi["mes"] / 12)
df_fwi["dia_sin"]  = np.sin(2 * np.pi * df_fwi["dia_anio"] / 365)
df_fwi["dia_cos"]  = np.cos(2 * np.pi * df_fwi["dia_anio"] / 365)

# Días consecutivos sin lluvia — < 0.1mm = día seco
def dias_sin_lluvia(serie: pd.Series) -> pd.Series:
    resultado, contador = [], 0
    for v in serie:
        contador = contador + 1 if v <= 0.1 else 0
        resultado.append(contador)
    return pd.Series(resultado, index=serie.index)

df_fwi["dias_secos"] = (
    df_fwi.groupby("cell_id")["precipitation"]
    .transform(dias_sin_lluvia)
)

logger.info("Estacionalidad y días secos calculados.")

# COMMAND ----------

# MAGIC %md ## Checkpoint CSV

# COMMAND ----------

# Guardar antes de cualquier operación Spark — protege el trabajo si la sesión expira
COLS_CHECKPOINT = [
    "cell_id", "fecha_join", "fire_occurred",
    "subregion_id", "elevation", "slope", "aspect",
    "dist_road_km", "land_cover_cat", "pop_density_km2",
    "mes_sin", "mes_cos", "dia_sin", "dia_cos",
    "temperature_2m", "relative_humidity", "wind_speed_10m",
    "precipitation", "solar_radiation",
    "soil_moisture_0_7cm", "soil_moisture_28_100cm",
    "ndvi", "vpd_kpa",
    "ffmc", "dmc", "bui", "isi", "fwi",
    "dias_secos",
]

df_fwi[COLS_CHECKPOINT].to_csv(PATH_CHECKPOINT, index=False)
logger.info(f"Checkpoint guardado: {len(df_fwi):,} filas → {PATH_CHECKPOINT}")
logger.info("Siguiente paso: 04_build_gold_p2.py")
