# 🔥 Sistema de Alerta Temprana de Incendios Forestales — Sierras de Córdoba

[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://www.python.org/)
[![PySpark](https://img.shields.io/badge/PySpark-3.5-orange)](https://spark.apache.org/)
[![Databricks](https://img.shields.io/badge/Platform-Databricks-red)](https://databricks.com/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

Sistema de machine learning para la predicción de incendios forestales con **12 horas de anticipación**, construido sobre un pipeline de datos medallón en Databricks, utilizando exclusivamente fuentes de datos públicas y gratuitas.

> **Trabajo Final — Diplomatura en Ciencia de Datos**

---

## 📋 Tabla de Contenidos

- [Descripción del Proyecto](#descripción-del-proyecto)
- [Resultados](#resultados)
- [Arquitectura](#arquitectura)
- [Datos](#datos)
- [Estructura del Repositorio](#estructura-del-repositorio)
- [Instalación y Uso](#instalación-y-uso)
- [Pipeline en Databricks](#pipeline-en-databricks)
- [Modelado](#modelado)
- [Limitaciones y Trabajo Futuro](#limitaciones-y-trabajo-futuro)

---

## Descripción del Proyecto

Las Sierras de Córdoba concentran una fracción significativa de los incendios forestales anuales de Argentina. Este proyecto construye un sistema de alerta temprana capaz de predecir, para cada celda de una grilla de 800 nodos sobre la región, si ocurrirá un incendio en las próximas **12 horas**, usando condiciones climáticas actuales y memoria temporal.

### Área de estudio

```
Bounding box: LAT [-33.5, -29.5] | LON [-65.5, -63.5]
Resolución:   0.1° × 0.1° (~11 km por celda)
Grilla:       40 filas × 20 columnas = 800 nodos
```

Cubre las Sierras de Córdoba completas, incluyendo los Comechingones al sur, las Sierras Chicas al este, y las Serranías de Ischilín al norte.

### Comparativa con trabajo relacionado

El proyecto toma como referencia a [Phoenix Eye (Flores et al., 2024)](https://github.com/jbric16/FlameForecast_Project), un sistema similar para México basado en ConvLSTM sobre imágenes MODIS.

---

## Resultados

### Random Forest V3 (modelo actual)

| Métrica | Resultado |
|---|---|
| ROC-AUC | 0.9163 |
| Recall | 54.1% |
| Precision | 19.0% |
| F2-Score | ~0.40 |
| Umbral óptimo | ~0.08–0.10 |

> **Contexto:** Con un desbalance de ~900:1 (bloques sin fuego vs con fuego), optimizamos por F2-score en lugar de accuracy, priorizando Recall sobre Precision. En sistemas de alerta temprana, un falso negativo (incendio no detectado) tiene un costo operativo mucho mayor que una falsa alarma.

---

## Arquitectura

El pipeline sigue la arquitectura medallón estándar de Databricks:

```
00_landing/          01_bronze/           02_silver/           03_gold/
─────────────        ─────────────        ─────────────        ─────────────
nasa_firms/    →     bronze_nasa    →     silver_nasa    →     gold_dataset
open_meteo/    →     bronze_meteo   →     silver_meteo   →        _full
aux_grid/                               silver_grid      →     gold_dataset
                                                                  _output
```

Cada capa tiene una responsabilidad clara:

- **Landing:** archivos crudos (CSV de NASA FIRMS, JSON de Open-Meteo)
- **Bronze:** ingesta incremental con Auto Loader, sin transformaciones
- **Silver:** limpieza, normalización, tipado, asignación a grilla
- **Gold:** join de fuentes, feature engineering, tabla lista para modelado

---

## Datos

### Fuentes

**NASA FIRMS — VIIRS SNPP (focos de calor)**
- Fuente: [firms.modaps.eosdis.nasa.gov](https://firms.modaps.eosdis.nasa.gov/)
- Período: 2020-01-01 → 2025-12-30
- Filtro: confidence `"n"` (nominal) y `"h"` (high) únicamente
- Total focos válidos: ~28,000 después de limpieza

**Open-Meteo Archive API (clima horario)**
- Fuente: [open-meteo.com](https://open-meteo.com/)
- Período: 2020-01-01 → 2025-12-30
- Resolución: horaria por nodo de grilla
- Variables: temperatura, humedad, VPD, precipitación, viento, ET₀, humedad de suelo

**Grilla sintética**
- Generada programáticamente sobre el bounding box
- 800 nodos en resolución 0.1°

### Variables del modelo

El dataset final contiene ~50 features por bloque de 6 horas por nodo:

- **Base:** temperatura, humedad relativa, VPD, precipitación, viento, ET₀, humedad de suelo
- **Lag:** memoria temporal a 6h, 12h, 24h y 48h atrás
- **Rolling:** medias y sumas móviles a 24h, 48h y 4 días
- **Sequía:** horas consecutivas sin lluvia (feature más predictivo)
- **Tendencias:** delta de temperatura, VPD y viento en el último bloque
- **Espaciales:** promedio de variables climáticas e incendios en los 8 nodos vecinos más cercanos
- **Estacionalidad:** codificación cíclica de mes y hora del día

### Nota sobre extracción de datos

La API gratuita de Open-Meteo Archive tiene un límite horario de ~30 nodos por hora. Para un proyecto en producción, la alternativa más eficiente es acceder directamente al bucket público de Open-Meteo en AWS S3 (`s3://openmeteo`, región `us-west-2`), que expone los mismos datos sin rate limits. Esto está documentado en la [documentación de Open-Meteo](https://openmeteo.com) como mejora futura para este proyecto.

---

## Estructura del Repositorio

```
fire_prediction_model/
│
├── 00_setup_functions/
│   └── common_functions.ipynb      # Funciones compartidas (Auto Loader, etc.)
│
├── 00_landing/
│   ├── grid_setup.ipynb            # Generación de la grilla de 800 nodos
│   ├── extract_nasa.ipynb          # Extracción NASA FIRMS (VIIRS)
│   └── extract_openMeteo.ipynb     # Extracción Open-Meteo Archive API
│
├── 01_bronze/
│   └── ingest_nasa.ipynb           # Ingesta NASA a Bronze con Auto Loader
│
├── 02_silver/
│   ├── transform_nasa.ipynb        # Limpieza y asignación a grilla (NASA)
│   ├── transform_openMeteo.ipynb   # Desagrupado horario y join con grilla
│   └── transform_grid.ipynb        # Optimización de tabla de grilla
│
├── 03_silver → 04_gold/
│   └── transform_gold_table.ipynb  # Join clima + incendios → tabla base
│
├── scripts/
│   ├── extract_nasa_v2.py          # Extractor NASA con bounding box ampliado
│   ├── extract_openMeteo_v3.py     # Extractor Open-Meteo con manejo automático de rate limit
│   ├── feature_engineering_v3.py   # Feature engineering completo (Databricks)
│   └── random_forest_v3.py         # Entrenamiento y evaluación local (sklearn)
│
├── notebooks/
│   └── eda.ipynb                   # Análisis exploratorio de datos (WIP)
│
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Instalación y Uso

### Requisitos

```bash
# Entorno local (modelado)
pip install -r requirements.txt

# Databricks (pipeline de datos)
# Usar Databricks Runtime 14.x con PySpark 3.5
```

### `requirements.txt`

```
pandas>=2.0
numpy>=1.24
scikit-learn>=1.3
matplotlib>=3.7
seaborn>=0.12
openmeteo-requests
requests-cache
retry-requests
```

### Variables de entorno

Antes de correr los scripts, configurar las siguientes variables. **No subir estas keys al repositorio.**

```bash
# NASA FIRMS API Key
# Registrarse en: https://firms.modaps.eosdis.nasa.gov/api/
NASA_API_KEY=tu_api_key_aqui
```

---

## Pipeline en Databricks

El pipeline completo se ejecuta en orden dentro de Databricks. Todos los notebooks están diseñados para ser idempotentes (re-ejecutables sin duplicar datos).

```
1. grid_setup.ipynb              → Crea aux_grid_master (800 nodos)
2. extract_nasa.ipynb            → Descarga CSVs de NASA FIRMS (10-15 min)
3. extract_openMeteo.ipynb       → Descarga JSONs de Open-Meteo (~20 horas, automático)
4. ingest_nasa.ipynb             → Bronze: ingesta con Auto Loader
5. transform_nasa.ipynb          → Silver: limpieza + asignación a grilla
6. transform_openMeteo.ipynb     → Silver: desagrupado horario
7. transform_grid.ipynb          → Silver: optimización de grilla
8. transform_gold_table.ipynb    → Gold: join clima + incendios
9. feature_engineering_v3.py    → Gold: features completos (~30-60 min en Databricks free)
```

Una vez completado el paso 9, exportar `gold_dataset_output` como Parquet para entrenamiento local:

```python
# En Databricks
spark.table("fire_risk_project.03_gold.gold_dataset_output") \
    .write.parquet("/dbfs/FileStore/gold_output.parquet")
```

---

## Modelado

El entrenamiento se realiza localmente con `random_forest_v3.py`. El script incluye:

- Split temporal estricto: train 2020–2023, test 2024
- Barrido automático de `class_weight` sobre validación interna (jul–dic 2023)
- Optimización de umbral por F2-score
- Análisis de errores por mes y condición climática
- Visualizaciones de curvas ROC, PR, feature importance

```bash
# Entrenamiento local (requiere gold_output.parquet)
python scripts/random_forest_v3.py
```

---

## Limitaciones y Trabajo Futuro

### Limitaciones actuales

- **Extracción de Open-Meteo:** el tier gratuito de la API Archive tiene un límite de ~30 nodos/hora, lo que hace que la extracción inicial tome 1-2 días. La solución de largo plazo es acceder directamente al bucket S3 público de Open-Meteo.
- **Sin datos de vegetación:** el modelo no incluye índices de vegetación (NDVI) que son predictores relevantes del riesgo de incendio. Pueden incorporarse desde NASA EarthData como join mensual.
- **Recall 54%:** el modelo actual detecta poco más de la mitad de los incendios reales. Parte de la pérdida se concentra en septiembre (mes pico en Córdoba).

### Trabajo futuro

- [ ] Integrar NDVI mensual desde NASA EarthData como feature de vegetación seca
- [ ] Implementar ConvLSTM sobre la grilla 40×20 para capturar patrones espaciales (requiere GPU)
- [ ] Pipeline de inferencia en tiempo real usando el endpoint de forecast de Open-Meteo (16 días)
- [ ] Dashboard Streamlit con mapa de riesgo interactivo por nodo
- [ ] Acceso directo al bucket S3 de Open-Meteo para eliminar limitaciones de la API
- [ ] Análisis específico de septiembre para entender y mejorar la detección en el mes pico

---

## Autor

**Julian Mendelewicz**
[LinkedIn](https://linkedin.com/in/jmendelewicz) | [GitHub](https://github.com/jmendelewicz)

---


---

*Datos de incendios: NASA FIRMS (dominio público). Datos climáticos: Open-Meteo (CC BY 4.0).*
