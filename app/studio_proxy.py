"""Студия на `/app`: UI из статики каталога, API проксируется на studio-api."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

STUDIO_API_URL = os.environ.get("STUDIO_API_URL", "").rstrip("/")
STUDIO_WEB_URL = os.environ.get("STUDIO_WEB_URL", "").rstrip("/")
STUDIO_UI_DIR = Path(__file__).resolve().parent.parent / "static" / "studio"

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

router = APIRouter()
_http: httpx.AsyncClient | None = None


def ui_dir() -> Path | None:
    index = STUDIO_UI_DIR / "index.html"
    return STUDIO_UI_DIR if index.is_file() else None


async def get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=2.0),
            follow_redirects=False,
        )
    return _http


def _forward_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}


async def _proxy(request: Request, url: str, *, stream_forever: bool = False) -> StreamingResponse:
    timeout = httpx.Timeout(None if stream_forever else 15.0, connect=2.0)
    client = await get_http()
    try:
        body = await request.body()
        req = client.build_request(
            request.method,
            url,
            headers=_forward_headers(request),
            content=body or None,
        )
        resp = await client.send(req, stream=True, timeout=timeout)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"studio proxy failed: {exc}") from exc

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}

    async def stream():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        stream(),
        status_code=resp.status_code,
        headers=out_headers,
        media_type=resp.headers.get("content-type"),
    )


@router.api_route("/app/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def studio_api(path: str, request: Request):
    if not STUDIO_API_URL:
        raise HTTPException(status_code=404, detail="Studio API is not enabled")
    qs = f"?{request.url.query}" if request.url.query else ""
    return await _proxy(
        request,
        f"{STUDIO_API_URL}/api/{path}{qs}",
        stream_forever=path.rstrip("/").endswith("/events"),
    )


def _safe_file(root: Path, path: str) -> Path | None:
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def mount_studio_ui(app: FastAPI) -> None:
    root = ui_dir()
    if root is None:
        if STUDIO_WEB_URL:
            app.include_router(_fallback_web_router())
        return

    @app.get("/app", include_in_schema=False)
    def studio_root():
        return RedirectResponse("/app/", status_code=302)

    @app.get("/app/", include_in_schema=False)
    def studio_index():
        return FileResponse(root / "index.html")

    @app.get("/app/{path:path}", include_in_schema=False)
    def studio_spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        found = _safe_file(root, path)
        if found is not None:
            return FileResponse(found)
        return FileResponse(root / "index.html")


def _fallback_web_router() -> APIRouter:
    fallback = APIRouter()

    @fallback.api_route("/app", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    @fallback.api_route("/app/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def studio_web(request: Request, path: str = ""):
        suffix = request.url.path
        qs = f"?{request.url.query}" if request.url.query else ""
        return await _proxy(request, f"{STUDIO_WEB_URL}{suffix}{qs}")

    return fallback
