# Databricks notebook source
# MAGIC %md
# MAGIC # Auditoría - Tablas Silver
# MAGIC
# MAGIC Verifica integridad, cobertura y calidad de todas las tablas Silver
# MAGIC antes de continuar con Gold.
# MAGIC
# MAGIC **Tablas auditadas:**
# MAGIC - `silver_nasa_firms`  — focos VIIRS
# MAGIC - `silver_era5`        — variables climáticas + features estáticas (dist_road, pop_density, topografía)
# MAGIC - `silver_ndvi`        — índice de vegetación diario
# MAGIC - `silver_land_cover`  — cobertura del suelo anual
# MAGIC - `silver_openmeteo`   — seed 35d + forecast 4d con FWI calculado (opcional, sólo si pipeline diario)
# MAGIC
# MAGIC **Fix A-7 (2026-05-16):** la versión previa auditaba
# MAGIC `static_features_silver`, tabla eliminada cuando `dist_road_km` y
# MAGIC `pop_density_km2` se propagaron a `aux_grid_pampa` y de ahí a
# MAGIC `silver_era5`. Los checks de features estáticas ahora se hacen
# MAGIC contra `silver_era5`. Se agregó auditoría de `silver_openmeteo`
# MAGIC para el pipeline diario de inferencia.

# COMMAND ----------

from pyspark.sql import functions as F

CATALOG    = "fire_risk_project"
TABLE_GRID = f"{CATALOG}.`00_landing`.aux_grid_pampa"

TABLE_NASA = f"{CATALOG}.`02_silver`.silver_nasa_firms"
TABLE_ERA5 = f"{CATALOG}.`02_silver`.silver_era5"
TABLE_NDVI = f"{CATALOG}.`02_silver`.silver_ndvi"
TABLE_LC   = f"{CATALOG}.`02_silver`.silver_land_cover"
TABLE_OM   = f"{CATALOG}.`02_silver`.silver_openmeteo"

N_NODOS    = 2266
N_DIAS     = 365 + 365 + 366
FECHA_MIN  = "2022-01-01"
FECHA_MAX  = "2024-12-31"

def header(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)

def has_table(fqname):
    try:
        spark.table(fqname).count()
        return True
    except Exception:
        return False

# COMMAND ----------

# MAGIC %md ## NASA FIRMS Silver

# COMMAND ----------

header("NASA FIRMS SILVER")
df = spark.table(TABLE_NASA)

print(f"Focos totales: {df.count():,}")
df.groupBy(F.year("acq_date").alias("anio")).count().orderBy("anio").show()
df.groupBy("confidence").count().show()

n_sin_nodo = df.join(spark.table(TABLE_GRID), on="cell_id", how="left_anti").count()
print(f"Focos sin nodo válido: {n_sin_nodo}  {'OK' if n_sin_nodo == 0 else 'Revisar'}")

# COMMAND ----------

# MAGIC %md ## ERA5 Silver — features climáticas + estáticas (dist_road, pop_density, topografía)

# COMMAND ----------

header("ERA5 SILVER")
df = spark.table(TABLE_ERA5)
total = df.count()
ESPERADO = N_NODOS * N_DIAS

print(f"Registros: {total:,}  (esperado: {ESPERADO:,}, diff: {total - ESPERADO:+,})")
print(f"Nodos únicos: {df.select('cell_id').distinct().count():,} / {N_NODOS}")

fechas = df.agg(F.min("fecha_join").alias("desde"), F.max("fecha_join").alias("hasta")).collect()[0]
print(f"Fechas: {fechas['desde']} → {fechas['hasta']}")

feature_cols_clima = [
    "temperature_2m", "relative_humidity", "precipitation",
    "wind_speed_10m", "vpd_kpa", "solar_radiation",
    "soil_moisture_0_7cm", "soil_moisture_28_100cm"
]
feature_cols_estaticas = [
    "elevation", "slope", "aspect",
    "dist_road_km", "pop_density_km2", "subregion_id"
]

