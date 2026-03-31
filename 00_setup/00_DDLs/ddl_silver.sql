-- =============================================================================
-- ddl_silver.sql
-- Tablas Silver de datos limpios, transformados y listos para Gold.
-- Fuente de la verdad del proyecto (SSOT).
-- Ejecutar después de: ddl_bronze.sql.
-- =============================================================================

USE CATALOG fire_risk_project;

-- =============================================================================
-- SILVER ERA5: variables climáticas transformadas para training
-- Se esperan unidades convertidas, clips aplicados, join con grilla.
-- Una fila por (cell_id, date).
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`02_silver`.silver_era5 (
    -- Claves
    cell_id STRING,
    date STRING,
    fecha_join DATE,     -- para joins con NASA FIRMS en Gold
    -- Variables climáticas mediodía — unidades finales
    temperature_2m DOUBLE,   -- °C
    relative_humidity DOUBLE,   -- % [0, 100]
    wind_speed_10m DOUBLE,   -- m/s
    wind_direction_10m DOUBLE,   -- grados [0, 360]
    vpd_kpa DOUBLE,   -- kPa ≥ 0
    -- Acumulados diarios
    precipitation DOUBLE,   -- mm ≥ 0
    solar_radiation DOUBLE,   -- MJ/m²
    -- Medias diarias
    soil_moisture_0_7cm DOUBLE,   -- vol/vol
    soil_moisture_28_100cm DOUBLE,   -- vol/vol
    -- Topografía y subregión (desde aux_grid_pampa)
    subregion_id INT,
    subregion_name STRING,
    elevation DOUBLE,   -- metros
    slope DOUBLE,   -- grados
    aspect DOUBLE    -- grados
)
USING DELTA
COMMENT 'ERA5-Land Silver con unidades finales, clips aplicados, join con grilla'
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
);

-- =============================================================================
-- SILVER NASA FIRMS: focos de incendio filtrados y georeferenciados
-- Transformaciones con confidence n/h, deduplicados, con cell_id asignado.
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`02_silver`.silver_nasa_firms (
    -- Identificación
    cell_id STRING,   -- nodo grilla 0.25°
    -- Coordenadas originales del foco
    latitude DOUBLE,
    longitude DOUBLE,
    -- Tiempo
    acq_date DATE,
    timestamp_incendio TIMESTAMP,
    fecha_join DATE,     -- para joins en Gold
    hora_join INT,      -- hora UTC para análisis diurno/nocturno
    -- Atributos del foco
    confidence STRING,   -- n o h
    frp DOUBLE,   -- Fire Radiative Power (MW)
    daynight STRING,
    type INT       -- 0 = vegetación
)
USING DELTA
COMMENT 'NASA FIRMS Silver con confidence n/h, deduplicado, cell_id asignado'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');

-- =============================================================================
-- SILVER NDVI: índice de vegetación diario (forward-fill)
-- MODIS compuestos cada 16 días (Forward-fill para que cada día tenga el último valor disponible en training).
-- Una fila por (cell_id, fecha).
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`02_silver`.ndvi_silver (
    cell_id STRING,
    fecha DATE,
    ndvi DOUBLE    -- [-1, 1] — nulos imputados con mediana
)
USING DELTA
COMMENT 'MODIS NDVI Silver con compuesto 16 días con forward-fill diario'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');

-- =============================================================================
-- SILVER LAND COVER: cobertura del suelo anual (MODIS MCD12Q1) 
-- Una fila por (cell_id, year).
-- Fuente: land_cover_2022_2024.csv en /grid_setup/ (generado por grid_download_static_data).
-- NOTA: Categorías -> 0=Urbano/Otro, 1=Cultivo, 2=Vegetación Natural
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`02_silver`.land_cover_silver (
    cell_id STRING,
    year INT,
    land_cover_type INT,      -- tipo IGBP original MODIS
    land_cover_cat INT       -- 0=Otro, 1=Cultivo, 2=Veg.Natural
)
USING DELTA
COMMENT 'MODIS MCD12Q1 Land Cover Silver con categoría simplificada por nodo y año. CSV fuente en grid_setup/.'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');

-- =============================================================================
-- NOTA: static_features_silver (dist_road_km, pop_density_km2) fue eliminada.
-- Esas features están directamente en aux_grid_pampa y se propagan vía silver_era5.
-- =============================================================================

-- =============================================================================
-- SILVER OPENMETEO: seed + forecast unidos con FWI y rolling features
-- Une bronze_openmeteo_seed (35 días) + bronze_openmeteo_forecast (4 días),
-- aplica clips, join con aux_grid_pampa, calcula FWI secuencial y rolling.
-- Una fila por (cell_id, date) — total 39 días por nodo.
-- Los 4 días de forecast se identifican por date >= fecha_corte.
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`02_silver`.silver_openmeteo (
    cell_id STRING,
    date DATE,
    is_forecast BOOLEAN,  -- True=días futuros, False=histórico
    -- Variables climáticas — mismas unidades que silver_era5
    temperature_2m DOUBLE,
    relative_humidity DOUBLE,
    wind_speed_10m DOUBLE,   -- m/s
    wind_direction_10m DOUBLE,
    vpd_kpa DOUBLE,
    precipitation DOUBLE,
    solar_radiation DOUBLE,
    soil_moisture_0_7cm DOUBLE,
    soil_moisture_28_100cm DOUBLE,
    -- Topografía y subregión (desde aux_grid_pampa)
    subregion_id INT,
    subregion_name STRING,
    elevation DOUBLE,
    slope DOUBLE,
    aspect DOUBLE,
    -- Features estáticas (desde aux_grid_pampa)
    dist_road_km DOUBLE,
    pop_density_km2 DOUBLE,
    -- Land cover (desde land_cover_silver — último año disponible)
    land_cover_cat INT,
    -- NDVI (desde ndvi_silver — último valor disponible)
    ndvi DOUBLE,
    -- FWI calculado secuencialmente (fwi_calculator)
    mes INT,
    dia_anio INT,
    ffmc DOUBLE,
    dmc DOUBLE,
    isi DOUBLE,
    bui DOUBLE,
    fwi DOUBLE,
    -- Features temporales
    dias_secos INT
)
USING DELTA
COMMENT 'Open-Meteo Silver de seed+forecast unidos, FWI calculado, listo para Gold'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');

-- =============================================================================
-- VERIFICACIÓN
-- =============================================================================
SHOW TABLES IN fire_risk_project.`02_silver`;