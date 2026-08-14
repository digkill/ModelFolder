"""Прокси облачной студии на `/app`, чтобы Traefik мог оставить публичный вход на каталог."""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

STUDIO_WEB_URL = os.environ.get("STUDIO_WEB_URL", "").rstrip("/")
STUDIO_API_URL = os.environ.get("STUDIO_API_URL", "").rstrip("/")

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


def enabled() -> bool:
    return bool(STUDIO_WEB_URL or STUDIO_API_URL)


def _forward_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}


async def _proxy(request: Request, url: str, *, stream_forever: bool = False) -> StreamingResponse:
    timeout = httpx.Timeout(None if stream_forever else 30.0, connect=5.0)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        body = await request.body()
        req = client.build_request(
            request.method,
            url,
            headers=_forward_headers(request),
            content=body or None,
        )
        resp = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"studio proxy failed: {exc}") from exc

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}

    async def stream():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

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


@router.api_route("/app", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@router.api_route("/app/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def studio_web(request: Request, path: str = ""):
    if not STUDIO_WEB_URL:
        raise HTTPException(status_code=404, detail="Studio UI is not enabled")
    suffix = request.url.path
    qs = f"?{request.url.query}" if request.url.query else ""
    return await _proxy(request, f"{STUDIO_WEB_URL}{suffix}{qs}")