print("\nNulos en features climáticas (mediodía / agregados diarios):")
null_exprs = [F.count(F.when(F.col(c).isNull(), c)).alias(c) for c in feature_cols_clima]
nulls = df.select(null_exprs).collect()[0]
for c in feature_cols_clima:
    pct = nulls[c] / total * 100
    print(f"  {c:<30} {nulls[c]:6,}  ({pct:.2f}%)  {'OK' if pct == 0 else 'Revisar'}")

print("\nNulos en features estáticas (propagadas desde aux_grid_pampa):")
null_exprs_est = [F.count(F.when(F.col(c).isNull(), c)).alias(c) for c in feature_cols_estaticas]
nulls_est = df.select(null_exprs_est).collect()[0]
for c in feature_cols_estaticas:
    pct = nulls_est[c] / total * 100
    print(f"  {c:<30} {nulls_est[c]:6,}  ({pct:.2f}%)  {'OK' if pct == 0 else 'Revisar'}")

print("\nRangos físicos (verificación contra clips de Silver):")
ranges = df.agg(
    F.min("relative_humidity").alias("rh_min"), F.max("relative_humidity").alias("rh_max"),
    F.min("precipitation").alias("p_min"),     F.max("precipitation").alias("p_max"),
    F.min("vpd_kpa").alias("vpd_min"),         F.max("vpd_kpa").alias("vpd_max"),
).collect()[0]
print(f"  RH:   [{ranges['rh_min']:.2f}, {ranges['rh_max']:.2f}]  esperado [0, 100]")
print(f"  Prec: [{ranges['p_min']:.2f}, {ranges['p_max']:.2f}]    esperado [0, ∞)")
print(f"  VPD:  [{ranges['vpd_min']:.2f}, {ranges['vpd_max']:.2f}]  esperado [0, ∞)")

# COMMAND ----------

# MAGIC %md ## NDVI Silver

# COMMAND ----------

header("NDVI SILVER")
df = spark.table(TABLE_NDVI)
total = df.count()

print(f"Filas: {total:,}  (esperado: ~{N_NODOS * N_DIAS:,})")
print(f"Nodos únicos: {df.select('cell_id').distinct().count():,}")
print(f"Fechas únicas: {df.select('fecha').distinct().count():,}  (esperado: {N_DIAS})")

n_null = df.filter(F.col("ndvi").isNull()).count()
n_oor  = df.filter((F.col("ndvi") < -1) | (F.col("ndvi") > 1)).count()
print(f"\nNulos NDVI:          {n_null:,}  {'OK' if n_null == 0 else 'Revisar'}")
print(f"NDVI fuera de rango: {n_oor:,}   {'OK' if n_oor  == 0 else 'Revisar'}")

df.groupBy(F.year("fecha").alias("anio")) \
    .agg(F.count("*").alias("filas"), F.round(F.mean("ndvi"), 4).alias("ndvi_medio")) \
    .orderBy("anio").show()

# COMMAND ----------

# MAGIC %md ## Land Cover Silver

# COMMAND ----------

header("LAND COVER SILVER")
df = spark.table(TABLE_LC)

print(f"Filas: {df.count():,}  (esperado: ~{N_NODOS * 3} → 1 por nodo × 3 años)")
print(f"Nodos únicos: {df.select('cell_id').distinct().count():,}")

df.groupBy("year", "land_cover_cat") \
    .count() \
    .withColumn("descripcion",
        F.when(F.col("land_cover_cat") == 0, "Urbano/Otro")
         .when(F.col("land_cover_cat") == 1, "Cultivo")
         .when(F.col("land_cover_cat") == 2, "Veg. Natural")) \
    .orderBy("year", "land_cover_cat").show()

n_null = df.filter(F.col("land_cover_cat").isNull()).count()
n_inc  = df.filter((F.col("land_cover_cat") < 0) | (F.col("land_cover_cat") > 2)).count()
print(f"Valores fuera de [0,2]:  {n_inc}   {'OK' if n_inc  == 0 else 'Revisar'}")
print(f"Nulos land_cover_cat:    {n_null}  {'OK' if n_null == 0 else 'Revisar'}")

# COMMAND ----------

# MAGIC %md ## Silver Open-Meteo (opcional — solo si se corrió transform_openmeteo)
# MAGIC
# MAGIC Esta tabla la genera `01_etl_pipeline/03_silver/transform_openmeteo.py`
# MAGIC con la ventana de 39 días (35 histórico + 4 forecast) y el FWI calculado.
# MAGIC Si no existe, este check se saltea (no es bloqueante para el training).

