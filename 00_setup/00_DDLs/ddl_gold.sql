-- =============================================================================
-- ddl_gold.sql
-- Tablas Gold —> datos refinados listos para ML y para el frontend.
-- Ejecutar después de: ddl_silver.sql.
-- =============================================================================

USE CATALOG fire_risk_project;

-- =============================================================================
-- GOLD TRAINING DATASET: One Big Table para entrenamiento XGBoost
-- 35 features + target. Una fila por (cell_id, fecha_join).
-- Generada por build_gold.py (parte 1, checkpoint FWI) + save_gold.py (parte 2, rolling).
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
    -- Sistema FWI canadiense (Van Wagner 1987)
    ffmc DOUBLE,   -- Fine Fuel Moisture Code
    dmc DOUBLE,   -- Duff Moisture Code
    bui DOUBLE,   -- Buildup Index
    isi DOUBLE,   -- Initial Spread Index
    fwi DOUBLE,   -- Fire Weather Index
    -- Features de ventana temporal
    dias_secos INT,      -- días consecutivos sin lluvia
    spi_90d DOUBLE,   -- índice precipitación estandarizado 90d
    fwi_roll14 DOUBLE,   -- rolling mean FWI 14 días
    fwi_roll30 DOUBLE,   -- rolling mean FWI 30 días
    temperature_2m_roll30 DOUBLE,
    wind_speed_10m_roll30 DOUBLE
)
USING DELTA
COMMENT 'OBT Gold v2 con 35 features para entrenamiento XGBoost'
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
);

-- =============================================================================
-- GOLD FORECAST TEMP: tabla temporal del pipeline de inferencia diaria
-- Lee de silver_openmeteo (solo is_forecast=True), agrega features de
-- ventana temporal (spi_90d, rolling means) e interacciones del modelo.
-- Se crea en etl_build_gold_forecast, se elimina en cloud_inference_engine.
-- 36 features = 35 del training + interacciones de add_features().
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
    -- Interacciones (add_features de train_model_v2.py)
    fwi_x_vpd DOUBLE,
    temp_x_dry DOUBLE,
    wind_x_fwi DOUBLE,
    ndvi_deficit DOUBLE
)
USING DELTA
COMMENT 'Gold temporal con 36 features listas para inferencia XGBoost'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');

-- =============================================================================
-- VERIFICACIÓN
-- =============================================================================
SHOW TABLES IN fire_risk_project.`03_gold`;