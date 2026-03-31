# Databricks notebook source
# MAGIC %md
# MAGIC # gee_helpers
# MAGIC
# MAGIC Utilidades para interactuar con Google Earth Engine (GEE).
# MAGIC No contiene lógica meteorológica ni de transformación de datos.
# MAGIC
# MAGIC **Funciones:**
# MAGIC - `inicializar_gee`                    — autenticación e inicialización
# MAGIC - `build_fc_puntos`                    — FeatureCollection de puntos (NDVI, ERA5)
# MAGIC - `build_fc_poligonos`                 — FeatureCollection de polígonos (WorldPop)
# MAGIC - `descargar_feature_collection`       — descarga GEE → pandas en batches
# MAGIC - `guardar_en_volume`                  — guarda DataFrame como CSV en Volume
# MAGIC - `siguiente_mes`                      — utilidad de fecha para loops mensuales

# COMMAND ----------

import ee
import os
import time
import pandas as pd

# COMMAND ----------

def inicializar_gee(project: str) -> None:
    """
    Autentica e inicializa Google Earth Engine.

    Args:
        project: ID del proyecto GEE (ej. 'my-gee-project')
    """
    ee.Authenticate()
    ee.Initialize(project=project)
    print(f"GEE inicializado — proyecto: {project}")


def build_fc_puntos(df_grid: pd.DataFrame) -> ee.FeatureCollection:
    """
    Construye una FeatureCollection de puntos GEE desde la grilla del proyecto.
    Usada para extracción de NDVI, Land Cover y ERA5.

    Args:
        df_grid: DataFrame con columnas: cell_id, latitude, longitude
    Returns:
        FeatureCollection GEE con un punto por nodo
    """
    return ee.FeatureCollection([
        ee.Feature(
            ee.Geometry.Point([float(row.longitude), float(row.latitude)]),
            {"cell_id": str(row.cell_id)}
        )
        for row in df_grid.itertuples()
    ])


def build_fc_poligonos(df_grid: pd.DataFrame, delta: float = 0.125) -> ee.FeatureCollection:
    """
    Construye una FeatureCollection de polígonos GEE desde la grilla.
    Usada para extracción de WorldPop (suma píxeles dentro de cada celda).

    Args:
        df_grid: DataFrame con columnas: cell_id, latitude, longitude
        delta:   Mitad del tamaño de celda (default 0.125 para grilla 0.25°)
    Returns:
        FeatureCollection GEE con un polígono por nodo
    """
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
    """
    Descarga una FeatureCollection GEE a pandas en batches.
    Maneja automáticamente colecciones grandes sin timeout.

    Args:
        fc:    FeatureCollection GEE a descargar
        cols:  Lista de columnas (properties) a extraer
        batch: Tamaño del batch por request GEE
        sleep: Pausa entre batches en segundos
    Returns:
        DataFrame con las columnas especificadas
    """
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
        print(f"  Descargados: {offset:,} / {n_total:,}")
        time.sleep(sleep)
        if len(batch_info) < batch:
            break

    return pd.DataFrame(features)


def guardar_en_volume(df: pd.DataFrame, volume_path: str, filename: str) -> str:
    """
    Guarda un DataFrame como CSV en un Volumen de Unity Catalog.

    Args:
        df:          DataFrame a guardar
        volume_path: Path del volumen (ej. '/Volumes/catalog/schema/vol')
        filename:    Nombre del archivo CSV
    Returns:
        Path completo del archivo guardado
    """
    dest = f"{volume_path}/{filename}"
    df.to_csv(dest, index=False)
    print(f"Guardado: {dest}  ({len(df):,} filas)")
    return dest


def siguiente_mes(year: int, month: int) -> tuple:
    """
    Retorna el año y mes siguiente.
    Utilidad para loops de extracción mensual.

    Args:
        year:  Año actual
        month: Mes actual (1-12)
    Returns:
        Tupla (year, month) del mes siguiente
    """
    return (year + 1, 1) if month == 12 else (year, month + 1)

# COMMAND ----------

print("gee_helpers cargado:")
print("  - inicializar_gee(project)")
print("  - build_fc_puntos(df_grid)")
print("  - build_fc_poligonos(df_grid, delta=0.125)")
print("  - descargar_feature_collection(fc, cols, batch=2000)")
print("  - guardar_en_volume(df, volume_path, filename)")
print("  - siguiente_mes(year, month)")
