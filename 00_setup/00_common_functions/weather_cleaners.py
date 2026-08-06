# Databricks notebook source
# MAGIC %md # Weather Cleaners

# COMMAND ----------

import pandas as pd
import numpy as np

# COMMAND ----------

def calc_relative_humidity(temp_c: np.ndarray, dewpoint_c: np.ndarray) -> np.ndarray:
    rh = 100 * (
        np.exp((17.625 * dewpoint_c) / (243.04 + dewpoint_c)) /
        np.exp((17.625 * temp_c)     / (243.04 + temp_c))
    )
    return np.clip(rh, 0, 100)

def calc_vpd(temp_c: np.ndarray, rh: np.ndarray) -> np.ndarray:
    es = 0.611 * np.exp(17.27 * temp_c / (temp_c + 237.3))
    ea = es * (rh / 100.0)
    return np.clip(es - ea, 0, None)

def calcular_variables_derivadas_era5(df: pd.DataFrame) -> pd.DataFrame:
    df["temperature_2m"] = df["temperature_2m"] - 273.15
    df["dewpoint_2m"]    = df["dewpoint_2m"]     - 273.15

    df["relative_humidity"] = calc_relative_humidity(
        df["temperature_2m"].values,
        df["dewpoint_2m"].values
    )

    df["wind_speed_10m"]     = np.sqrt(df["wind_u_10m"]**2 + df["wind_v_10m"]**2)
    df["wind_direction_10m"] = (
        270 - np.degrees(np.arctan2(df["wind_v_10m"], df["wind_u_10m"]))
    ) % 360

    df["vpd_kpa"] = calc_vpd(df["temperature_2m"].values, df["relative_humidity"].values)

    df["precipitation"]   = df["precipitation"]   * 1000
    df["solar_radiation"] = df["solar_radiation"]  / 1_000_000

    return df.drop(columns=["wind_u_10m", "wind_v_10m", "dewpoint_2m"])

def aggregate_hourly_to_daily(df_hourly: pd.DataFrame, noon_utc: int = 15) -> pd.DataFrame:
    df_hourly = df_hourly.copy()
    df_hourly["date"] = df_hourly["datetime"].dt.strftime("%Y-%m-%d")
    df_hourly["hour"] = df_hourly["datetime"].dt.hour

    df_noon = df_hourly[df_hourly["hour"] == noon_utc].copy()
    df_noon["relative_humidity"] = calc_relative_humidity(
        df_noon["temperature_2m"].values,
        df_noon["dew_point_2m"].values
    )
    df_noon["vpd_kpa"] = calc_vpd(
        df_noon["temperature_2m"].values,
        df_noon["relative_humidity"].values
    )
    df_noon = df_noon[[
        "date", "cell_id", "temperature_2m", "relative_humidity",
        "wind_speed_10m", "wind_direction_10m", "vpd_kpa"
    ]]

    df_hourly["solar_radiation_mj"] = df_hourly["shortwave_radiation"] * 3600 / 1_000_000
    df_sum = (
        df_hourly.groupby(["cell_id", "date"], as_index=False)
        .agg(
            precipitation   = ("precipitation",      "sum"),
            solar_radiation = ("solar_radiation_mj", "sum"),
        )
    )

    df_mean = (
        df_hourly.groupby(["cell_id", "date"], as_index=False)
        .agg(
            soil_moisture_0_7cm    = ("soil_moisture_0_7",    "mean"),
            soil_moisture_28_100cm = ("soil_moisture_28_100", "mean"),
        )
    )

    df_day = df_noon.merge(df_sum,  on=["cell_id", "date"], how="left")
    df_day = df_day.merge(df_mean, on=["cell_id", "date"], how="left")

    return df_day

def clip_climate_df(df: pd.DataFrame) -> pd.DataFrame:
    if "precipitation" in df.columns:
        df["precipitation"] = df["precipitation"].clip(lower=0)
    if "relative_humidity" in df.columns:
        df["relative_humidity"] = df["relative_humidity"].clip(0, 100)
    if "vpd_kpa" in df.columns:
        df["vpd_kpa"] = df["vpd_kpa"].clip(lower=0)
    return df

# COMMAND ----------

print("WEATHER CLEANERS LOADED")
print("  RELATIVE HUMIDITY FROM DEW POINT")
print("  VAPOUR PRESSURE DEFICIT")
print("  ERA5 DERIVED VARIABLES")
print("  HOURLY TO DAILY AGGREGATION")
print("  PHYSICAL RANGE CLIPS")
