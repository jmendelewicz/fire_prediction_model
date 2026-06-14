"""
AlertaFuego — Predictions API
================================================================================
Serves the daily fire-risk predictions produced by the Databricks inference job
(`cloud_inference_engine.py` → `predictions_ui.json` in a Unity Catalog Volume)
to the frontend.

Data flow (no local computation):
    Databricks job → /Volumes/.../outputs/predictions_ui.json
        → this API downloads it via the Databricks SDK (same ~/.databrickscfg auth)
        → caches it locally → serves it to the frontend over HTTP (CORS enabled).

The API never runs the model. It only relays the precomputed, calibrated
predictions. `risk_level` in the payload is a CALIBRATED probability (0-100),
post-fix R1.

Endpoints
    GET  /                     service info
    GET  /health               liveness + whether predictions are loaded
    GET  /predictions          full payload (all nodes)
    GET  /predictions/meta     metadata only (no nodes) — cheap
    GET  /predictions/{cell}   single cell
    POST /refresh              re-download from the Databricks Volume

Run locally:
    pip install -r 05_api/requirements.txt
    uvicorn app:app --reload --port 8000        # from inside 05_api/

Config via env vars (all optional, sane defaults):
    DATABRICKS_VOLUME_PATH   /Volumes/fire_risk_project/03_gold/outputs/predictions_ui.json
    PREDICTIONS_CACHE        ./predictions_ui.cache.json
    ALLOWED_ORIGINS          comma-separated; default "*"
    AUTO_REFRESH_ON_START    "1" to pull from Databricks at startup (default "1")
"""

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

VOLUME_PATH = os.getenv(
    "DATABRICKS_VOLUME_PATH",
    "/Volumes/fire_risk_project/03_gold/outputs/predictions_ui.json",
)
CACHE_PATH = Path(os.getenv("PREDICTIONS_CACHE",
                            str(Path(__file__).parent / "predictions_ui.cache.json")))
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
AUTO_REFRESH = os.getenv("AUTO_REFRESH_ON_START", "1") == "1"

app = FastAPI(title="AlertaFuego Predictions API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# In-memory state
_state = {"payload": None, "source": None, "loaded_at": None}


def _download_from_databricks() -> dict:
    """Pull predictions_ui.json from the Unity Catalog Volume via the SDK.

    Uses ~/.databrickscfg (DEFAULT profile) or DATABRICKS_HOST/TOKEN env vars.
    Raises on any failure so the caller can fall back to cache.
    """
    from databricks.sdk import WorkspaceClient  # imported lazily

    w = WorkspaceClient()
    resp = w.files.download(VOLUME_PATH)
    raw = resp.contents.read()
    return json.loads(raw)


def _load(refresh: bool) -> dict:
    """Load predictions: try Databricks (if refresh), else local cache."""
    if refresh:
        try:
            payload = _download_from_databricks()
            CACHE_PATH.write_text(json.dumps(payload), encoding="utf-8")
            _state.update(payload=payload, source="databricks", loaded_at=time.time())
            return payload
        except Exception as e:  # noqa: BLE001 — fall back to cache, report later
            _state["last_error"] = f"{type(e).__name__}: {e}"

    if CACHE_PATH.exists():
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        _state.update(payload=payload, source="cache", loaded_at=time.time())
        return payload

    _state.update(payload=None, source=None, loaded_at=None)
    return None


@app.on_event("startup")
def _startup():
    _load(refresh=AUTO_REFRESH)


@app.get("/")
def root():
    return {
        "service": "AlertaFuego Predictions API",
        "version": "1.0",
        "volume_path": VOLUME_PATH,
        "endpoints": ["/health", "/predictions", "/predictions/meta",
                      "/predictions/{cell_id}", "/refresh (POST)"],
    }


@app.get("/health")
def health():
    p = _state["payload"]
    return {
        "status": "ok" if p else "no_data",
        "source": _state["source"],
        "loaded_at": _state["loaded_at"],
        "n_nodes": (p.get("n_nodes") if p else 0),
        "model_version": (p.get("model_version") if p else None),
        "calibration": (p.get("calibration") if p else None),
        "last_error": _state.get("last_error"),
    }


@app.get("/predictions")
def predictions():
    p = _state["payload"]
    if not p:
        raise HTTPException(503, "No predictions loaded. POST /refresh once "
                                 "Databricks has produced predictions_ui.json.")
    return p


@app.get("/predictions/meta")
def predictions_meta():
    p = _state["payload"]
    if not p:
        raise HTTPException(503, "No predictions loaded.")
    return {k: v for k, v in p.items() if k != "nodes"}


@app.get("/predictions/{cell_id}")
def prediction_cell(cell_id: str):
    p = _state["payload"]
    if not p:
        raise HTTPException(503, "No predictions loaded.")
    for node in p.get("nodes", []):
        if node["cell_id"] == cell_id:
            return node
    raise HTTPException(404, f"cell_id {cell_id} not found")


@app.post("/refresh")
def refresh():
    payload = _load(refresh=True)
    if not payload:
        raise HTTPException(502, f"Could not load predictions. "
                                 f"last_error={_state.get('last_error')}")
    return {"refreshed": True, "source": _state["source"],
            "n_nodes": payload.get("n_nodes")}
