"""Сервисный API выдачи моделей по запросу.

Внешние клиенты ходят сюда с ключом `Authorization: Bearer mfk_…`.
Витрина и админка этот префикс не используют — у них cookie / HTTP Basic.
"""

from __future__ import annotations

import time
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

import app.db as db
from app import ai_describe, api_keys, billing
from app.model_catalog import get_model_item, search_model_items
from app.paths import API_BASE_URL, OPENAI_API_KEY
from app.tag_normalize import normalize_tag_list
from app.taxonomy import AGE_RATINGS, CATEGORIES, CATEGORY_SET, TAG_FACETS, canonical_category, ratings_up_to

router = APIRouter(prefix="/v1", tags=["service"])

SEMANTIC_CANDIDATES = 300


class SearchBody(BaseModel):
    query: str | None = Field(None, description="Свободный запрос (семантический поиск)")
    name_contains: str | None = None
    category: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list, description="Хотя бы один тег")
    tags_all: list[str] = Field(default_factory=list, description="Все перечисленные теги")
    exclude_tags: list[str] = Field(default_factory=list)
    ext: list[str] = Field(default_factory=list)
    path_prefix: str | None = None
    age_max: str | None = Field(None, description="everyone | teen | mature | adult")
    kid_only: bool = False
    safe: bool = Field(False, description="Исключить 18+ и неклассифицированное")
    only_with_preview: bool = False
    animated: bool | None = None
    rigged: bool | None = None
    sort: str = Field("name")
    limit: int = Field(24, ge=1, le=100)
    offset: int = Field(0, ge=0)
    facets: bool = False


def _client(request: Request) -> dict | None:
    return getattr(request.state, "api_client", None)


def require_scope(scope: str):
    def _dep(request: Request):
        client = _client(request)
        if not api_keys.has_scope(client, scope):
            raise HTTPException(
                status_code=403,
                detail=f"API key missing scope: {scope}",
            )
        billing.enforce(client, "request")
        if scope == "download":
            billing.enforce(client, "download")
        if scope == "search":
            billing.enforce(client, "search")
        allowed, limit, remaining = api_keys.check_rate_limit(client)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": "60", "X-RateLimit-Limit": str(limit)},
            )
        request.state.rate_limit = (limit, remaining)
        if client is not None:
            db.touch_api_key(int(client["id"]), time.time())
            billing.meter(client, requests=1)
        return client

    return _dep


def _resolve_base_url(request: Request, base_url: str | None) -> str | None:
    if base_url and base_url.strip():
        return base_url.strip().rstrip("/")
    if API_BASE_URL:
        return API_BASE_URL
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host:
        return None
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme}://{host}".rstrip("/")


def _abs(base: str | None, path: str | None) -> str | None:
    if not base or not path:
        return None
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base.rstrip('/')}{path}"


def _public_item(item: dict, base_url: str | None) -> dict:
    """Ссылки на выдачу файлов ведут в /v1, а не во внутренние эндпоинты витрины."""
    path = item["path"]
    q = quote(path, safe="")
    preview = f"/v1/preview?path={q}" if item.get("preview_url") else None
    download = f"/v1/file?path={q}"
    blend = item.get("blend_path")
    out = {
        "name": item.get("name"),
        "path": path,
        "ext": item.get("ext"),
        "size": item.get("size"),
        "modified": item.get("modified"),
        "category": item.get("category"),
        "description": item.get("description"),
        "tags": item.get("tag_list") or [],
        "tag_facets": item.get("tag_facets"),
        "age_rating": item.get("age_rating"),
        "kid_friendly": item.get("kid_friendly"),
        "nsfw": item.get("nsfw"),
        "animated": item.get("animated"),
        "rigged": item.get("rigged"),
        "geometry": item.get("geometry"),
        "preview_url": preview,
        "download_url": download,
        "blend_path": blend,
        "blend_download_url": f"/v1/file?path={quote(blend, safe='')}" if blend else None,
        "content_hash": item.get("content_hash"),
    }
    if base_url:
        out["preview_url_abs"] = _abs(base_url, preview)
        out["download_url_abs"] = _abs(base_url, download)
        out["blend_download_url_abs"] = _abs(base_url, out["blend_download_url"])
    return out


def _sql_filters(body: SearchBody) -> dict:
    return {
        "tags_any": normalize_tag_list(body.tags),
        "tags_all": normalize_tag_list(body.tags_all),
        "exclude_tags": normalize_tag_list(body.exclude_tags),
        "categories": [c for c in (canonical_category(x) for x in body.category) if c],
        "ext": [x.lower().lstrip(".") for x in body.ext if str(x).strip()],
        "name_contains": (body.name_contains or "").strip() or None,
        "path_prefix": (body.path_prefix or "").strip().replace("\\", "/") or None,
        "age_ratings": ratings_up_to(body.age_max),
        "kid_only": body.kid_only,
        "exclude_nsfw": body.safe,
        "only_with_preview": body.only_with_preview,
        "animated": body.animated,
        "rigged": body.rigged,
    }


