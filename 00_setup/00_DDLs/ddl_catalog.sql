-- =============================================================================
-- ddl_catalog.sql
-- Inicialización del catálogo Unity Catalog
-- Crea catálogo, schemas (capas Medallion) y volúmenes de almacenamiento.
-- Primera ejecución.
-- =============================================================================

-- Catálogo principal
CREATE CATALOG IF NOT EXISTS fire_risk_project;
USE CATALOG fire_risk_project;

-- =============================================================================
-- SCHEMAS (capas Medallion)
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS fire_risk_project.`00_landing`;
CREATE SCHEMA IF NOT EXISTS fire_risk_project.`01_bronze`;
CREATE SCHEMA IF NOT EXISTS fire_risk_project.`02_silver`;
CREATE SCHEMA IF NOT EXISTS fire_risk_project.`03_gold`;

-- =============================================================================
-- VOLÚMENES — 00_landing -> Almacenan archivos crudos extraídos de fuentes externas.
-- =============================================================================

-- Grilla y setup
CREATE VOLUME IF NOT EXISTS fire_risk_project.`00_landing`.ecoregions;
    -- Shapefile RESOLVE Ecoregions 2017

-- Datos climáticos históricos (Training Pipeline)
CREATE VOLUME IF NOT EXISTS fire_risk_project.`00_landing`.era5_files;
    -- CSVs mensuales ERA5-Land via GEE (era5_YYYY_MM.csv)

CREATE VOLUME IF NOT EXISTS fire_risk_project.`00_landing`.nasa_files;
    -- CSVs NASA FIRMS VIIRS (nasa_YYYYMMDD.csv)

CREATE VOLUME IF NOT EXISTS fire_risk_project.`00_landing`.modis_ndvi;
    -- CSV NDVI MODIS 16 días (ndvi_2022_2024.csv)

CREATE VOLUME IF NOT EXISTS fire_risk_project.`00_landing`.modis_static;
    -- Obsoleto: land_cover_2022_2024.csv se genera en grid_setup/ (ver grid_download_static_data)
    -- Mantenido por retrocompatibilidad — puede eliminarse en refactorizaciones futuras.

CREATE VOLUME IF NOT EXISTS fire_risk_project.`00_landing`.grid_setup;
    -- osm_road_distance.csv      ← output de grid_download_static_data (OSM)
    -- population_density.csv     ← output de grid_download_static_data (WorldPop)
    -- land_cover_2022_2024.csv   ← output de grid_download_static_data (MODIS MCD12Q1)
    -- grilla_pampa.png           ← output de grid_visualization

-- Datos forecast (Daily Inference Pipeline)
CREATE VOLUME IF NOT EXISTS fire_risk_project.`00_landing`.open_meteo_forecast;
    -- /forecast/  -> forecast_YYYYMMDD.csv  (extracción diaria 4 días, se sobreescribe)
    -- /seed/      -> seed.csv (extracción one-time / reset, 35 días históricos)
    --               se conserva como respaldo para resetear bronze.bronze_openmeteo_seed

-- =============================================================================
-- VOLÚMENES — 01_bronze -> Checkpoints y schemas del Auto Loader (Structured Streaming).
-- =============================================================================
CREATE VOLUME IF NOT EXISTS fire_risk_project.`01_bronze`.vol_procesamiento;
    -- Checkpoints: /vol_procesamiento/era5/checkpoint
    --              /vol_procesamiento/nasa/checkpoint
    --              /vol_procesamiento/ndvi/checkpoint
    -- Schemas:     /vol_procesamiento/era5/schema
    --              /vol_procesamiento/nasa/schema
    --              /vol_procesamiento/ndvi/schema

-- =============================================================================
-- VOLÚMENES — 03_gold -> Checkpoints del modelo, datasets de entrenamiento y outputs del pipeline.
-- =============================================================================
CREATE VOLUME IF NOT EXISTS fire_risk_project.`03_gold`.training_dataset_v2;
    -- gold_checkpoint.csv (checkpoint Gold p1)
    -- training_dataset_v2.csv (dataset final para entrenamiento local)
    -- xgboost_v2.json (modelo entrenado) 

CREATE VOLUME IF NOT EXISTS fire_risk_project.`03_gold`.outputs;
    -- predictions_ui.json (output diario)

-- =============================================================================
-- TABLA DE REFERENCIA: aux_grid_pampa
-- Grilla maestra 0.25° con todos los features estáticos generada por 00_setup/grid_setup/
-- =============================================================================
CREATE TABLE IF NOT EXISTS fire_risk_project.`00_landing`.aux_grid_pampa (
    -- Identificación del nodo
    cell_id STRING,   -- formato: "-34.2500_-63.0000"
    latitude DOUBLE,
    longitude DOUBLE,
    grid_row INT,      -- índice fila en la grilla
    grid_col INT,      -- índice columna en la grilla
    -- Subregión ecológica (RESOLVE Ecoregions 2017)
    subregion_id INT,      -- 1=Pampa Humeda, 3=Delta, 4=Monte...
    subregion_name STRING,
    -- Topografía (SRTM 30m via GEE)
    elevation DOUBLE,   -- metros sobre el nivel del mar
    slope DOUBLE,   -- grados
    aspect DOUBLE,   -- grados (0=Norte, 90=Este...)
    -- Features estáticos de infraestructura y población
    dist_road_km DOUBLE,   -- distancia mínima a ruta más cercana (km)
    pop_density_km2 DOUBLE,   -- densidad poblacional (hab/km²)
    -- Máscara de tierra
    is_valid BOOLEAN   -- True = dentro de Argentina/Uruguay
)
USING DELTA
COMMENT 'Grilla maestra 0.25° con 2266 nodos y features estáticos. Generada por 00_setup/grid_setup.'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true');
 
-- =============================================================================
-- VERIFICACIÓN
-- =============================================================================
SHOW SCHEMAS IN fire_risk_project;
SHOW TABLES IN fire_risk_project.`00_landing`;