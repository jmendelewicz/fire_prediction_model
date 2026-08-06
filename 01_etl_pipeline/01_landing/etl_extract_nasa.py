# Databricks notebook source
# MAGIC %md
# MAGIC # Extracción NASA FIRMS — VIIRS SNPP
# MAGIC
# MAGIC Descarga focos de incendio activos desde la API de NASA FIRMS
# MAGIC para la región pampeana argentina.
# MAGIC
# MAGIC **Fuente:**  NASA FIRMS — VIIRS SNPP Standard Processing (SP)
# MAGIC **Output:**  `/Volumes/fire_risk_project/00_landing/nasa_files/nasa_YYYYMMDD.csv`
# MAGIC **Período:** 2022-01-01 → 2024-12-31
# MAGIC
# MAGIC **Requisito:**
# MAGIC   API key de NASA FIRMS guardada como Databricks Secret:

# COMMAND ----------

import requests
import os
import time
import pandas as pd
from datetime import datetime, date, timedelta
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    force=True
)
logger = logging.getLogger("ETL_NASA")

# COMMAND ----------

# MAGIC %md ## Configuración

# COMMAND ----------

try:
    NASA_API_KEY = dbutils.secrets.get(scope="fire-risk", key="nasa_firms_api_key")
except Exception as e:
    raise RuntimeError(
        "NASA FIRMS API key no encontrada en Databricks Secrets. "
        "Configurar con: databricks secrets put --scope fire-risk --key nasa_firms_api_key. "
        f"Error original: {e}"
    )

SOURCE = "VIIRS_SNPP_SP"
DAYS_PER_REQ = 5

AREA = "-68.0,-42.0,-56.0,-28.0"

PATH_NASA = "/Volumes/fire_risk_project/00_landing/nasa_files"

DATE_START = date(2022, 1, 1)
DATE_END   = date(2024, 12, 31)

FORCE_REDOWNLOAD = False

# COMMAND ----------

# MAGIC %md ## Función de extracción

# COMMAND ----------

def etl_nasa(
    start_date: date,
    end_date: date,
    force: bool = False
) -> None:
    total_days = (end_date - start_date).days
    total_req  = total_days // DAYS_PER_REQ + 1

    archivos_guardados = 0
    archivos_salteados = 0
    archivos_error     = 0
    focos_totales      = 0
    req_actual         = 0
    inicio             = time.time()
    current_date       = start_date

    logger.info(f"Inicio extracción NASA FIRMS")
    logger.info(f"Período: {start_date} → {end_date}  |  Área: {AREA}")
    logger.info(f"Force redownload: {force}")

    while current_date <= end_date:
        req_actual += 1
        file_name  = f"nasa_{current_date.strftime('%Y%m%d')}.csv"
        final_path = os.path.join(PATH_NASA, file_name)

        if not force and os.path.exists(final_path):
            archivos_salteados += 1
            current_date += timedelta(days=DAYS_PER_REQ)
            continue

        url = (
            f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
            f"{NASA_API_KEY}/{SOURCE}/{AREA}/{DAYS_PER_REQ}/{current_date}"
        )

        try:
            response = requests.get(url, timeout=30)

            if response.status_code == 200 and "Invalid" not in response.text:
                n_focos = max(0, len(response.text.strip().split("\n")) - 1)
                focos_totales += n_focos

                with open(final_path, "w") as f:
                    f.write(response.text)

                archivos_guardados += 1

                if req_actual % 50 == 0 or n_focos > 0:
                    elapsed = (time.time() - inicio) / 60
                    logger.info(
                        f"[{req_actual}/{total_req}] {file_name} | "
                        f"{n_focos} focos | "
                        f"{req_actual/total_req*100:.1f}% | "
                        f"{elapsed:.1f} min"
                    )

            elif response.status_code == 429:
                logger.warning("Rate limit alcanzado. Esperando 60s...")
                time.sleep(60)
                continue

            else:
                logger.warning(
                    f"Sin datos para {current_date} "
                    f"(status={response.status_code})"
                )
                archivos_error += 1

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout en {current_date}. Reintentando...")
            time.sleep(5)
            continue

        except Exception as e:
            logger.error(f"Error en {current_date}: {e}")
            archivos_error += 1

        current_date += timedelta(days=DAYS_PER_REQ)
        time.sleep(1)

    tiempo_total = (time.time() - inicio) / 60
    logger.info("=" * 55)
    logger.info("RESUMEN NASA FIRMS")
    logger.info("=" * 55)
    logger.info(f"  Archivos guardados: {archivos_guardados}")
    logger.info(f"  Archivos salteados: {archivos_salteados}")
    logger.info(f"  Archivos con error: {archivos_error}")
    logger.info(f"  Total focos:        {focos_totales:,}")
    logger.info(f"  Tiempo total:       {tiempo_total:.1f} min")

# COMMAND ----------

# MAGIC %md ## Idempotencia

# COMMAND ----------

total_dias    = (DATE_END - DATE_START).days
total_bloques = total_dias // DAYS_PER_REQ + 1
csvs_existentes = [
    f for f in os.listdir(PATH_NASA)
    if f.startswith("nasa_") and f.endswith(".csv")
] if os.path.exists(PATH_NASA) else []

if len(csvs_existentes) >= total_bloques * 0.95:
    print(f"Archivos ya descargados: {len(csvs_existentes)} / {total_bloques} bloques esperados — saliendo.")
    dbutils.notebook.exit("SKIP: nasa ya descargado.")

print(f"NASA: {len(csvs_existentes)} / {total_bloques} bloques existentes — descargando faltantes")

# MAGIC %md ## Ejecutar extracción

# COMMAND ----------

etl_nasa(start_date=DATE_START, end_date=DATE_END, force=FORCE_REDOWNLOAD)

# COMMAND ----------

archivos = sorted([f for f in os.listdir(PATH_NASA) if f.endswith(".csv")])
print(f"Archivos CSV en volumen: {len(archivos)}")

if archivos:
    dfs = []
    for f in archivos:
        try:
            df_tmp = pd.read_csv(os.path.join(PATH_NASA, f))
            if len(df_tmp) > 0:
                dfs.append(df_tmp)
        except Exception:
            pass

    if dfs:
        df_total = pd.concat(dfs, ignore_index=True)
        print(f"Total focos VIIRS: {len(df_total):,}")
        print(f"Columnas: {list(df_total.columns)}")

        if "acq_date" in df_total.columns:
            df_total["year"] = pd.to_datetime(df_total["acq_date"]).dt.year
            print(f"\nFocos por año:")
            print(df_total.groupby("year").size().to_string())

        print(f"\nRango coordenadas:")
        print(f"  Lat: {df_total['latitude'].min():.2f} → {df_total['latitude'].max():.2f}")
        print(f"  Lon: {df_total['longitude'].min():.2f} → {df_total['longitude'].max():.2f}")
    else:
        print("No hay focos de incendio en el período.")
