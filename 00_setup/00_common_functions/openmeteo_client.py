# Databricks notebook source
# MAGIC %md
# MAGIC # openmeteo_client
# MAGIC
# MAGIC Cliente HTTP para la API de Open-Meteo.
# MAGIC Compatible con Databricks Serverless — usa solo `requests` (nativo).
# MAGIC No depende de openmeteo-requests ni de otras librerías externas.
# MAGIC
# MAGIC **Funciones:**
# MAGIC - `fetch_openmeteo_batch`    — request GET con retry 429
# MAGIC - `run_batched_extraction`   — loop completo de batches sobre la grilla

# COMMAND ----------

import time
import requests
import pandas as pd
import numpy as np

# Variables horarias a extraer — estándar del proyecto
HOURLY_VARS = [
    "temperature_2m",
    "dew_point_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "precipitation",
    "shortwave_radiation",
    "soil_moisture_0_to_7cm",
    "soil_moisture_28_to_100cm",
]

OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"

# COMMAND ----------

def fetch_openmeteo_batch(
    lats: list,
    lons: list,
    forecast_days: int = 4,
    past_days: int = 0,
    max_retries: int = 5,
    sleep_between: int = 10,
) -> list:
    """
    Hace un GET request a la API de Open-Meteo para múltiples coordenadas.
    Maneja automáticamente el rate limit (429) con backoff exponencial.

    Args:
        lats:           Lista de latitudes
        lons:           Lista de longitudes
        forecast_days:  Días de pronóstico a extraer
        past_days:      Días históricos a extraer
        max_retries:    Intentos máximos ante rate limit
        sleep_between:  Segundos de pausa base entre batches
    Returns:
        Lista de dicts con datos horarios por ubicación
    Raises:
        Exception: Si falla después de max_retries intentos
    """
    params = {
        "latitude":      lats,
        "longitude":     lons,
        "hourly":        HOURLY_VARS,
        "timezone":      "GMT",
        "forecast_days": forecast_days,
        "past_days":     past_days,
    }

    for attempt in range(max_retries):
        try:
            response = requests.get(OPENMETEO_URL, params=params, timeout=60)

            # Manejar 429 antes de raise_for_status
            if response.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"  Rate limit (429). Esperando {wait}s "
                      f"(intento {attempt+1}/{max_retries})...")
                time.sleep(wait)
                continue

            response.raise_for_status()
            data = response.json()

            # La API devuelve dict para 1 coord, list para múltiples
            return data if isinstance(data, list) else [data]

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else 0
            if status == 429:
                wait = 60 * (attempt + 1)
                print(f"  Rate limit HTTPError. Esperando {wait}s "
                      f"(intento {attempt+1}/{max_retries})...")
                time.sleep(wait)
            else:
                raise e
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"  Error: {str(e)[:80]}. Reintentando ({attempt+1}/{max_retries})...")
                time.sleep(10)
            else:
                raise e

    raise Exception(f"Batch falló después de {max_retries} intentos.")


def parse_location_response(loc_data: dict, cell_id: str) -> pd.DataFrame:
    """
    Convierte la respuesta JSON de una ubicación en un DataFrame horario.

    Args:
        loc_data: Dict con datos horarios de una ubicación (formato Open-Meteo)
        cell_id:  Identificador del nodo de la grilla
    Returns:
        DataFrame horario con todas las variables + cell_id
    """
    hourly = loc_data["hourly"]
    times  = pd.to_datetime(hourly["time"], utc=True)

    df = pd.DataFrame({
        "datetime":             times,
        "temperature_2m":       hourly["temperature_2m"],
        "dew_point_2m":         hourly["dew_point_2m"],
        "wind_speed_10m":       hourly["wind_speed_10m"],
        "wind_direction_10m":   hourly["wind_direction_10m"],
        "precipitation":        hourly["precipitation"],
        "shortwave_radiation":  hourly["shortwave_radiation"],
        "soil_moisture_0_7":    hourly["soil_moisture_0_to_7cm"],
        "soil_moisture_28_100": hourly["soil_moisture_28_to_100cm"],
    })
    df["cell_id"] = cell_id
    return df


def run_batched_extraction(
    df_grid: pd.DataFrame,
    forecast_days: int = 4,
    past_days: int = 0,
    batch_size: int = 100,
    sleep_between: int = 10,
) -> pd.DataFrame:
    """
    Ejecuta la extracción completa de Open-Meteo sobre todos los nodos
    de la grilla en batches, retornando un DataFrame horario consolidado.

    Args:
        df_grid:       DataFrame con columnas: cell_id, latitude, longitude
        forecast_days: Días de pronóstico
        past_days:     Días históricos (para seed)
        batch_size:    Nodos por request (max recomendado: 100)
        sleep_between: Pausa en segundos entre batches
    Returns:
        DataFrame horario con todas las variables para todos los nodos
    """
    total_batches = (len(df_grid) - 1) // batch_size + 1
    results       = []

    for i in range(0, len(df_grid), batch_size):
        batch     = df_grid.iloc[i : i + batch_size]
        batch_num = i // batch_size + 1
        print(f"Batch {batch_num} / {total_batches} ({len(batch)} nodos)...")

        t0         = time.time()
        batch_data = fetch_openmeteo_batch(
            lats          = batch.latitude.tolist(),
            lons          = batch.longitude.tolist(),
            forecast_days = forecast_days,
            past_days     = past_days,
        )

        # Parsear cada ubicación del batch
        batch_dfs = [
            parse_location_response(loc, batch.iloc[j]["cell_id"])
            for j, loc in enumerate(batch_data)
        ]
        results.append(pd.concat(batch_dfs, ignore_index=True))

        elapsed = time.time() - t0
        print(f"  OK: {sum(len(d) for d in batch_dfs):,} filas en {elapsed:.1f}s")

        if batch_num < total_batches:
            time.sleep(sleep_between)

    return pd.concat(results, ignore_index=True)

# COMMAND ----------

print("openmeteo_client cargado:")
print("  - fetch_openmeteo_batch(lats, lons, forecast_days, past_days, ...)")
print("  - parse_location_response(loc_data, cell_id)")
print("  - run_batched_extraction(df_grid, forecast_days, past_days, batch_size, ...)")
