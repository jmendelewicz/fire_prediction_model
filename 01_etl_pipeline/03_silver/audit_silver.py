# Databricks notebook source
# MAGIC %md
# MAGIC # Auditoría - Tablas Silver
# MAGIC
# MAGIC Verifica integridad, cobertura y calidad de todas las tablas Silver
# MAGIC antes de continuar con Gold.
# MAGIC
# MAGIC **Tablas auditadas:**
# MAGIC - `silver_nasa_firms` — focos VIIRS
# MAGIC - `silver_era5` — variables climáticas
# MAGIC - `ndvi_silver` — índice de vegetación
# MAGIC - `land_cover_silver` — cobertura del suelo
# MAGIC - `static_features_silver` — distancia a rutas y población

# COMMAND ----------

from pyspark.sql import functions as F

CATALOG = "fire_risk_project"
TABLE_GRID   = f"{CATALOG}.00_landing.aux_grid_pampa"

TABLE_NASA   = f"{CATALOG}.02_silver.silver_nasa_firms"
TABLE_ERA5   = f"{CATALOG}.02_silver.silver_era5"
TABLE_NDVI   = f"{CATALOG}.02_silver.ndvi_silver"
TABLE_LC     = f"{CATALOG}.02_silver.land_cover_silver"
TABLE_STATIC = f"{CATALOG}.02_silver.static_features_silver"

N_NODOS    = 2266
N_DIAS     = 365 + 365 + 366   # 2022 + 2023 + 2024 (bisiesto)
FECHA_MIN  = "2022-01-01"
FECHA_MAX  = "2024-12-31"

def header(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)

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

# MAGIC %md ## ERA5 Silver

# COMMAND ----------

header("ERA5 SILVER")
df = spark.table(TABLE_ERA5)
total = df.count()
ESPERADO = N_NODOS * N_DIAS

print(f"Registros: {total:,}  (esperado: {ESPERADO:,}, diff: {total - ESPERADO:+,})")
print(f"Nodos únicos: {df.select('cell_id').distinct().count():,} / {N_NODOS}")

fechas = df.agg(F.min("fecha_join").alias("desde"), F.max("fecha_join").alias("hasta")).collect()[0]
print(f"Fechas: {fechas['desde']} → {fechas['hasta']}")

feature_cols = [
    "temperature_2m", "relative_humidity", "precipitation",
    "wind_speed_10m", "vpd_kpa", "solar_radiation",
    "soil_moisture_0_7cm", "soil_moisture_28_100cm"
]
null_exprs = [F.count(F.when(F.col(c).isNull(), c)).alias(c) for c in feature_cols]
nulls = df.select(null_exprs).collect()[0]
print("\nNulos por variable:")
for c in feature_cols:
    pct = nulls[c] / total * 100
    print(f"  {c:<30} {nulls[c]:6,}  ({pct:.2f}%)  {'OK' if pct == 0 else 'Error'}")

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
print(f"\nNulos NDVI:          {n_null:,}  {'OK' if n_null == 0 else 'Error'}")
print(f"NDVI fuera de rango: {n_oor:,}   {'OK' if n_oor  == 0 else 'Error'}")

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
print(f"Valores fuera de [0,2]:  {n_inc}   {'OK' if n_inc  == 0 else 'Error'}")
print(f"Nulos land_cover_cat:    {n_null}  {'OK' if n_null == 0 else 'Error'}")

# COMMAND ----------

# MAGIC %md ## 5 · Static Features Silver

# COMMAND ----------

header("STATIC FEATURES SILVER")
df = spark.table(TABLE_STATIC)

print(f"Nodos: {df.count():,}  (esperado: {N_NODOS})")
df.describe(["dist_road_km", "pop_density_km2"]).show()

n_null_dist = df.filter(F.col("dist_road_km").isNull()).count()
n_null_pop  = df.filter(F.col("pop_density_km2").isNull()).count()
n_nonzero   = df.filter(F.col("pop_density_km2") > 0).count()
print(f"Nulos dist_road_km:    {n_null_dist}  {'OK' if n_null_dist == 0 else 'Error'}")
print(f"Nulos pop_density_km2: {n_null_pop}   {'OK' if n_null_pop  == 0 else 'Error'}")
print(f"Nodos con pop > 0:     {n_nonzero:,} ({100*n_nonzero/df.count():.1f}%)")

# COMMAND ----------

# MAGIC %md ## Consistencia entre tablas

# COMMAND ----------

header("CONSISTENCIA ENTRE TABLAS")

cells = {
    "ERA5":    spark.table(TABLE_ERA5).select("cell_id").distinct(),
    "NDVI":    spark.table(TABLE_NDVI).select("cell_id").distinct(),
    "LC":      spark.table(TABLE_LC).select("cell_id").distinct(),
    "Static":  spark.table(TABLE_STATIC).select("cell_id").distinct(),
}

ref = cells["ERA5"]
for nombre, df_cells in cells.items():
    n       = df_cells.count()
    missing = ref.join(df_cells, on="cell_id", how="left_anti").count()
    print(f"  {nombre:<10}: {n:,} nodos | faltantes vs ERA5: {missing}  {'Correcto' if missing == 0 else 'Errores'}")

# COMMAND ----------

# MAGIC %md ## Resumen ejecutivo

# COMMAND ----------

header("RESUMEN EJECUTIVO")

df_era5   = spark.table(TABLE_ERA5)
df_ndvi   = spark.table(TABLE_NDVI)
df_lc     = spark.table(TABLE_LC)
df_static = spark.table(TABLE_STATIC)
df_nasa   = spark.table(TABLE_NASA)

checks = {
    "ERA5 sin nulos críticos":      df_era5.filter(F.col("temperature_2m").isNull()).count() == 0,
    "NDVI sin nulos":               df_ndvi.filter(F.col("ndvi").isNull()).count() == 0,
    "NDVI en rango [-1,1]":         df_ndvi.filter((F.col("ndvi") < -1) | (F.col("ndvi") > 1)).count() == 0,
    "Land Cover sin nulos":         df_lc.filter(F.col("land_cover_cat").isNull()).count() == 0,
    "Static sin nulos dist_road":   df_static.filter(F.col("dist_road_km").isNull()).count() == 0,
    "NASA sin focos huérfanos":     df_nasa.join(spark.table(TABLE_GRID), on="cell_id", how="left_anti").count() == 0,
    "Nodos consistentes":           cells["ERA5"].join(cells["NDVI"],   on="cell_id", how="left_anti").count() == 0 and
                                    cells["ERA5"].join(cells["Static"], on="cell_id", how="left_anti").count() == 0,
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
    print("Revisar los items marcados antes de continuar.")
