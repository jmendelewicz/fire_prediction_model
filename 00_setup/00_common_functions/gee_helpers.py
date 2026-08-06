# Databricks notebook source
# MAGIC %md # Earth Engine Helpers

# COMMAND ----------

import ee
import os
import time
import pandas as pd

# COMMAND ----------

def inicializar_gee(project: str) -> None:
    ee.Authenticate()
    ee.Initialize(project=project)
    print(f"EARTH ENGINE READY PROJECT {project}")

def build_fc_puntos(df_grid: pd.DataFrame) -> ee.FeatureCollection:
    return ee.FeatureCollection([
        ee.Feature(
            ee.Geometry.Point([float(row.longitude), float(row.latitude)]),
            {"cell_id": str(row.cell_id)}
        )
        for row in df_grid.itertuples()
    ])

def build_fc_poligonos(df_grid: pd.DataFrame, delta: float = 0.125) -> ee.FeatureCollection:
    return ee.FeatureCollection([
        ee.Feature(
            ee.Geometry.Rectangle([
                float(row.longitude) - delta, float(row.latitude) - delta,
                float(row.longitude) + delta, float(row.latitude) + delta,
            ]),
            {"cell_id": str(row.cell_id)}
        )
        for row in df_grid.itertuples()
    ])

def descargar_feature_collection(
    fc: ee.FeatureCollection,
    cols: list,
    batch: int = 2000,
    sleep: float = 0.3,
) -> pd.DataFrame:
    n_total  = fc.size().getInfo()
    features = []
    offset   = 0

    while True:
        batch_info = fc.toList(batch, offset).getInfo()
        if not batch_info:
            break
        for f in batch_info:
            props = f.get("properties", {})
            features.append({c: props.get(c) for c in cols})
        offset += len(batch_info)
        print(f"  DOWNLOADED {offset:,} OF {n_total:,}")
        time.sleep(sleep)
        if len(batch_info) < batch:
            break

    return pd.DataFrame(features)

def guardar_en_volume(df: pd.DataFrame, volume_path: str, filename: str) -> str:
    dest = f"{volume_path}/{filename}"
    df.to_csv(dest, index=False)
    print(f"FILE SAVED {dest} ROWS {len(df):,}")
    return dest

def siguiente_mes(year: int, month: int) -> tuple:
    return (year + 1, 1) if month == 12 else (year, month + 1)

# COMMAND ----------

print("EARTH ENGINE HELPERS LOADED")
print("  GEE INIT")
print("  POINT FEATURE COLLECTION")
print("  POLYGON FEATURE COLLECTION")
print("  BATCH DOWNLOAD TO PANDAS")
print("  SAVE CSV TO VOLUME")
print("  NEXT MONTH HELPER")
