-- =============================================================================
-- ddl_gold.sql
-- Tablas Gold —> datos refinados listos para ML y para el frontend.
-- Ejecutar después de: ddl_silver.sql.
-- =============================================================================

USE CATALOG fire_risk_project;

-- =============================================================================
-- GOLD TRAINING DATASET: One Big Table para entrenamiento XGBoost
-- 38 features + target. Una fila por (cell_id, fecha_join).
-- Generada por build_gold.py (parte 1: joins + FWI secuencial) + save_gold.py
-- (parte 2: rolling + spatial neighbors + export).
--
-- AUDIT fix M-8/AN-4 (2026-05-16): la versión previa declaraba 33 columnas;
-- save_gold.py escribe 38 con overwriteSchema=true, generando schema drift
-- silencioso. Las 3 columnas espaciales (fwi_vecinos_mean/max, fire_vecinos_3d)
-- ahora están explícitas.
--
-- NOTA: las 4 features de interacción (fwi_x_vpd, temp_x_dry, wind_x_fwi,
-- ndvi_anomaly) NO viven en esta tabla — se computan in-memory por
-- train_model_v4.add_features() durante el training. Se materializan en
-- forecast_gold_temp para serving.
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`03_gold`.training_dataset_v2 (
    -- Claves
    cell_id STRING,
    fecha_join DATE,
    -- Target
    fire_occurred INT,      -- 0/1
    -- Estáticas (desde aux_grid_pampa)
    subregion_id INT,
    elevation DOUBLE,
    slope DOUBLE,
    aspect DOUBLE,
    dist_road_km DOUBLE,
    pop_density_km2 DOUBLE,
    -- Land cover (anual desde land_cover_silver)
    land_cover_cat INT,
    -- Estacionalidad circular
    mes_sin DOUBLE,
    mes_cos DOUBLE,
    dia_sin DOUBLE,
    dia_cos DOUBLE,
    calendario_agricola INT,      -- flag cultivo × meses cosecha/quema
    -- Variables climáticas mediodía
    temperature_2m DOUBLE,
    relative_humidity DOUBLE,
    wind_speed_10m DOUBLE,
    precipitation DOUBLE,
    solar_radiation DOUBLE,
    soil_moisture_0_7cm DOUBLE,
    soil_moisture_28_100cm DOUBLE,
    -- Derivadas climáticas
    ndvi DOUBLE,
    vpd_kpa DOUBLE,
    -- Sistema FWI canadiense (Van Wagner 1987, tabla Le ajustada a 35°S)
    ffmc DOUBLE,   -- Fine Fuel Moisture Code
    dmc DOUBLE,   -- Duff Moisture Code
    bui DOUBLE,   -- Buildup Index
    isi DOUBLE,   -- Initial Spread Index
    fwi DOUBLE,   -- Fire Weather Index
    -- Features de ventana temporal
    dias_secos INT,      -- días consecutivos sin lluvia
    spi_90d DOUBLE,   -- índice precipitación estandarizado 90d (ver M-2)
    fwi_roll14 DOUBLE,   -- rolling mean FWI 14 días
    fwi_roll30 DOUBLE,   -- rolling mean FWI 30 días
    temperature_2m_roll30 DOUBLE,
    wind_speed_10m_roll30 DOUBLE,
    -- Features espaciales (queen contiguity ±0.25°) — agregadas en save_gold.py
    fwi_vecinos_mean DOUBLE,   -- media FWI de vecinos en el mismo día
    fwi_vecinos_max DOUBLE,    -- máx FWI de vecinos en el mismo día
    fire_vecinos_3d INT        -- 1 si algún vecino tuvo fuego en últimos 3 días (estricto pasado)
)
USING DELTA
COMMENT 'OBT Gold v2 con 38 features (35 base + 3 espaciales) para entrenamiento XGBoost v4'
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
);

-- =============================================================================
-- GOLD FORECAST TEMP: tabla temporal del pipeline de inferencia diaria
-- Lee de silver_openmeteo (solo is_forecast=True), agrega features de ventana
-- temporal (spi_90d, rolling means), features espaciales (queen contiguity),
-- interacciones e ndvi_anomaly. Se sobreescribe diariamente.
--
-- AUDIT fix C-3/C-4/C-5/AN-5 (2026-05-16): la versión previa declaraba 36
-- columnas con `ndvi_deficit`, pero el modelo v4 entrenado usa `ndvi_anomaly`
-- y 3 features espaciales que NO estaban en el DDL ni en el build script.
-- Esta versión refleja exactamente las 42 features que ve XGBoost v4.
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`03_gold`.forecast_gold_temp (
    cell_id STRING,
    date STRING,
    -- Estáticas
    subregion_id DOUBLE,
    elevation DOUBLE,
    slope DOUBLE,
    aspect DOUBLE,
    dist_road_km DOUBLE,
    land_cover_cat INT,
    pop_density_km2 DOUBLE,
    -- Estacionalidad
    mes_sin DOUBLE,
    mes_cos DOUBLE,
    dia_sin DOUBLE,
    dia_cos DOUBLE,
    calendario_agricola INT,
    -- Climáticas
    temperature_2m DOUBLE,
    relative_humidity DOUBLE,
    wind_speed_10m DOUBLE,
    precipitation DOUBLE,
    solar_radiation DOUBLE,
    soil_moisture_0_7cm DOUBLE,
    soil_moisture_28_100cm DOUBLE,
    ndvi DOUBLE,
    vpd_kpa DOUBLE,
    -- FWI
    ffmc DOUBLE,
    dmc DOUBLE,
    bui DOUBLE,
    isi DOUBLE,
    fwi DOUBLE,
    -- Ventanas temporales
    dias_secos INT,
    spi_90d DOUBLE,
    fwi_roll14 DOUBLE,
    fwi_roll30 DOUBLE,
    temperature_2m_roll30 DOUBLE,
    wind_speed_10m_roll30 DOUBLE,
    -- Espaciales (queen contiguity ±0.25°) — alineado a training v4
    fwi_vecinos_mean DOUBLE,
    fwi_vecinos_max DOUBLE,
    fire_vecinos_3d INT,
    -- Interacciones — alineado a train_model_v4.add_features
    fwi_x_vpd DOUBLE,
    temp_x_dry DOUBLE,
    wind_x_fwi DOUBLE,
    -- NDVI anomaly — alineado a train_model_v4 con medias persistidas
    ndvi_anomaly DOUBLE
)
USING DELTA
COMMENT 'Gold temporal con 42 features listas para inferencia XGBoost v4 (train↔serve aligned)'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');

-- =============================================================================
-- VERIFICACIÓN
-- =============================================================================
SHOW TABLES IN fire_risk_project.`03_gold`;