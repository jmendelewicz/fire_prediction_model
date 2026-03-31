# Librerías

from pyspark.sql.functions import current_timestamp, col
import logging
%sql
CREATE VOLUME IF NOT EXISTS fire_risk_project.01_bronze.vol_procesamiento;

-- Es necesario crear el volumen, dado que no puedo almacenar los metadatos y las carpetas en la raiz
# Logger para avisar

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s', datefmt='%H:%M:%S', force=True)
logger = logging.getLogger("ETL_BRONZE")

# Rutas

PATH_SOURCE_NASA = "/Volumes/fire_risk_project/00_landing/nasa_files/nasa_firms" # Rutas de datos Landing
PATH_CHECKPOINT_NASA = "/Volumes/fire_risk_project/01_bronze/vol_procesamiento/nasa_files/checkpoint" # Checkpoint
PATH_SCHEMA_NASA = "/Volumes/fire_risk_project/01_bronze/vol_procesamiento/nasa_files/schema_nasa" # Schema Locations (Memoria de la estructura de datos) 
TABLE_NASA = "fire_risk_project.01_bronze.bronze_nasa_firms" # Tablas a Bronze
%run ../00_setup_functions/common_functions
# Ejecución NASA 
procesar_a_bronze(PATH_SOURCE_NASA, TABLE_NASA, PATH_CHECKPOINT_NASA, PATH_SCHEMA_NASA, "csv")
%sql
-- Conteo total de las entradas en las tablas (con Query de SQL)

SELECT 'NASA FIRMS' as dataset, count(*) as total_filas FROM fire_risk_project.01_bronze.bronze_nasa_firms
%sql
-- Rastreamos los metadatos para ver que todo funcione bien.

SELECT source_filename, ingestion_timestamp 
FROM fire_risk_project.01_bronze.bronze_nasa_firms 
LIMIT 3;
# Finalizado el proceso ingesta para datos NASA FIRMS