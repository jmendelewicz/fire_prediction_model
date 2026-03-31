# Databricks notebook source
# MAGIC %md
# MAGIC # weather_cleaners
# MAGIC
# MAGIC Módulo de transformaciones matemáticas puras sobre datos climáticos.
# MAGIC No depende de APIs ni de Spark — solo pandas y numpy.
# MAGIC
# MAGIC **Funciones:**
# MAGIC - `calc_relative_humidity` — fórmula Magnus (Alduchov & Eskridge, 1996)
# MAGIC - `calc_vpd` — déficit de presión de vapor en kPa
# MAGIC - `calcular_variables_derivadas_era5` — conversiones ERA5 (Kelvin, u/v, unidades)
# MAGIC - `aggregate_hourly_to_daily` — snapshot 15UTC + suma diaria + media diaria
# MAGIC - `clip_climate_df` — clips de valores físicamente imposibles

# COMMAND ----------

import pandas as pd
import numpy as np

# COMMAND ----------

def calc_relative_humidity(temp_c: np.ndarray, dewpoint_c: np.ndarray) -> np.ndarray:
    """
    Calcula la humedad relativa via fórmula Magnus.
    Idéntica en ERA5 y OpenMeteo — constantes Alduchov & Eskridge (1996).

    Args:
        temp_c:     Temperatura en °C
        dewpoint_c: Punto de rocío en °C
    Returns:
        Humedad relativa en % — clipeada a [0, 100]
    """
    rh = 100 * (
        np.exp((17.625 * dewpoint_c) / (243.04 + dewpoint_c)) /
        np.exp((17.625 * temp_c)     / (243.04 + temp_c))
    )
    return np.clip(rh, 0, 100)


def calc_vpd(temp_c: np.ndarray, rh: np.ndarray) -> np.ndarray:
    """
    Calcula el Déficit de Presión de Vapor (VPD) en kPa.
    Idéntica en ERA5 y OpenMeteo.

    Args:
        temp_c: Temperatura en °C
        rh:     Humedad relativa en %
    Returns:
        VPD en kPa — clipeado a [0, ∞)
    """
    es = 0.611 * np.exp(17.27 * temp_c / (temp_c + 237.3))
    ea = es * (rh / 100.0)
    return np.clip(es - ea, 0, None)


def calcular_variables_derivadas_era5(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aplica todas las conversiones necesarias a los datos crudos de ERA5-Land.

    Conversiones:
        - Temperatura / Punto de rocío: Kelvin → Celsius
        - Humedad relativa: fórmula Magnus desde temp y dewpoint
        - Viento: componentes u/v → velocidad escalar (m/s) y dirección (°)
        - VPD: presión de vapor de saturación vs actual (kPa)
        - Precipitación: m → mm (× 1000)
        - Radiación solar: J/m² → MJ/m² (÷ 1_000_000)

    Args:
        df: DataFrame con columnas ERA5 crudas (en unidades originales SI)
    Returns:
        DataFrame con variables derivadas — elimina wind_u/v y dewpoint_2m
    """
    # Kelvin → Celsius
    df["temperature_2m"] = df["temperature_2m"] - 273.15
    df["dewpoint_2m"]    = df["dewpoint_2m"]     - 273.15

    # Humedad relativa
    df["relative_humidity"] = calc_relative_humidity(
        df["temperature_2m"].values,
        df["dewpoint_2m"].values
    )

    # Velocidad y dirección del viento
    df["wind_speed_10m"]     = np.sqrt(df["wind_u_10m"]**2 + df["wind_v_10m"]**2)
    df["wind_direction_10m"] = (
        270 - np.degrees(np.arctan2(df["wind_v_10m"], df["wind_u_10m"]))
    ) % 360

    # VPD
    df["vpd_kpa"] = calc_vpd(df["temperature_2m"].values, df["relative_humidity"].values)

    # Conversiones de unidades
    df["precipitation"]   = df["precipitation"]   * 1000         # m → mm
    df["solar_radiation"] = df["solar_radiation"]  / 1_000_000   # J/m² → MJ/m²

    return df.drop(columns=["wind_u_10m", "wind_v_10m", "dewpoint_2m"])


def aggregate_hourly_to_daily(df_hourly: pd.DataFrame, noon_utc: int = 15) -> pd.DataFrame:
    """
    Agrega datos horarios de Open-Meteo a resolución diaria.
    Replica exactamente la lógica de ERA5-Land (Van Wagner, 1987):

        - Snapshot mediodía (noon_utc): temp, dewpoint → HR, viento, VPD
        - Suma diaria: precipitación, radiación solar (W/m² → MJ/m²)
        - Media diaria: soil moisture (0-7cm, 28-100cm)

    Args:
        df_hourly: DataFrame con columnas horarias de Open-Meteo
                   Debe tener columnas: datetime, temperature_2m, dew_point_2m,
                   wind_speed_10m, wind_direction_10m, precipitation,
                   shortwave_radiation, soil_moisture_0_7, soil_moisture_28_100, cell_id
        noon_utc:  Hora UTC del snapshot de mediodía (default 15 = 12:00 Argentina)
    Returns:
        DataFrame diario con una fila por (cell_id, date)
    """
    df_hourly = df_hourly.copy()
    df_hourly["date"] = df_hourly["datetime"].dt.strftime("%Y-%m-%d")
    df_hourly["hour"] = df_hourly["datetime"].dt.hour

    # Snapshot mediodía
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

    # Suma diaria: precipitación y radiación (W/m²×3600÷1e6 → MJ/m²)
    df_hourly["solar_radiation_mj"] = df_hourly["shortwave_radiation"] * 3600 / 1_000_000
    df_sum = (
        df_hourly.groupby(["cell_id", "date"], as_index=False)
        .agg(
            precipitation   = ("precipitation",      "sum"),
            solar_radiation = ("solar_radiation_mj", "sum"),
        )
    )

    # Media diaria: soil moisture
    df_mean = (
        df_hourly.groupby(["cell_id", "date"], as_index=False)
        .agg(
            soil_moisture_0_7cm    = ("soil_moisture_0_7",    "mean"),
            soil_moisture_28_100cm = ("soil_moisture_28_100", "mean"),
        )
    )

    # Join de las tres agregaciones
    df_day = df_noon.merge(df_sum,  on=["cell_id", "date"], how="left")
    df_day = df_day.merge(df_mean, on=["cell_id", "date"], how="left")

    return df_day


def clip_climate_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aplica clips a valores climáticamente imposibles.
    Replica los clips de la capa Silver de ERA5.

    Args:
        df: DataFrame con columnas climáticas
    Returns:
        DataFrame con valores clipeados in-place
    """
    if "precipitation" in df.columns:
        df["precipitation"] = df["precipitation"].clip(lower=0)
    if "relative_humidity" in df.columns:
        df["relative_humidity"] = df["relative_humidity"].clip(0, 100)
    if "vpd_kpa" in df.columns:
        df["vpd_kpa"] = df["vpd_kpa"].clip(lower=0)
    return df

# COMMAND ----------

print("weather_cleaners cargado:")
print("  - calc_relative_humidity(temp_c, dewpoint_c)")
print("  - calc_vpd(temp_c, rh)")
print("  - calcular_variables_derivadas_era5(df)")
print("  - aggregate_hourly_to_daily(df_hourly, noon_utc=15)")
print("  - clip_climate_df(df)")