def _run_search(body: SearchBody, base_url: str | None) -> dict:
    filters = _sql_filters(body)
    query = (body.query or "").strip()
    if query:
        if not OPENAI_API_KEY:
            raise HTTPException(
                status_code=400,
                detail="Semantic search is not configured on this server",
            )
        semantic = ai_describe.semantic_search(
            query,
            limit=SEMANTIC_CANDIDATES,
            relevance_ratio=None,
            filters={
                "categories": filters["categories"],
                "tags_any": filters["tags_any"],
                "tags_all": filters["tags_all"],
                "age_ratings": filters["age_ratings"],
                "ext": filters["ext"],
                "exclude_nsfw": body.safe,
                "kid_only": body.kid_only,
                "animated": body.animated,
            },
        )
        if not semantic.get("ok"):
            raise HTTPException(status_code=400, detail=semantic.get("error", "Search failed"))
        ranked = [r["path"] for r in semantic["results"]]
        payload = search_model_items(
            limit=body.limit,
            offset=body.offset,
            sort="path",
            with_facets=body.facets,
            base_url=None,
            paths_subset=ranked,
            **filters,
        )
        order = {path: i for i, path in enumerate(ranked)}
        payload["items"].sort(key=lambda item: order.get(item["path"], len(order)))
        payload["query"] = query
    else:
        payload = search_model_items(
            limit=body.limit,
            offset=body.offset,
            sort=body.sort,
            with_facets=body.facets,
            base_url=None,
            **filters,
        )
    payload["items"] = [_public_item(item, base_url) for item in payload["items"]]
    return payload


