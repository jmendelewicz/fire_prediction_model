# Databricks notebook source
# MAGIC %md # Open-Meteo Client

# COMMAND ----------

import time
import requests
import pandas as pd
import numpy as np

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

            if response.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"  RATE LIMIT 429 WAIT {wait} SECONDS "
                      f"ATTEMPT {attempt+1} OF {max_retries}")
                time.sleep(wait)
                continue

            response.raise_for_status()
            data = response.json()

            return data if isinstance(data, list) else [data]

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else 0
            if status == 429:
                wait = 60 * (attempt + 1)
                print(f"  RATE LIMIT HTTP ERROR WAIT {wait} SECONDS "
                      f"ATTEMPT {attempt+1} OF {max_retries}")
                time.sleep(wait)
            else:
                raise e
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"  REQUEST FAILED {str(e)[:80]} RETRY {attempt+1} OF {max_retries}")
                time.sleep(10)
            else:
                raise e

    raise Exception(f"BATCH FAILED AFTER {max_retries} ATTEMPTS")

def parse_location_response(loc_data: dict, cell_id: str) -> pd.DataFrame:
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
    total_batches = (len(df_grid) - 1) // batch_size + 1
    results       = []

    for i in range(0, len(df_grid), batch_size):
        batch     = df_grid.iloc[i : i + batch_size]
        batch_num = i // batch_size + 1
        print(f"BATCH {batch_num} OF {total_batches} CELLS {len(batch)}")

        t0         = time.time()
        batch_data = fetch_openmeteo_batch(
            lats          = batch.latitude.tolist(),
            lons          = batch.longitude.tolist(),
            forecast_days = forecast_days,
            past_days     = past_days,
        )

        batch_dfs = [
            parse_location_response(loc, batch.iloc[j]["cell_id"])
            for j, loc in enumerate(batch_data)
        ]
        results.append(pd.concat(batch_dfs, ignore_index=True))

        elapsed = time.time() - t0
        print(f"  BATCH DONE ROWS {sum(len(d) for d in batch_dfs):,} SECONDS {elapsed:.1f}")

        if batch_num < total_batches:
            time.sleep(sleep_between)

    return pd.concat(results, ignore_index=True)

# COMMAND ----------

print("OPEN METEO CLIENT LOADED")
print("  BATCH FETCH WITH RETRY")
print("  LOCATION RESPONSE PARSER")
print("  FULL GRID EXTRACTION LOOP")
