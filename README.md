# Fire Prediction Model — Pampa argentina

Trabajo final de diplomatura. Modelo de predicción diaria de riesgo de incendios sobre la región pampeana, basado en XGBoost optimizado por *Sparrow Search Algorithm* (SSA) con autocorrelación espacial. Pipeline en Databricks (arquitectura medallion) y training local.

Inspirado en Wang, Yu & Wang (2026), *Spatial Prediction of Forest Fire Risk in Guangdong Province*, AppliedMath 6(1):10. El paper está en `references/`.

---

## Modelo

El modelo canónico es **v4**, entrenado con 42 features sobre 2266 nodos a 0.25° (~25 km). Métricas sobre el test held-out (último semestre de 2024):

| Métrica | Valor |
|---|---|
| AUC-ROC | 0.8965 |
| Average Precision | 0.3038 |
| Precision / Recall @ F2 | 0.218 / 0.634 |
| Precision / Recall @ F1 | 0.334 / 0.424 |

## Estructura del repositorio

```
fire_prediction_model/
├── 00_setup/                       # Setup Databricks (one-time)
│   ├── 00_DDLs/                    # CREATE TABLE catalog/bronze/silver/gold
│   ├── 00_grid/                    # aux_grid_pampa (2266 nodos 0.25°)
│   └── 00_common_functions/        # fwi_calculator, gee_helpers, openmeteo_client
│
├── 01_etl_pipeline/                # Pipeline medallion (Databricks)
│   ├── 01_landing/                 # ERA5, MODIS, NASA FIRMS, Open-Meteo
│   ├── 02_bronze/                  # ingest_datasets.py (Auto Loader)
│   ├── 03_silver/                  # transform_*.py + audit_silver.py
│   └── 04_gold/                    # build_gold + save_gold (training)
│                                   # build_gold_forecast (serving)
│
├── 02_ml_model/                    # Training local
│   └── model_v4/                   # Modelo canónico + cloud_inference_engine.py
│
├── 03_orchestration/               # Orquestador diario
│   └── cloud_orchestration_main.ipynb
│
├── references/                     # Paper de referencia + notas previas
└── requirements.txt
```

---

## Pipeline end-to-end

### 1. Setup Databricks (one-time)

Crear catálogo y tablas:

```sql
00_setup/00_DDLs/ddl_catalog.sql
00_setup/00_DDLs/ddl_bronze.sql
00_setup/00_DDLs/ddl_silver.sql
00_setup/00_DDLs/ddl_gold.sql
```

Generar la grilla y los rasters estáticos:

```python
00_setup/00_grid/grid_setup.py
00_setup/00_grid/grid_subregion_classification.py
00_setup/00_grid/grid_download_static_data.py    # OSM roads + WorldPop + MODIS LC
```

Para NASA FIRMS hay que cargar la API key en Databricks Secrets:

```bash
databricks secrets create-scope --scope fire-risk
databricks secrets put --scope fire-risk --key nasa_firms_api_key
```

### 2. Generación del dataset

```
Landing → Bronze → Silver → audit_silver → build_gold → save_gold
                                              ↓
                                       save_gold.csv (~1 GB)
```

`save_gold.csv` está publicado como release de GitHub porque excede los 100 MB del repo. También se puede regenerar corriendo el ETL completo.

### 3. Training local

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python 02_ml_model/model_v4/train_model_v4.py
```

### 4. Serving diario

`03_orchestration/cloud_orchestration_main.ipynb` corre los 5 pasos del job de inferencia (extract → ingest → silver_openmeteo → gold_forecast → inferencia v4). Se programa con un Databricks Job (cron 06:00 UTC recomendado).

---

## Metodología

- **Grilla**: 2266 celdas sobre la Pampa argentina a 0.25°×0.25°.
- **Período**: 2022-01-01 → 2024-12-31.
- **Spin-up FWI**: se descartan las primeras ~8 semanas del training (`>= 2022-03-01`) porque DMC y DC necesitan ese tiempo para converger desde los defaults (`FFMC=85, DMC=6, DC=15`).
- **Split temporal 3-way**:
  - Train (balanceado 1:1 por subregión): `[2022-03-01, 2024-05-01)`
  - Validation (distribución real ~2.4%): `[2024-05-01, 2024-07-01)`
  - Test held-out: `[2024-07-01, 2024-12-31]`
- **Target**: `fire_occurred ∈ {0,1}` derivado de NASA FIRMS VIIRS.
- **FWI**: Van Wagner (1987) + tabla Le ajustada a hemisferio sur (~35°S).
- **Optimización**: SSA 15×15 con early-stop por paciencia, AP en CV 3-fold sobre train balanceado.
- **Calibración**: dos thresholds — F1 (balance) y F2 (recall-biased, operativo).
- **Reproducibilidad**: MD5 del dataset + random_state persistidos en `metricas_v4.csv`.

Las features incluyen el FWI canadiense completo (FFMC, DMC, DC, ISI, BUI, FWI), variables meteorológicas (temperatura, humedad relativa, VPD, viento, precipitación, radiación solar), humedad de suelo en dos profundidades, NDVI y anomalía de NDVI por celda, estáticas (elevación, pendiente, aspecto, distancia a caminos, densidad de población, cobertura), rolling means (14 y 30 días), estacionalidad (sin/cos), calendario agrícola, interacciones (`fwi × vpd`, `temp × dry`, `wind × fwi`), y autocorrelación espacial (`fwi_vecinos_mean`, `fwi_vecinos_max`, `fire_vecinos_3d`).
