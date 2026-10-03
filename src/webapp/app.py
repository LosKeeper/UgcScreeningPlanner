#!/usr/bin/env python3

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

# Charger les variables d'environnement depuis .env
load_dotenv()

from modules.stats_db import QueryError, describe_schema, run_readonly_query  # noqa: E402

from .config_store import ConfigError, get_schedule, load_config, resolve_path, save_config  # noqa: E402
from .runner import PipelineRunner, output_json_path  # noqa: E402
from .scheduler import Scheduler, next_run  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"

runner = PipelineRunner()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = Scheduler(runner)
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="UGC Screening Planner", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def get_config():
    config = load_config()
    config["schedule"] = get_schedule(config)
    return config


@app.put("/api/config")
def put_config(payload: dict = Body(...)):
    try:
        return save_config(payload)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/run", status_code=202)
def start_run():
    if not runner.start("manual"):
        raise HTTPException(
            status_code=409, detail="Un lancement est déjà en cours")
    return runner.snapshot()


@app.get("/api/run")
def get_run(since: int = 0):
    scheduled = next_run(get_schedule(load_config()), datetime.now())
    return {
        **runner.snapshot(since),
        "next_scheduled": scheduled.isoformat() if scheduled else None,
    }


@app.get("/api/result")
def get_result():
    path = output_json_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False}
    return {
        "available": True,
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        "data": data,
    }


@app.get("/api/result/ics")
def get_result_ics():
    path = resolve_path(os.getenv(
        "GOOGLE_CALENDAR_PLANNING_ICS", "planned_screenings.ics"))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Aucun fichier ICS généré")
    return FileResponse(path, media_type="text/calendar", filename=path.name)


@app.post("/api/sql")
def post_sql(payload: dict = Body(...)):
    query = payload.get("query")
    if not isinstance(query, str):
        raise HTTPException(status_code=422, detail="Champ « query » manquant")
    try:
        return run_readonly_query(query)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except QueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/sql/schema")
def get_sql_schema():
    try:
        return describe_schema()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
