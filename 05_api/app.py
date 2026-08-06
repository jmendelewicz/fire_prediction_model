
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

_state = {"payload": None, "source": None, "loaded_at": None}

def _download_from_databricks() -> dict:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    resp = w.files.download(VOLUME_PATH)
    raw = resp.contents.read()
    return json.loads(raw)

def _load(refresh: bool) -> dict:
    if refresh:
        try:
            payload = _download_from_databricks()
            CACHE_PATH.write_text(json.dumps(payload), encoding="utf-8")
            _state.update(payload=payload, source="databricks", loaded_at=time.time())
            return payload
        except Exception as e:
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