# COMMAND ----------

header("SILVER OPENMETEO (opcional)")
if not has_table(TABLE_OM):
    print(f"Tabla {TABLE_OM} no existe.")
    print("Esto es normal si todavía no se corrió el pipeline diario de forecast.")
    print("Para crearla: correr 01_etl_pipeline/03_silver/transform_openmeteo.py")
else:
    df = spark.table(TABLE_OM)
    total = df.count()
    print(f"Filas: {total:,}  (esperado: ~{N_NODOS * 39:,} = 2266 × 39 días)")

    df.groupBy("is_forecast").count().orderBy("is_forecast").show()

    fwi_stats = df.agg(
        F.round(F.mean("fwi"), 2).alias("fwi_medio"),
        F.round(F.max("fwi"),  2).alias("fwi_max"),
        F.count(F.when(F.col("fwi").isNull(), 1)).alias("fwi_nulos")
    ).collect()[0]
    print(f"FWI medio: {fwi_stats['fwi_medio']}  |  FWI max: {fwi_stats['fwi_max']}  |  Nulos: {fwi_stats['fwi_nulos']}")

# COMMAND ----------

# MAGIC %md ## Consistencia entre tablas

# COMMAND ----------

header("CONSISTENCIA ENTRE TABLAS")

cells = {
    "ERA5":  spark.table(TABLE_ERA5).select("cell_id").distinct(),
    "NDVI":  spark.table(TABLE_NDVI).select("cell_id").distinct(),
    "LC":    spark.table(TABLE_LC).select("cell_id").distinct(),
}

ref = cells["ERA5"]
for nombre, df_cells in cells.items():
    n       = df_cells.count()
    missing = ref.join(df_cells, on="cell_id", how="left_anti").count()
    print(f"  {nombre:<10}: {n:,} nodos | faltantes vs ERA5: {missing}  {'OK' if missing == 0 else 'Revisar'}")

# COMMAND ----------

# MAGIC %md ## Resumen ejecutivo

# COMMAND ----------

header("RESUMEN EJECUTIVO")

df_era5 = spark.table(TABLE_ERA5)
df_ndvi = spark.table(TABLE_NDVI)
df_lc   = spark.table(TABLE_LC)
df_nasa = spark.table(TABLE_NASA)

checks = {
    "ERA5 sin nulos climáticos":     df_era5.filter(F.col("temperature_2m").isNull()).count() == 0,
    "ERA5 sin nulos dist_road":      df_era5.filter(F.col("dist_road_km").isNull()).count() == 0,
    "ERA5 sin nulos pop_density":    df_era5.filter(F.col("pop_density_km2").isNull()).count() == 0,
    "ERA5 sin nulos subregion":      df_era5.filter(F.col("subregion_id").isNull()).count() == 0,
    "NDVI sin nulos":                df_ndvi.filter(F.col("ndvi").isNull()).count() == 0,
    "NDVI en rango [-1,1]":          df_ndvi.filter((F.col("ndvi") < -1) | (F.col("ndvi") > 1)).count() == 0,
    "Land Cover sin nulos":          df_lc.filter(F.col("land_cover_cat").isNull()).count() == 0,
    "NASA sin focos huérfanos":      df_nasa.join(spark.table(TABLE_GRID), on="cell_id", how="left_anti").count() == 0,
    "Nodos consistentes ERA5/NDVI":  cells["ERA5"].join(cells["NDVI"], on="cell_id", how="left_anti").count() == 0,
    "Nodos consistentes ERA5/LC":    cells["ERA5"].join(cells["LC"],   on="cell_id", how="left_anti").count() == 0,
}

print(f"{'Check':<40} Estado")
print("-" * 55)
for check, ok in checks.items():
    print(f"  {check:<38} {'OK' if ok else 'Revisar'}")

n_ok = sum(checks.values())
print(f"\nResultado: {n_ok}/{len(checks)} checks pasados")
if n_ok == len(checks):
    print("Todas las tablas Silver listas para Gold.")
else:
    print("Revisar los items marcados antes de continuar con build_gold.py.")
