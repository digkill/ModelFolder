import mimetypes
import time
import zipfile
from html import escape
from urllib.parse import quote
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import app.db as db
from app import ai_describe, auth, storage, vector_store
from app.launch_groups import GroupFilters, get_launch_group, list_launch_groups
from app.model_catalog import filters_from_query, list_model_items, search_model_items
from app.paths import (
    API_BASE_URL,
    LAUNCH_GROUPS_PATH,
    MODELS_ROOT,
    OPENAI_API_KEY,
    OPENAI_EMBED_MODEL,
    PREVIEW_BATCH_PER_CYCLE,
    PREVIEW_ENGINE,
    PREVIEW_MAX_MEMORY_MB,
    PREVIEW_PIXEL_SIZE,
    PREVIEW_SUBPROCESS_TIMEOUT_SEC,
    PREVIEWS_DIR,
    QDRANT_COLLECTION,
)
from app.scanner import run_scan_cycle, start_background_scanner, stop_background_scanner
from app.tag_normalize import normalize_tag_list
from app.taxonomy import (
    AGE_RATINGS,
    CATEGORIES,
    CATEGORY_SET,
    TAG_FACETS,
    canonical_category,
    ratings_up_to,
)
from app.vision_tags import run_auto_tag_batch
from app.vpath import ZIP_SEP, is_safe_zip_member, split_vpath

static_dir = Path(__file__).resolve().parent.parent / "static"


class AppendTagsBody(BaseModel):
    path: str = Field(..., description="Путь модели как в каталоге")
    tags: list[str] = Field(default_factory=list)


class SemanticSearchBody(BaseModel):
    query: str = Field(..., description="Запрос на естественном языке")
    limit: int = Field(12, ge=1, le=50)
    category: list[str] = Field(default_factory=list, description="Категории каталога")
    tags: list[str] = Field(default_factory=list, description="Хотя бы один тег")
    tags_all: list[str] = Field(default_factory=list, description="Все перечисленные теги")
    ext: list[str] = Field(default_factory=list)
    age_max: str | None = Field(None, description="everyone | teen | mature | adult")
    kid_only: bool = Field(False, description="Только пригодные для детской игры")
    safe: bool = Field(False, description="Исключить 18+ и неклассифицированное")


class CatalogSearchBody(BaseModel):
    """Фасетный поиск по каталогу; при заданном `query` подмешивается семантика."""

    query: str | None = Field(None, description="Свободный запрос (семантический поиск)")
    name_contains: str | None = Field(None, description="Подстрока в имени или пути")
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
    animated: bool | None = Field(
        None, description="true — только анимированные, false — только статичные"
    )
    rigged: bool | None = Field(None, description="Наличие скелета (риг)")
    sort: str = Field("name", description="name | newest | size | complexity | ...")
    limit: int = Field(60, ge=1, le=200)
    offset: int = Field(0, ge=0)
    facets: bool = Field(False, description="Вернуть счётчики категорий/тегов/форматов")


class ModelQueryBody(BaseModel):
    tags: list[str] = Field(default_factory=list, description="Хотя бы один тег")
    tags_all: list[str] = Field(default_factory=list, description="Все перечисленные теги")
    path_prefix: str | None = Field(None, description="Путь начинается с префикса")
    ext: list[str] = Field(default_factory=list, description="Расширения: glb, fbx, …")
    name_contains: str | None = Field(None, description="Подстрока в имени или пути")


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


