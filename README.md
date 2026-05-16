# AlertaFuego — Monitoreo climático-agrícola con módulo de predicción de incendios (Pampa argentina)

> Trabajo final de diplomatura. Plataforma de monitoreo climático-agrícola con un módulo operativo de **predicción de riesgo de incendios** sobre la región pampeana (2266 nodos a 0.25°). Implementación de XGBoost optimizado por Sparrow Search Algorithm (SSA) + autocorrelación espacial. Inspirado en Wang, Yu & Wang (2026), *Spatial Prediction of Forest Fire Risk in Guangdong Province*, AppliedMath 6(1):10. Ver `references/appliedmath-06-00010.pdf`.

Las features del pipeline (FWI, VPD, soil moisture, NDVI, sequía) son **cross-purpose**: alimentan tanto el modelo de fuegos como un módulo derivado de **estrés agronómico** (documentado en `PROJECT_ARCHITECTURE.md §1` y §6.f como extensión natural).

---

## Modelo canónico

| Versión | Estado | Notas |
|---------|--------|-------|
| **v4**  | **Producción** | XGBoost + SSA + 42 features (38 base + 3 espaciales + interactions in-runtime), split temporal honesto, sin leakage, FWI con tabla Le ajustada a 35°S |
| v3      | Archivado en `02_ml_model/legacy/` | Sin features espaciales — sirvió de comparación |
| v2      | Archivado en `02_ml_model/legacy/` | Base inicial |
| v1      | Solo histórico | Baseline |

Para correr el training canónico (después de bajar `save_gold.csv` del volumen Databricks):

```bash
python 02_ml_model/model_v4/train_model_v4.py
```

Tarda ~15–25 minutos en CPU moderno. Genera en `02_ml_model/model_v4/`:
- `xgboost_v4.json` — modelo serializado
- `feature_cols_v4.pkl` — orden exacto de las 42 features (XGBoost es position-sensitive)
- `ndvi_means_per_cell_v4.csv` — medias persistidas para serving consistency
- `ndvi_global_mean_v4.json` — fallback global para celdas no presentes en train
- `best_params_v4.json` — hiperparámetros encontrados por SSA
- `metricas_v4.csv` — incluye **MD5 del dataset** y `random_state` (reproducibilidad bit-a-bit)
- `threshold_calibration_v4.csv`, `feature_importance_v4.csv`, `ssa_convergence_v4.csv`
- `evaluation_v4.png` — plots ROC, PR, convergencia SSA, top-20 importance

---

## Estructura del repositorio

```
fire_prediction_model/
├── 00_setup/                       # Setup Databricks (one-time)
│   ├── 00_DDLs/                    # CREATE TABLE bronze/silver/gold/catalog
│   ├── 00_grid/                    # aux_grid_pampa (2266 nodos 0.25°×0.25°)
│   └── 00_common_functions/        # fwi_calculator, gee_helpers, openmeteo_client, ...
│
├── 01_etl_pipeline/                # Pipeline Medallion (Databricks)
│   ├── 01_landing/                 # Extract a CSV: ERA5, MODIS, NASA FIRMS, OpenMeteo
│   ├── 02_bronze/                  # ingest_datasets.py (Auto Loader idempotente)
│   ├── 03_silver/                  # transform_*.py + audit_silver.py
│   └── 04_gold/                    # build_gold.py + save_gold.py (training)
│                                   # build_gold_forecast.py (serving — 42 features aligned a v4)
│
├── 02_ml_model/                    # Training local sobre save_gold.csv
│   ├── model_v4/                   # ← VERSIÓN CANÓNICA + cloud_inference_engine.py
│   └── legacy/                     # v2, v3 — ver legacy/README.md
│
├── 03_orchestration/               # Orquestador diario Databricks
│   └── cloud_orchestration_main.ipynb
│
├── 04_frontend/                    # Dashboard React/Vite (consumer del predictions_ui.json)
│
├── references/                     # Paper de referencia + notas
├── PROJECT_ARCHITECTURE.md         # Arquitectura completa + roadmap
├── DATABRICKS_RUNBOOK.md           # Paso-a-paso para correr el ETL en Databricks
├── AUDIT.md                        # Auditoría exhaustiva
└── requirements.txt                # Dependencias Python locales (training)
```

---

## Cómo correr el proyecto end-to-end

### 1. Setup en Databricks (one-time)

Ver `DATABRICKS_RUNBOOK.md` § 1-3. Resumen:

```sql
-- En el SQL Editor de Databricks, en orden:
00_setup/00_DDLs/ddl_catalog.sql
00_setup/00_DDLs/ddl_bronze.sql
00_setup/00_DDLs/ddl_silver.sql
00_setup/00_DDLs/ddl_gold.sql
```

