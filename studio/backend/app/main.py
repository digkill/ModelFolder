from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.db import Store, migrate
from app.hub import Hub
from app.game import GAMES_DIR
from app.orchestrator import Engine
from app.providers import AUDIO_DIR, Clients
from app.registry import MODELS


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    pool = AsyncConnectionPool(conninfo=settings.dsn, min_size=1, max_size=10, open=False)
    await pool.open()
    await migrate(pool)
    store = Store(pool)
    hub = Hub()
    clients = Clients(settings)
    app.state.settings = settings
    app.state.pool = pool
    app.state.store = store
    app.state.hub = hub
    app.state.clients = clients
    app.state.orch = Engine(store, clients, hub)
    app.state.tasks = set()
    yield
    await clients.aclose()
    await pool.close()


app = FastAPI(title="ModelFolder Studio", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _store(request: Request) -> Store:
    return request.app.state.store


def _clients(request: Request) -> Clients:
    return request.app.state.clients


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _track(request: Request, task: asyncio.Task) -> None:
    tasks: set[asyncio.Task] = request.app.state.tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _health_payload(request: Request) -> dict:
    cfg = _settings(request)
    clients = _clients(request)
    return {
        "ok": True,
        "service": "studio",
        "orchestrator": clients.pick_orchestrator(),
        "catalog_url": cfg.catalog,
        "keys": {
            "openai": bool(cfg.openai_api_key),
            "anthropic": bool(cfg.anthropic_key),
            "grok": bool(cfg.grok_key),
            "kie": bool(cfg.kie_key),
            "meshy": bool(cfg.meshy_api_key),
        },
    }


@app.get("/health")
@app.get("/api/v1/health")
async def health(request: Request):
    return _health_payload(request)


@app.get("/api/v1/games/{name}")
def studio_game(name: str):
    stem = name.removesuffix(".html")
    if "/" in stem or "\\" in stem:
        raise HTTPException(404, "not found")
    path = GAMES_DIR / f"{stem}.html"
    if not path.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(
        path,
        media_type="text/html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/v1/audio/{name}")
def studio_audio(name: str):
    if "/" in name or "\\" in name:
        raise HTTPException(404, "not found")
    suffix = "".join(Path(name).suffixes).lower() or ".mp3"
    if suffix not in {".mp3", ".wav", ".ogg", ".m4a"}:
        raise HTTPException(404, "not found")
    path = AUDIO_DIR / name
    if not path.is_file():
        raise HTTPException(404, "not found")
    media = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
    }[suffix]
    return FileResponse(path, media_type=media, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/v1/models")
async def list_models():
    return {"models": MODELS}


@app.get("/api/v1/projects")
async def list_projects(request: Request):
    return {"items": await _store(request).list_projects()}


class CreateProjectBody(BaseModel):
    prompt: str
    platform: str = "web"
    title: str = ""


class ChatBody(BaseModel):
    message: str
    model: str = ""


@app.post("/api/v1/projects", status_code=202)
async def create_project(request: Request, body: CreateProjectBody):
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    platform = body.platform.strip().lower()
    if platform not in {"web", "mobile", "desktop"}:
        platform = "web"
    title = body.title.strip() or "Новая игра"
    store = _store(request)
    project = await store.create_project(title, prompt, platform)
    await store.add_message(project["id"], "user", prompt)
    task = asyncio.create_task(request.app.state.orch.run(project))
    _track(request, task)
    return project


@app.get("/api/v1/projects/{project_id}")
async def get_project(request: Request, project_id: str):
    project = await _store(request).get_project(project_id)
    if not project:
        raise HTTPException(404, "not found")
    return _clients(request).publicize_project(project)


@app.post("/api/v1/projects/{project_id}/rerun", status_code=202)
async def rerun_project(request: Request, project_id: str):
    store = _store(request)
    project = await store.get_project(project_id)
    if not project:
        raise HTTPException(404, "not found")
    task = asyncio.create_task(request.app.state.orch.rerun_media(project))
    _track(request, task)
    return {"ok": True, "id": project_id, "status": "generating"}


@app.post("/api/v1/projects/{project_id}/review", status_code=202)
async def review_project(request: Request, project_id: str):
    store = _store(request)
    project = await store.get_project(project_id)
    if not project:
        raise HTTPException(404, "not found")
    task = asyncio.create_task(request.app.state.orch.review_game(project))
    _track(request, task)
    return {"ok": True, "id": project_id, "status": "reviewing"}


@app.get("/api/v1/projects/{project_id}/events")
async def events(request: Request, project_id: str):
    hub: Hub = request.app.state.hub
    queue = hub.subscribe(project_id)

    async def stream():
        try:
            yield 'event: hello\ndata: {"ok":true}\n\n'
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            hub.unsubscribe(project_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/v1/projects/{project_id}/chat")
async def chat(request: Request, project_id: str, body: ChatBody):
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "message required")
    store = _store(request)
    project = await store.get_project(project_id)
    if not project:
        raise HTTPException(404, "not found")
    await store.add_message(project_id, "user", message)
    request.app.state.hub.publish(project_id, "message", {"role": "user", "content": message})
    model = body.model.strip() or _clients(request).pick_orchestrator()
    orch: Engine = request.app.state.orch
    try:
        text, used = await orch.chat(project, message, model)
    except Exception as exc:
        text = (
            "Контекст проекта сохранён (план, модели, чат). "
            f"Оркестратор сейчас не ответил: {exc}"
        )
        used = "error"
    saved = await store.add_message(project_id, "assistant", text)
    request.app.state.hub.publish(project_id, "message", saved)
    return {"message": saved, "model": used}


@app.get("/api/v1/catalog/search")
async def catalog_search(request: Request, q: str = Query(..., min_length=1)):
    try:
        items = await _clients(request).search_catalog(q.strip(), 16)
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"items": items}
