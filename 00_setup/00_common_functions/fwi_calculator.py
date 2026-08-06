# Databricks notebook source
# MAGIC %md # FWI Calculator

# COMMAND ----------

import numpy as np
import pandas as pd

# COMMAND ----------

def calcular_ffmc(temp, rh, wind, rain, ffmc_prev):
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
    Le = [13.9, 12.4, 10.9, 9.4, 8.0, 7.0, 6.0, 6.5, 7.5, 9.0, 12.8, 13.9]
    if temp < -1.1:
        return max(dmc_prev, 0.001)
    k = 1.894 * (temp + 1.1) * (100 - rh) * Le[mes - 1] * 1e-6
    return max(0.001, dmc_prev + 100 * k)

def calcular_dc(temp, rain, dc_prev, mes):
    if rain > 2.8:
        rd      = 0.83 * rain - 1.27
        qo      = 800 * np.exp(-dc_prev / 400)
        qr      = max(qo + 3.937 * rd, 0.001)
        dr      = 400 * np.log(800 / qr)
        dc_prev = max(dr, 0)
    LF = [6.4, 5.0, 2.4, 0.4, -1.6, -1.6, -1.6, -1.6, -1.1, 0.9, 3.8, 5.8]
    if temp < -2.8:
        return max(dc_prev, 0.001)
    v = max(0.36 * (temp + 2.8) + LF[mes - 1], 0)
    return max(0.001, dc_prev + 0.5 * v)

def calcular_isi(wind, ffmc):
    fm = 147.2 * (101 - ffmc) / (59.5 + ffmc)
    ff = 19.115 * np.exp(-0.1386 * fm) * (1 + fm**5.31 / 4.93e7)
    return 0.208 * ff * np.exp(0.05039 * wind)

def calcular_bui(dmc, dc):
    denom = dmc + 0.4 * dc
    if denom == 0:
        return 0.0
    if dmc <= 0.4 * dc:
        bui = 0.8 * dmc * dc / denom
    else:
        bui = dmc - (1 - 0.8 * dc / denom) * (0.92 + (0.0114 * dmc)**1.7)
    return max(0.0, bui)

def calcular_fwi(isi, bui):
    fd = 0.626 * bui**0.809 + 2 if bui <= 80 else 1000 / (25 + 108.64 * np.exp(-0.023 * bui))
    b  = 0.1 * isi * fd
    return np.exp(2.72 * (0.434 * np.log(b))**0.647) if b > 1 else b

def calcular_fwi_serie(
    df_nodo: pd.DataFrame,
    ffmc_init: float = 85.0,
    dmc_init: float = 6.0,
    dc_init: float = 15.0,
) -> pd.DataFrame:
    n         = len(df_nodo)
    ffmc_arr  = np.full(n, np.nan)
    dmc_arr   = np.full(n, np.nan)
    isi_arr   = np.full(n, np.nan)
    bui_arr   = np.full(n, np.nan)
    fwi_arr   = np.full(n, np.nan)
    ffmc_prev = ffmc_init
    dmc_prev  = dmc_init
    dc_prev   = dc_init

    for i, row in enumerate(df_nodo.itertuples()):
        wind_kmh = row.wind_speed_10m * 3.6
        mes      = row.mes if hasattr(row, "mes") else pd.Timestamp(str(row.date)).month
        ffmc_i   = calcular_ffmc(row.temperature_2m, row.relative_humidity,
                                 wind_kmh, row.precipitation, ffmc_prev)
        dmc_i    = calcular_dmc(row.temperature_2m, row.relative_humidity,
                                row.precipitation, dmc_prev, mes)
        dc_i     = calcular_dc(row.temperature_2m, row.precipitation, dc_prev, mes)
        isi_i    = calcular_isi(wind_kmh, ffmc_i)
        bui_i    = calcular_bui(dmc_i, dc_i)
        fwi_i    = calcular_fwi(isi_i, bui_i)

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

print("FWI CALCULATOR LOADED")
print("  FFMC FINE FUEL MOISTURE CODE")
print("  DMC DUFF MOISTURE CODE")
print("  DC DROUGHT CODE")
print("  ISI INITIAL SPREAD INDEX")
print("  BUI BUILDUP INDEX")
print("  FWI FIRE WEATHER INDEX")
print("  FWI SERIES PER GRID CELL")