def _csv_list(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


@router.get("")
@router.get("/")
def service_root(request: Request, _: Annotated[object, Depends(require_scope("catalog"))]):
    """Краткая справка по сервисному API."""
    client = _client(request)
    return {
        "ok": True,
        "service": "modelfolder",
        "version": "v1",
        "auth": "Authorization: Bearer mfk_…  or  X-API-Key: mfk_…",
        "scopes": (client or {}).get("scopes") or list(api_keys.ALL_SCOPES),
        "endpoints": {
            "GET /v1/models": "Поиск и фильтры (пагинация)",
            "POST /v1/search": "Тот же поиск телом JSON, включая семантический query",
            "GET /v1/model?path=": "Карточка одной модели",
            "GET /v1/file?path=": "Скачать файл модели",
            "GET /v1/preview?path=": "Превью (PNG/картинка)",
            "GET /v1/similar?path=": "Похожие модели",
            "GET /v1/categories": "Категории и фасеты тегов",
            "GET /v1/tags": "Счётчики тегов",
            "GET /v1/me": "Информация о ключе и тарифе",
        "GET /v1/billing": "Подписка, квоты и расход за период",
        },
    }


@router.get("/me")
def me(request: Request, _: Annotated[object, Depends(require_scope("catalog"))]):
    client = _client(request)
    if client is None:
        return {
            "auth": "session",
            "scopes": list(api_keys.ALL_SCOPES),
            "rate_limit_per_min": None,
            "billing": None,
        }
    return {
        "auth": "api_key",
        "id": client["id"],
        "name": client["name"],
        "key_prefix": client["key_prefix"],
        "scopes": client["scopes"],
        "rate_limit_per_min": client["rate_limit_per_min"],
        "request_count": client["request_count"],
        "created_at": client["created_at"],
        "last_used_at": client["last_used_at"],
        "customer_id": client.get("customer_id"),
        "billing": billing.public_billing(client),
    }


@router.get("/billing")
def billing_status(request: Request, _: Annotated[object, Depends(require_scope("catalog"))]):
    """Текущий тариф, статус подписки и расход квот за период."""
    return billing.public_billing(_client(request)) or {
        "mode": "session",
        "note": "Запрос без API-ключа — биллинг не применяется",
    }


@router.get("/categories")
def categories(_: Annotated[object, Depends(require_scope("catalog"))]):
    counts = {row["category"]: row["count"] for row in db.list_category_counts()}
    return {
        "categories": [
            {"category": name, "count": counts.get(name, 0)} for name in CATEGORIES
        ]
        + [
            {"category": name, "count": count}
            for name, count in counts.items()
            if name not in CATEGORY_SET
        ],
        "age_ratings": list(AGE_RATINGS),
        "tag_facets": {facet: list(tags) for facet, tags in TAG_FACETS.items()},
    }


@router.get("/tags")
def tags(_: Annotated[object, Depends(require_scope("catalog"))]):
    return {"tags": db.list_tag_counts()}


@router.get("/models")
def list_models(
    request: Request,
    _: Annotated[object, Depends(require_scope("catalog"))],
    q: str | None = Query(None, description="Семантический запрос на естественном языке"),
    name: str | None = Query(None, description="Подстрока в имени или пути"),
    category: list[str] = Query(default=[]),
    tags: list[str] = Query(default=[]),
    tags_all: list[str] = Query(default=[]),
    exclude_tags: list[str] = Query(default=[]),
    ext: list[str] = Query(default=[]),
    path_prefix: str | None = None,
    age_max: str | None = None,
    kid_only: bool = False,
    safe: bool = False,
    only_with_preview: bool = False,
    animated: bool | None = None,
    rigged: bool | None = None,
    sort: str = "name",
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
    facets: bool = False,
    base_url: str | None = None,
):
    if q and not api_keys.has_scope(_client(request), "search"):
        raise HTTPException(status_code=403, detail="API key missing scope: search")
    client = _client(request)
    if q:
        billing.enforce(client, "search")
    limit = billing.cap_page_size(client, limit)
    body = SearchBody(
        query=q,
        name_contains=name,
        category=_csv_list(category),
        tags=_csv_list(tags),
        tags_all=_csv_list(tags_all),
        exclude_tags=_csv_list(exclude_tags),
        ext=_csv_list(ext),
        path_prefix=path_prefix,
        age_max=age_max,
        kid_only=kid_only,
        safe=safe,
        only_with_preview=only_with_preview,
        animated=animated,
        rigged=rigged,
        sort=sort,
        limit=limit,
        offset=offset,
        facets=facets,
    )
    payload = _run_search(body, _resolve_base_url(request, base_url))
    if q:
        billing.meter(client, searches=1)
    return payload


@router.post("/search")
def search(
    body: SearchBody,
    request: Request,
    _: Annotated[object, Depends(require_scope("catalog"))],
    base_url: str | None = None,
):
    if (body.query or "").strip() and not api_keys.has_scope(_client(request), "search"):
        raise HTTPException(status_code=403, detail="API key missing scope: search")
    client = _client(request)
    if (body.query or "").strip():
        billing.enforce(client, "search")
    body.limit = billing.cap_page_size(client, body.limit)
    payload = _run_search(body, _resolve_base_url(request, base_url))
    if (body.query or "").strip():
        billing.meter(client, searches=1)
    return payload


@router.get("/model")
def get_model(
    request: Request,
    _: Annotated[object, Depends(require_scope("catalog"))],
    path: str = Query(..., description="Путь модели в каталоге"),
    base_url: str | None = None,
):
    item = get_model_item(path)
    if item is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return _public_item(item, _resolve_base_url(request, base_url))


@router.get("/similar")
def similar(
    request: Request,
    _: Annotated[object, Depends(require_scope("search"))],
    path: str = Query(..., description="Путь модели, для которой искать похожие"),
    limit: int = Query(12, ge=1, le=50),
    base_url: str | None = None,
):
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=400, detail="Semantic search is not configured")
    billing.meter(_client(request), searches=1)
    result = ai_describe.similar_to_path(path, limit=limit)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Search failed"))
    paths = [r["path"] for r in result.get("results") or []]
    scores = {r["path"]: r.get("score") for r in result.get("results") or []}
    rows = db.get_assets_bulk(paths)
    tags_map = db.get_tags_bulk(paths)
    items = []
    from app.model_catalog import build_item

    resolved = _resolve_base_url(request, base_url)
    for p in paths:
        row = rows.get(p)
        if not row:
            continue
        item = _public_item(build_item(row, tags_map.get(p, [])), resolved)
        item["score"] = scores.get(p)
        items.append(item)
    return {"ok": True, "path": path, "items": items}


@router.get("/file")
def download_file(
    request: Request,
    path: str = Query(..., description="Путь модели или сопутствующего файла"),
    _: Annotated[object, Depends(require_scope("download"))] = None,
):
    from app.main import _serve_asset

    client = _client(request)
    row = db.get_assets_bulk([path]).get(path)
    size = int(row["size"]) if row and row.get("size") is not None else 0
    billing.enforce_bytes(client, size)
    billing.meter(client, downloads=1, bytes_=size)
    return _serve_asset(path, as_attachment=True)


@router.get("/preview")
def preview(
    path: str = Query(..., description="Путь модели в каталоге"),
    _: Annotated[object, Depends(require_scope("catalog"))] = None,
):
    from app.main import serve_preview

    return serve_preview(path=path)
