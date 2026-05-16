# AlertaFuego — Predicción de Riesgo de Incendio en la Pampa Argentina

> Trabajo final de diplomatura. Implementación de un sistema de predicción de riesgo de incendio basado en XGBoost optimizado por Sparrow Search Algorithm (SSA), inspirado en la metodología de Wang, Yu & Wang (2026), *Spatial Prediction of Forest Fire Risk in Guangdong Province*, AppliedMath 6(1):10. Ver `references/appliedmath-06-00010.pdf`.

---

## Modelo canónico

| Versión | Estado | Notas |
|---------|--------|-------|
| **v4**  | **Producción** | XGBoost + SSA + features espaciales (queen contiguity), split temporal honesto, sin leakage |
| v3      | Archivado en `02_ml_model/legacy/` | Sin features espaciales |
| v2      | Archivado en `02_ml_model/legacy/` | Base inicial sobre la que se construyeron las versiones siguientes |
| v1      | Solo histórico (no incluido en repo) | Modelo baseline |

Para correr el training canónico:

```bash
python 02_ml_model/model_v4/train_model_v4.py
```

Tarda ~15–25 minutos en CPU moderno. Genera en `02_ml_model/model_v4/`:
- `xgboost_v4.json` — modelo serializado
- `best_params_v4.json` — hiperparámetros encontrados por SSA
- `metricas_v4.csv`, `threshold_calibration_v4.csv`, `feature_importance_v4.csv`
- `ssa_convergence_v4.csv` — curva de convergencia del SSA
- `evaluation_v4.png` — plots de ROC, PR, convergencia e importance

---

## Estructura del repositorio

```
fire_prediction_model/
├── 00_setup/                       # Setup de la grilla maestra y DDLs (Databricks)
│   ├── 00_DDLs/                    # CREATE TABLE de bronze/silver/gold/catalog
│   ├── 00_grid/                    # Generación de aux_grid_pampa (2266 nodos 0.25°×0.25°)
│   └── 00_common_functions/        # Módulos compartidos: fwi_calculator, gee_helpers, ...
│
├── 01_etl_pipeline/                # Pipeline Medallion (Databricks)
│   ├── 01_landing/                 # Extracción a CSV: ERA5, MODIS, NASA FIRMS, OpenMeteo
│   ├── 02_bronze/                  # Ingest a Delta (auto-loader, idempotente)
│   ├── 03_silver/                  # Limpieza + normalización + audit
│   └── 04_gold/                    # OBT (One Big Table) lista para training
│
├── 02_ml_model/                    # Training local sobre save_gold.csv
│   ├── model_v4/                   # ← VERSIÓN CANÓNICA
│   └── legacy/                     # Versiones anteriores (v2, v3) — ver legacy/README.md
│
├── 03_orchestration/               # Orquestador maestro Databricks (job diario)
│   └── cloud_orchestration_main.ipynb
│
├── 04_frontend/                    # Dashboard React/Vite (visualización)
│
├── references/                     # Paper de referencia + notas del autor
├── PROJECT_ARCHITECTURE.md         # Descripción extendida de la arquitectura
├── AUDIT.md                        # Auditoría exhaustiva — hallazgos + plan de fixes
└── requirements.txt                # Dependencias Python locales (training)
```

---

## Setup local

```bash
# 1. Crear entorno
python -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\Activate.ps1       # Windows PowerShell

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Obtener save_gold.csv (~1 GB)
# Generarlo con el pipeline ETL en Databricks (01_etl_pipeline/04_gold/),
# o descargarlo del volumen /Volumes/fire_risk_project/03_gold/training_dataset_v2/.
# Colocar en la raíz del repo (queda ignorado por .gitignore).
```

---

## Frontend

```bash
cd 04_frontend
npm install
npm run dev          # desarrollo (http://localhost:5173)
npm run build        # build de producción → dist/
```

---

## Pipeline en la nube (Databricks)

Todos los scripts bajo `00_setup/`, `01_etl_pipeline/` y `03_orchestration/` están pensados para ejecutarse como notebooks de Databricks. No requieren ejecución local. El orquestador `03_orchestration/cloud_orchestration_main.ipynb` arma el job diario:

1. `etl_extract_openmeteo_forecast` (Landing — 4 días de forecast)
2. `transform_openmeteo` (Silver — actualiza ventana 35d + forecast)
3. `build_gold_forecast` (Gold — features finales para inferencia)
4. Inferencia (XGBoost v4)
5. Export JSON → frontend

---

## Metodología (resumen)

- **Grilla**: 2266 nodos sobre la Pampa argentina, resolución 0.25° × 0.25° (~25 km).
- **Período de entrenamiento**: 2022-01-01 → 2024-12-31.
- **Split temporal estricto**: `< 2024-07-01` para train, `≥ 2024-07-01` para test. Los últimos ~60 días del train se separan como validation slice para early stopping.
- **Target**: `fire_occurred ∈ {0, 1}` derivado de NASA FIRMS (VIIRS active fires). Tasa basal ~2.4 %.
- **Balanceo**: muestreo 1:1 sobre el TRAIN, estratificado por subregión. Test conserva el desbalance real.
- **FWI**: sistema canadiense (Van Wagner 1987) ajustado a hemisferio sur ~35°S.
- **Optimización**: SSA con 15 sparrows × 15 iteraciones, early stop por paciencia (3).
- **Calibración**: dos umbrales óptimos — F1 (balance) y F2 (recall-biased, operación).

Detalles en `PROJECT_ARCHITECTURE.md`.

---

## Auditoría

`AUDIT.md` contiene la auditoría completa del proyecto (8 críticos / 13 altos / 9 medios / 5 bajos). Los hallazgos críticos C-1, C-2, C-7 y C-8 fueron corregidos en esta entrega. Los demás están documentados como roadmap.

---

## Licencia y autoría

Trabajo final de diplomatura — Julián Mendelewicz, 2026.