```python
# En notebooks Databricks:
00_setup/00_grid/grid_setup.py
00_setup/00_grid/grid_subregion_classification.py
00_setup/00_grid/grid_download_static_data.py   # OSM roads + WorldPop + MODIS LC
```

### 2. Setup secrets (one-time)

```bash
# Crear scope + cargar API key NASA (revocar la vieja primero — ver AUDIT.md CN-1)
databricks secrets create-scope --scope fire-risk
databricks secrets put --scope fire-risk --key nasa_firms_api_key
```

### 3. Pipeline de training (one-time o tras cambios de scope/datos)

Ver `DATABRICKS_RUNBOOK.md` § 4-7.

```
Landing → Bronze → Silver → audit_silver → build_gold → save_gold
                                              ↓
                                      save_gold.csv (~1 GB)
                                              ↓ download
                                       train_model_v4.py (local)
                                              ↓ upload modelo + artefactos
                                       /Volumes/.../training_dataset_v2/
```

### 4. Pipeline de serving (diario)

`cloud_orchestration_main.ipynb` corre los 5 pasos del job. Se programa con un Databricks Job (cron 06:00 UTC recomendado).

### 5. Frontend

```bash
cd 04_frontend
npm install
npm run dev          # desarrollo (http://localhost:5173)
npm run build        # build → dist/
```

---

## Setup local (training)

```bash
# 1. Crear entorno
python -m venv .venv
.venv\Scripts\Activate.ps1       # Windows PowerShell
# source .venv/bin/activate      # Linux/macOS

# 2. Instalar
pip install -r requirements.txt

# 3. Obtener save_gold.csv (~1 GB)
# Generarlo en Databricks (build_gold + save_gold) y descargarlo desde:
# /Volumes/fire_risk_project/03_gold/training_dataset_v2/training_dataset_v2.csv
# Renombrarlo a save_gold.csv y dejarlo en la raíz del repo (gitignored).

# 4. Entrenar
python 02_ml_model/model_v4/train_model_v4.py
```

---

## Metodología (resumen)

- **Grilla**: 2266 nodos sobre la Pampa argentina, resolución 0.25° (~25 km).
- **Período**: 2022-01-01 → 2024-12-31 (3 años).
- **Spin-up FWI**: se descartan los primeros 60 días del training (`>= 2022-03-01`) — DMC/DC necesitan ese tiempo para converger desde `FFMC=85, DMC=6, DC=15`.
- **Split temporal 3-way**:
  - Train balanceado 1:1 por subregión: `[2022-03-01, 2024-05-01)`.
  - Validation (early-stop reference, distribución real ~2.4%): `[2024-05-01, 2024-07-01)`.
  - **Test held-out**: `[2024-07-01, 2024-12-31]` — nunca toca el modelo.
- **Target**: `fire_occurred ∈ {0,1}` derivado de NASA FIRMS VIIRS.
- **FWI**: Van Wagner 1987 + tabla Le ajustada a 35°S (corrección `AUDIT.md` C-8).
- **Optimización**: SSA 15×15 con early-stop por paciencia, AP en CV 3-fold sobre train balanceado.
- **Calibración**: dos thresholds — F1 (balance) y F2 (recall-biased, default operativo).
- **Reproducibilidad**: MD5 del dataset + random_state persistidos en `metricas_v4.csv`.

Detalles completos en `PROJECT_ARCHITECTURE.md`.

---

## Pipeline en la nube (Databricks)

Todos los scripts bajo `00_setup/`, `01_etl_pipeline/` y `03_orchestration/` están pensados para correr como notebooks de Databricks. No requieren ejecución local. Ver `DATABRICKS_RUNBOOK.md` para el orden y criterios de validación.

---

## Auditoría

`AUDIT.md` contiene la auditoría completa del proyecto. Sumario al 2026-05-16:

| Severidad | Auditoría 2026-05-15 | Auditoría 2026-05-16 | Cerrados | Abiertos (documentados) |
|-----------|---------------------|---------------------|----------|------------------------|
| Crítico   | 8                   | +3                  | 11       | 0                      |
| Alto      | 13                  | +6                  | 11       | 8 (roadmap)            |
| Medio     | 9                   | +3                  | 6        | 6                      |
| Bajo      | 5                   | +2                  | 2        | 5                      |

Los críticos cerrados incluyen los leakages metodológicos (C-1 NDVI, C-2 eval_set, C-9 fire_vecinos forward), el bug numérico del FWI (C-8 tabla Le), el frontend roto (C-7), el schema drift train↔serve (C-3/C-4/C-5), y la API key de NASA expuesta (CN-1). Los altos abiertos son trabajos de production-grade (Feature Store, DLT EXPECT, Databricks Jobs) documentados como roadmap en `PROJECT_ARCHITECTURE.md §6`.

---

## Licencia y autoría

Trabajo final de diplomatura — Julián Mendelewicz, 2026.