def _locate_asset(file_path: str) -> tuple[str, str | None]:
    """
    Возвращает (key, None) для обычного файла или (zip_key, member) для файла в архиве.
    Виртуальный путь: folder/archive.zip::inner/model.glb
    """
    if ZIP_SEP not in file_path:
        try:
            return storage.safe_key(file_path), None
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Invalid path") from e
    try:
        left, right = split_vpath(file_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid path") from e
    if not is_safe_zip_member(right):
        raise HTTPException(status_code=403, detail="Forbidden zip member")
    try:
        key = storage.safe_key(left)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid path") from e
    if not key.lower().endswith(".zip"):
        raise HTTPException(status_code=404, detail="Zip not found")
    return key, right


def _serve_asset(file_path: str, *, as_attachment: bool):
    key, member = _locate_asset(file_path)
    if member is not None:
        try:
            zip_local = storage.local_path(key)
        except (FileNotFoundError, ValueError, OSError) as e:
            raise HTTPException(status_code=404, detail="Zip not found") from e
        return _stream_zip_member(
            zip_local,
            member,
            as_attachment=as_attachment,
            fallback_name=Path(member).name,
        )
    if not storage.object_exists(key):
        raise HTTPException(status_code=404, detail="File not found")
    filename = Path(key).name
    if storage.is_s3():
        media = "application/octet-stream" if as_attachment else storage.guess_media_type(key)
        headers: dict[str, str] = {}
        if as_attachment:
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return StreamingResponse(storage.open_stream(key), media_type=media, headers=headers)
    local = storage.local_path(key)
    if as_attachment:
        return FileResponse(local, filename=filename, media_type="application/octet-stream")
    return FileResponse(local)


def _zip_member_stream(zip_path: Path, member: str):
    def gen():
        with zipfile.ZipFile(zip_path, "r") as zf:
            with zf.open(member, "r") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

    return gen()


def _stream_zip_member(
    zip_path: Path,
    member: str,
    *,
    as_attachment: bool,
    fallback_name: str,
) -> StreamingResponse:
    media = mimetypes.guess_type(member)[0] or "application/octet-stream"
    headers: dict[str, str] = {}
    if as_attachment:
        headers["Content-Disposition"] = f'attachment; filename="{fallback_name}"'
    return StreamingResponse(
        _zip_member_stream(zip_path, member),
        media_type=media,
        headers=headers,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    if not storage.is_s3():
        MODELS_ROOT.mkdir(parents=True, exist_ok=True)
    db.init_db()
    start_background_scanner()
    yield
    stop_background_scanner()


app = FastAPI(title="Models gallery", lifespan=lifespan)


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Закрывает каталог целиком: и UI, и API, и отдачу файлов моделей."""
    if not auth.is_enabled() or auth.is_public_path(request.url.path):
        return await call_next(request)
    if auth.authenticated_user(request):
        return await call_next(request)
    # Браузеру показываем форму, программному клиенту — честный 401.
    if "text/html" in request.headers.get("accept", ""):
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
    return JSONResponse(
        {"detail": "Authentication required"},
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="ModelFolder"'},
    )


app.add_middleware(
    CORSMiddleware,
    # Авторизация по cookie: с allow_origins="*" браузер запретит credentials,
    # поэтому при включённом логине пускаем только собственный источник.
    allow_origins=["*"] if not auth.is_enabled() else [],
    allow_credentials=auth.is_enabled(),
    allow_methods=["*"],
    allow_headers=["*"],
)


def _login_page(next_url: str, error: str | None = None) -> HTMLResponse:
    page = static_dir / "login.html"
    if not page.is_file():
        return HTMLResponse("<p>Missing static/login.html</p>", status_code=500)
    html = page.read_text(encoding="utf-8")
    html = html.replace("__NEXT__", escape(next_url or "/", quote=True))
    if error:
        html = html.replace("<!--ERROR-->", f'<div class="error">{escape(error)}</div>')
    return HTMLResponse(html, status_code=200 if not error else 401)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = Query("/")) -> HTMLResponse:
    if not auth.is_enabled() or auth.authenticated_user(request):
        return RedirectResponse(next or "/", status_code=303)
    return _login_page(next)


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    next_url = str(form.get("next") or "/")
    if not next_url.startswith("/"):
        next_url = "/"  # не даём увести пользователя на чужой сайт через ?next=
    if not auth.verify_credentials(username, password):
        return _login_page(next_url, error="Неверный логин или пароль")
    response = RedirectResponse(next_url, status_code=303)
    forwarded_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    auth.set_session_cookie(response, username, secure=forwarded_proto == "https")
    return response


@app.get("/logout")
@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session_cookie(response)
    return response

if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

if PREVIEWS_DIR.is_dir():
    app.mount("/previews", StaticFiles(directory=str(PREVIEWS_DIR)), name="previews")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    index_path = static_dir / "index.html"
    if not index_path.is_file():
        return HTMLResponse("<p>Missing static/index.html</p>", status_code=500)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "storage": storage.health(),
        "models_dir": str(MODELS_ROOT),
        "exists": MODELS_ROOT.is_dir(),
        "data_dir": str(PREVIEWS_DIR.parent),
        "previews_dir": str(PREVIEWS_DIR),
        "zip_virtual_sep": ZIP_SEP,
        "preview_batch_per_cycle": PREVIEW_BATCH_PER_CYCLE,
        "preview_subprocess_timeout_sec": PREVIEW_SUBPROCESS_TIMEOUT_SEC,
        "preview_pixel_size": PREVIEW_PIXEL_SIZE,
        "preview_engine": PREVIEW_ENGINE,
        "preview_max_memory_mb": PREVIEW_MAX_MEMORY_MB,
        "openai_tagging_configured": bool(OPENAI_API_KEY),
        "vector_collection": QDRANT_COLLECTION,
        "embed_model": OPENAI_EMBED_MODEL,
        "launch_groups_path": str(LAUNCH_GROUPS_PATH),
        "launch_groups_configured": LAUNCH_GROUPS_PATH.is_file(),
        "api_base_url": API_BASE_URL,
    }


@app.get("/api/status")
def status() -> dict:
    meta = db.get_catalog_meta()
    rows = db.list_assets()
    return {
        **meta,
        "asset_count": len(rows),
    }


@app.post("/api/scan")
def scan_now(background_tasks: BackgroundTasks) -> dict:
    background_tasks.add_task(run_scan_cycle)
    return {"ok": True, "message": "Scan scheduled"}


@app.post("/api/retry-failed-previews")
def retry_failed_previews(background_tasks: BackgroundTasks) -> dict:
    def _retry_and_process() -> None:
        with db.write_transaction() as conn:
            rows = conn.execute(
                "SELECT path, preview_file FROM assets WHERE preview_status = 'error' AND ext IN ('glb','gltf','fbx')"
            ).fetchall()
            for _path, prev in rows:
                db.unlink_preview_file(prev)
            conn.execute(
                """
                UPDATE assets SET
                    preview_file = NULL,
                    preview_status = 'pending',
                    preview_error = NULL
                WHERE preview_status = 'error' AND ext IN ('glb','gltf','fbx')
                """
            )
        run_scan_cycle()

    background_tasks.add_task(_retry_and_process)
    return {"ok": True, "message": "Failed previews queued for retry"}


@app.get("/api/models")
def list_models(
    request: Request,
    tags: str | None = Query(
        None,
        description="Теги через запятую — модель подходит, если есть хотя бы один",
    ),
    tags_all: str | None = Query(
        None,
        description="Теги через запятую — нужны все перечисленные",
    ),
    group: str | None = Query(
        None,
        description="ID группы запуска из launch_groups.json",
    ),
    path_prefix: str | None = Query(None, description="Путь начинается с префикса"),
    ext: str | None = Query(None, description="Расширения через запятую"),
    name_contains: str | None = Query(None, description="Подстрока в имени или пути"),
    base_url: str | None = Query(
        None,
        description="Базовый URL для абсолютных ссылок (view/download/preview)",
    ),
) -> dict:
    launch_group = None
    if group:
        launch_group = get_launch_group(LAUNCH_GROUPS_PATH, group)
        if launch_group is None:
            raise HTTPException(status_code=404, detail=f"Unknown launch group: {group}")
        payload = list_model_items(
            group=launch_group,
            base_url=_resolve_base_url(request, base_url),
        )
    else:
        payload = list_model_items(
            filters=filters_from_query(
                tags=tags,
                tags_all=tags_all,
                path_prefix=path_prefix,
                ext=ext,
                name_contains=name_contains,
            ),
            base_url=_resolve_base_url(request, base_url),
        )
    payload.pop("count", None)
    return payload


@app.get("/api/launch-groups")
def launch_groups_list() -> dict:
    groups = list_launch_groups(LAUNCH_GROUPS_PATH)
    return {
        "path": str(LAUNCH_GROUPS_PATH),
        "configured": LAUNCH_GROUPS_PATH.is_file(),
        "groups": [g.to_public_dict(include_unit_main=False) for g in groups],
    }


@app.get("/api/launch-groups/{group_id}")
def launch_group_detail(group_id: str) -> dict:
    group = get_launch_group(LAUNCH_GROUPS_PATH, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"Unknown launch group: {group_id}")
    return group.to_public_dict()


@app.get("/api/launch-groups/{group_id}/models")
def launch_group_models(
    group_id: str,
    request: Request,
    base_url: str | None = Query(
        None,
        description="Базовый URL для абсолютных ссылок (view/download/preview)",
    ),
) -> dict:
    group = get_launch_group(LAUNCH_GROUPS_PATH, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"Unknown launch group: {group_id}")
    return list_model_items(
        group=group,
        base_url=_resolve_base_url(request, base_url),
    )


@app.post("/api/models/query")
def models_query(
    body: ModelQueryBody,
    request: Request,
    base_url: str | None = Query(
        None,
        description="Базовый URL для абсолютных ссылок (view/download/preview)",
    ),
) -> dict:
    tags = normalize_tag_list(body.tags)
    tags_all = normalize_tag_list(body.tags_all)
    ext = tuple(x.lower().lstrip(".") for x in body.ext if str(x).strip())
    filters = GroupFilters(
        tags=tuple(tags),
        tags_all=tuple(tags_all),
        path_prefix=(body.path_prefix or "").strip().replace("\\", "/") or None,
        ext=ext,
        name_contains=(body.name_contains or "").strip() or None,
    )
    return list_model_items(
        filters=filters,
        base_url=_resolve_base_url(request, base_url),
    )


def _sql_filters(body: CatalogSearchBody) -> dict:
    """Пользовательские параметры → аргументы db.build_asset_filter."""
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


@app.get("/api/categories")
def categories() -> dict:
    """Категории каталога со счётчиками — основа навигации по большому каталогу."""
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


@app.post("/api/catalog/search")
def catalog_search(
    body: CatalogSearchBody,
    request: Request,
    base_url: str | None = Query(None, description="Базовый URL для абсолютных ссылок"),
) -> dict:
    """Фасетный поиск: фильтры считаются в БД, семантика — в Qdrant.

    Без `query` это обычная постраничная выдача с фильтрами. С `query` сначала
    берётся семантическая выборка из Qdrant (с теми же фильтрами в payload), а
    затем она пересекается с БД — так свободный запрос не ломает фасеты.
    """
    filters = _sql_filters(body)
    resolved_base = _resolve_base_url(request, base_url)
    query = (body.query or "").strip()

    if query:
        if not OPENAI_API_KEY:
            raise HTTPException(
                status_code=400, detail="Задайте OPENAI_API_KEY для поиска по запросу"
            )
        semantic = ai_describe.semantic_search(
            query,
            # Берём с запасом: часть попаданий отсеют SQL-фильтры.
            limit=min(200, (body.offset + body.limit) * 3),
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
            base_url=resolved_base,
            paths_subset=ranked,
            **{k: v for k, v in filters.items()},
        )
        # Возвращаем порядок Qdrant: SQL отдал строки, но релевантность знает он.
        order = {path: i for i, path in enumerate(ranked)}
        payload["items"].sort(key=lambda item: order.get(item["path"], len(order)))
        payload["query"] = query
        return payload

    return search_model_items(
        limit=body.limit,
        offset=body.offset,
        sort=body.sort,
        with_facets=body.facets,
        base_url=resolved_base,
        **filters,
    )


@app.get("/api/preview")
def serve_preview(path: str = Query(..., description="Путь модели в каталоге")):
    """Превью модели: локальный PNG, если он есть, иначе оригинал из хранилища.

    Ingest работает на одной машине, а отдаёт каталог другая — на ней локальных
    PNG нет, зато исходная картинка лежит в хранилище рядом с моделью.
    """
    row = db.get_assets_bulk([path]).get(path)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown model path")
    basename = row.get("preview_file")
    if basename and row.get("preview_status") == "ok":
        local = (PREVIEWS_DIR / basename).resolve()
        try:
            local.relative_to(PREVIEWS_DIR.resolve())
        except ValueError:
            local = None
        if local is not None and local.is_file():
            return FileResponse(local, media_type="image/png")
    preview_key = row.get("preview_key")
    if not preview_key or not storage.object_exists(preview_key):
        raise HTTPException(status_code=404, detail="Preview not available")
    return StreamingResponse(
        storage.open_stream(preview_key),
        media_type=storage.guess_media_type(preview_key),
    )


@app.get("/api/tag-list")
def tag_list() -> dict:
    return {"tags": db.list_tag_counts()}


@app.post("/api/tags/append")
def tags_append(body: AppendTagsBody) -> dict:
    tags = normalize_tag_list(body.tags)
    if not tags:
        raise HTTPException(status_code=400, detail="No valid tags")
    with db.write_transaction() as conn:
        if not db.get_row(conn, body.path):
            raise HTTPException(status_code=404, detail="Unknown model path")
        db.add_tags(conn, body.path, tags, "manual", time.time())
    return {"ok": True, "path": body.path, "tags": tags}


@app.post("/api/tags/auto")
def tags_auto(
    background_tasks: BackgroundTasks,
    limit: int = Query(30, ge=1, le=200),
    only_missing: bool = Query(True),
    sync: bool = Query(False, description="Выполнить сразу и вернуть статистику"),
) -> dict:
    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="Задайте OPENAI_API_KEY для авто-тегов по превью",
        )
    if sync:
        return run_auto_tag_batch(limit=limit, only_missing=only_missing)

    def _job() -> None:
        run_auto_tag_batch(limit=limit, only_missing=only_missing)

    background_tasks.add_task(_job)
    return {
        "ok": True,
        "scheduled": True,
        "limit": limit,
        "only_missing": only_missing,
    }


@app.post("/api/describe")
def describe_models(
    background_tasks: BackgroundTasks,
    limit: int = Query(20, ge=1, le=200),
    only_missing: bool = Query(True),
    sync: bool = Query(False, description="Выполнить сразу и вернуть статистику"),
) -> dict:
    """AI-описание моделей по превью + запись эмбеддингов в Qdrant."""
    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="Задайте OPENAI_API_KEY для AI-описаний и векторного поиска",
        )
    if sync:
        return ai_describe.run_describe_batch(limit=limit, only_missing=only_missing)

    def _job() -> None:
        ai_describe.run_describe_batch(limit=limit, only_missing=only_missing)

    background_tasks.add_task(_job)
    return {"ok": True, "scheduled": True, "limit": limit, "only_missing": only_missing}


@app.post("/api/reindex-embeddings")
def reindex_embeddings(
    background_tasks: BackgroundTasks,
    limit: int = Query(200, ge=1, le=1000),
    sync: bool = Query(False),
) -> dict:
    """Досылает в Qdrant модели с описанием, но без свежего вектора."""
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=400, detail="Задайте OPENAI_API_KEY")
    if sync:
        return ai_describe.run_embed_batch(limit=limit)

    background_tasks.add_task(lambda: ai_describe.run_embed_batch(limit=limit))
    return {"ok": True, "scheduled": True, "limit": limit}


@app.post("/api/search/semantic")
def search_semantic(body: SemanticSearchBody) -> dict:
    """Поиск моделей по описанию на естественном языке (например «sci-fi робот с оружием»)."""
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=400, detail="Задайте OPENAI_API_KEY")
    result = ai_describe.semantic_search(
        body.query,
        limit=body.limit,
        filters={
            "categories": [c for c in (canonical_category(x) for x in body.category) if c],
            "tags_any": normalize_tag_list(body.tags),
            "tags_all": normalize_tag_list(body.tags_all),
            "ext": [x.lower().lstrip(".") for x in body.ext if str(x).strip()],
            "age_ratings": ratings_up_to(body.age_max),
            "exclude_nsfw": body.safe,
            "kid_only": body.kid_only,
        },
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Search failed"))
    return result


@app.get("/api/search/similar")
def search_similar(
    path: str = Query(..., description="Путь модели, для которой искать похожие"),
    limit: int = Query(12, ge=1, le=50),
) -> dict:
    """Похожие модели на заданную (по её AI-описанию)."""
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=400, detail="Задайте OPENAI_API_KEY")
    result = ai_describe.similar_to_path(path, limit=limit)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Search failed"))
    return result


@app.get("/api/vector-status")
def vector_status() -> dict:
    return {
        "collection": QDRANT_COLLECTION,
        "embed_model": OPENAI_EMBED_MODEL,
        "openai_configured": bool(OPENAI_API_KEY),
        **vector_store.collection_info(),
    }


@app.get("/api/file")
def serve_file(path: str = Query(..., description="Path relative to models root")):
    return _serve_asset(path, as_attachment=True)


@app.get("/files/{file_path:path}")
def serve_models_file(file_path: str):
    return _serve_asset(file_path, as_attachment=False)
