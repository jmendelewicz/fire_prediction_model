-- =============================================================================
-- ddl_bronze.sql
-- Tablas Bronze de ingesta cruda sin modificaciones.
-- NOTA: El Auto Loader crea estas tablas automáticamente en el primer run, 
-- pero definirlas acá garantiza el schema esperado y permite auditoría.
-- Ejecutar después de: ddl_catalog.sql.
-- =============================================================================

USE CATALOG fire_risk_project;

-- =============================================================================
-- BRONZE ERA5: variables climáticas crudas desde GEE para training
-- Una fila por (cell_id, date). Columnas en unidades originales.
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`01_bronze`.bronze_era5 (
    -- Claves
    cell_id STRING,
    date STRING,
    -- Variables mediodía (15:00 UTC) 
    temperature_2m DOUBLE,   -- Kelvin
    dewpoint_2m DOUBLE,   -- Kelvin
    wind_u_10m DOUBLE,   -- m/s componente U
    wind_v_10m DOUBLE,   -- m/s componente V
    -- Suma diaria
    precipitation DOUBLE,   -- m
    solar_radiation DOUBLE,   -- J/m²
    -- Media diaria
    soil_moisture_0_7cm DOUBLE,   -- vol/vol
    soil_moisture_28_100cm DOUBLE,   -- vol/vol
    -- Metadata de ingesta agregada por Auto Loader
    source_filename STRING,
    ingestion_timestamp TIMESTAMP
)
USING DELTA
COMMENT 'ERA5-Land crudo desde GEE con unidades originales'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');

-- =============================================================================
-- BRONZE NASA FIRM: Focos de incendio VIIRS SNPP para training
-- Una fila por detección que incluye todos los campos de la API.
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`01_bronze`.bronze_nasa_firms (
    -- Coordenadas y tiempo
    latitude DOUBLE,
    longitude DOUBLE,
    acq_date DATE,
    acq_time STRING,   -- HHMM como string
    -- Atributos del foco
    brightness DOUBLE,
    scan DOUBLE,
    track DOUBLE,
    satellite STRING,
    instrument STRING,
    confidence STRING,   -- n=nominal, h=high, l=low
    version STRING,
    bright_t31 DOUBLE,
    frp DOUBLE,   -- Fire Radiative Power (MW)
    daynight STRING,   -- D=day, N=night
    type INT,      -- 0=veg, 1=active volcano, 2=static, 3=offshore
    -- Metadata de ingesta
    source_filename STRING,
    ingestion_timestamp TIMESTAMP
)
USING DELTA
COMMENT 'NASA FIRMS VIIRS SNPP con focos de incendio sin filtrar'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');

-- =============================================================================
-- BRONZE MODIS NDVI: índice de vegetación cada 16 días para train y ejecución
-- Una fila por (cell_id, fecha).
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`01_bronze`.bronze_modis_ndvi (
    cell_id STRING,
    fecha STRING,   -- YYYY-MM-DD
    ndvi DOUBLE,   -- escala cruda GEE × 0.0001
    -- Metadata de ingesta
    source_filename STRING,
    ingestion_timestamp TIMESTAMP
)
USING DELTA
COMMENT 'MODIS MOD13A2 NDVI con una fila por nodo por compuesto de 16 días'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');

-- =============================================================================
-- BRONZE LAND COVER: cobertura del suelo MODIS MCD12Q1 anual
-- Una fila por (cell_id, fecha). Datos crudos desde CSV en modis_static/.
-- La categorización simplificada se hace en Silver.
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`01_bronze`.bronze_land_cover (
    cell_id STRING,
    fecha STRING,            -- YYYY-MM-DD
    year INT,                -- año del producto
    land_cover_type INT,     -- tipo IGBP original MODIS (1-17)
    land_cover_cat INT,      -- categoría simplificada (0/1/2)
    -- Metadata de ingesta
    source_filename STRING,
    ingestion_timestamp TIMESTAMP
)
USING DELTA
COMMENT 'MODIS MCD12Q1 Land Cover crudo con clasificación IGBP por nodo y año'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');

-- =============================================================================
-- BRONZE FORECAST SEED: historial climático 35 días para seed del FWI
-- Tabla deslizante: etl_update_seed hace MERGE de 4 días nuevos cada día 
-- y elimina filas con más de 35 días de antigüedad.
-- Estructura idéntica al output de extract_openmeteo_forecast.
-- Va en Bronze porque es la primera capa de persistencia estructurada
-- de los datos de Open-Meteo, no un archivo crudo de Landing.
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`01_bronze`.bronze_openmeteo_seed (
    cell_id STRING,
    date DATE,
    -- Variables climáticas — unidades finales (Open-Meteo ya las da procesadas)
    temperature_2m DOUBLE,   -- °C
    relative_humidity DOUBLE,   -- % [0, 100]
    wind_speed_10m DOUBLE,   -- m/s (convertido de km/h en etl_extract)
    wind_direction_10m DOUBLE,   -- grados
    precipitation DOUBLE,   -- mm
    solar_radiation DOUBLE,   -- MJ/m²
    soil_moisture_0_7cm DOUBLE,   -- vol/vol
    soil_moisture_28_100cm DOUBLE,   -- vol/vol
    vpd_kpa DOUBLE    -- kPa ≥ 0
)
USING DELTA
COMMENT 'Open-Meteo Bronze con seed histórico de 35 días para cálculo FWI en inferencia diaria'
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.enableChangeDataFeed'       = 'true'
);

-- =============================================================================
-- BRONZE OPENMETEO FORECAST: pronóstico diario 4 días 
-- Primera ingesta estructurada del CSV de forecast descargado de Open-Meteo.
-- Se sobreescribe completamente cada día con el nuevo pronóstico.
-- Se elimina después de generar forecast_gold_temp en Gold.
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`01_bronze`.bronze_openmeteo_forecast (
    cell_id STRING,
    date DATE,
    temperature_2m DOUBLE,   -- °C
    relative_humidity DOUBLE,   -- % [0, 100]
    wind_speed_10m DOUBLE,   -- m/s
    wind_direction_10m DOUBLE,   -- grados
    precipitation DOUBLE,   -- mm
    solar_radiation DOUBLE,   -- MJ/m²
    soil_moisture_0_7cm DOUBLE,   -- vol/vol
    soil_moisture_28_100cm DOUBLE,   -- vol/vol
    vpd_kpa DOUBLE    -- kPa ≥ 0
)
USING DELTA
COMMENT 'Open-Meteo Bronze Forecast con pronóstico de 4 días, se sobreescribe diariamente'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');

-- =============================================================================
-- VERIFICACIÓN
-- =============================================================================
SHOW TABLES IN fire_risk_project.`01_bronze`;