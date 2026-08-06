# AlertaFuego — Predictions API

Expone las predicciones diarias de riesgo de incendio (producidas por el job de
inferencia en Databricks) al frontend. **No corre el modelo**: relaya el
`predictions_ui.json` calibrado que genera `cloud_inference_engine.py`.

```
Databricks job → /Volumes/fire_risk_project/03_gold/outputs/predictions_ui.json
   → esta API lo descarga (Databricks SDK, auth de ~/.databrickscfg)
   → lo cachea localmente → lo sirve por HTTP (CORS) al frontend
```

`risk_level` es una **probabilidad calibrada** (0–100), post-fix R1 (Platt).

## Endpoints
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | estado + si hay datos cargados |
| GET | `/predictions` | payload completo (todos los nodos) |
| GET | `/predictions/meta` | solo metadata (barato) |
| GET | `/predictions/{cell_id}` | un nodo |
| POST | `/refresh` | re-descarga desde el Volume de Databricks |

## Correr local
```bash
cd 05_api
pip install -r requirements.txt
# Sin Databricks aún → servir el sample:
AUTO_REFRESH_ON_START=0 PREDICTIONS_CACHE=predictions_ui.sample.json uvicorn app:app --port 8000
# Con Databricks (requiere ~/.databrickscfg):
uvicorn app:app --port 8000   # descarga del Volume al arrancar
```

## Config (env vars)
| Var | Default |
|---|---|
| `DATABRICKS_VOLUME_PATH` | `/Volumes/fire_risk_project/03_gold/outputs/predictions_ui.json` |
| `PREDICTIONS_CACHE` | `./predictions_ui.cache.json` |
| `ALLOWED_ORIGINS` | `*` (poner el dominio del frontend en prod) |
| `AUTO_REFRESH_ON_START` | `1` |

## Deploy en Hugging Face Space (Docker SDK)
1. Crear un Space tipo **Docker**.
2. Subir `app.py`, `requirements.txt`, `Dockerfile`, `predictions_ui.sample.json`.
3. Para datos reales: setear los secrets `DATABRICKS_HOST` y `DATABRICKS_TOKEN`
   en el Space y `AUTO_REFRESH_ON_START=1`. El SDK los toma de las env vars.
4. El Space corre en el puerto 7860 (ya configurado en el Dockerfile).
