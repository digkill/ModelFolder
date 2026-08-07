"""AI-описания моделей и семантический поиск похожих через Qdrant.

Пайплайн:
  1. По PNG-превью OpenAI Vision генерирует короткое текстовое описание модели.
  2. Описание (+ имя + теги) кодируется в эмбеддинг OpenAI.
  3. Вектор кладётся в Qdrant с payload'ом (path, name, description).
  4. Поиск похожих / семантический поиск идёт по косинусной близости.
"""

from __future__ import annotations

import base64
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import app.db as db
from app import vector_store
from app.openai_client import make_openai_client
from app.paths import (
    OPENAI_API_KEY,
    OPENAI_DESCRIBE_MODEL,
    OPENAI_EMBED_MODEL,
    WORKER_CONCURRENCY,
)
from app.preview_access import resolve_preview_file

log = logging.getLogger(__name__)

SOURCE_OPENAI = "openai"


def _openai_client():
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set (добавьте в .env или окружение)")
    return make_openai_client()


def _safe_preview_path(preview_file: str, preview_key: str | None = None) -> Path | None:
    return resolve_preview_file(preview_file, preview_key)


def describe_image(client, png_path: Path) -> str:
    """Возвращает короткое описание 3D-модели по её превью."""
    b64 = base64.standard_b64encode(png_path.read_bytes()).decode("ascii")
    prompt = (
        "You see a thumbnail of a single 3D model. Describe it in 2-4 English "
        "sentences for a similarity search index. Cover: object category, style "
        "(realistic, stylized, low-poly, sci-fi, fantasy, cartoon...), likely "
        "materials/colors, and typical use-case (game asset, prop, character, "
        "vehicle, environment...). Be concrete and factual, no markdown, no lists."
    )
    resp = client.chat.completions.create(
        model=OPENAI_DESCRIBE_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                            "detail": "low",
                        },
                    },
                ],
            }
        ],
        max_tokens=220,
    )
    return (resp.choices[0].message.content or "").strip()


def embed_text(client, text: str) -> list[float]:
    resp = client.embeddings.create(model=OPENAI_EMBED_MODEL, input=text)
    return list(resp.data[0].embedding)


def build_embed_text(
    name: str,
    tags: list[str],
    description: str,
    *,
    category: str | None = None,
    collection: str | None = None,
) -> str:
    parts: list[str] = []
    if name:
        parts.append(f"Name: {name}")
    if category:
        parts.append(f"Category: {category}")
    if collection and collection != name:
        parts.append(f"Collection: {collection}")
    if tags:
        parts.append(f"Tags: {', '.join(tags)}")
    if description:
        parts.append(description)
    return "\n".join(parts).strip()


def _tags_for(path: str) -> list[str]:
    trows = db.get_tags_bulk([path]).get(path, [])
    return [t["tag"] for t in trows]


def _payload_for(path: str, asset: dict | None, tags: list[str], description: str) -> dict:
    """Payload точки Qdrant: всё, по чему потом фильтруется семантический поиск."""
    asset = asset or {}

    def flag(value) -> bool | None:
        return None if value is None else bool(int(value))

    return {
        "name": asset.get("name"),
        "description": description,
        "tags": tags,
        "category": asset.get("category"),
        "collection": asset.get("collection"),
        "ext": asset.get("ext"),
        "age_rating": asset.get("age_rating"),
        "nsfw": bool(flag(asset.get("nsfw")) or flag(asset.get("content_adult"))),
        "kid_friendly": bool(flag(asset.get("kid_friendly"))),
        "animated": bool(asset.get("animation_count") or 0),
        "rigged": bool(flag(asset.get("has_rig"))),
        "face_count": asset.get("face_count"),
        "size": asset.get("size"),
    }


def _index_one(client, path: str, name: str, description: str, now: float) -> None:
    """Строит эмбеддинг и апсертит модель в Qdrant, помечает embedded_at."""
    tags = _tags_for(path)
    asset = db.get_assets_bulk([path]).get(path)
    text = build_embed_text(
        name,
        tags,
        description,
        category=(asset or {}).get("category"),
        collection=(asset or {}).get("collection"),
    )
    vector = embed_text(client, text)
    vector_store.upsert_model(path, vector, _payload_for(path, asset, tags, description))
    with db.write_transaction() as conn:
        db.set_embedded_at(conn, path, now)


def run_describe_batch(*, limit: int = 20, only_missing: bool = True) -> dict:
    """Генерирует описания для моделей с превью, эмбеддит и кладёт в Qdrant."""
    if not OPENAI_API_KEY:
        return {"ok": False, "error": "Set OPENAI_API_KEY", "processed": 0}

    client = _openai_client()
    rows = db.fetch_assets_for_describe(max(1, min(limit, 200)), only_missing)
    processed = 0
    errors: list[str] = []

    def _describe_one(row: dict) -> tuple[str, str | None]:
        """Описание + эмбеддинг одной модели: всё время уходит на сеть."""
        path = row["path"]
        png = _safe_preview_path(row["preview_file"], row.get("preview_key"))
        if png is None:
            return path, "preview file missing"
        try:
            now = time.time()
            description = describe_image(client, png)
            if not description:
                return path, "empty description"
            with db.write_transaction() as conn:
                db.set_description(
                    conn, path, description=description, source=SOURCE_OPENAI, now=now
                )
            _index_one(client, path, row["name"], description, now)
            return path, None
        except Exception as e:  # noqa: BLE001
            log.warning("Describe/embed failed %s: %s", path, e)
            return path, str(e)

    # Модели независимы, поэтому обрабатываем пачку параллельно: последовательные
    # вызовы Vision + embeddings делали обогащение в разы медленнее заливки.
    with ThreadPoolExecutor(max_workers=max(1, WORKER_CONCURRENCY)) as pool:
        for path, error in pool.map(_describe_one, rows):
            if error:
                errors.append(f"{path}: {error}")
            else:
                processed += 1

    return {
        "ok": True,
        "processed": processed,
        "candidates": len(rows),
        "errors": errors[:50],
        "error_count": len(errors),
    }


def run_embed_batch(*, limit: int = 200) -> dict:
    """Досылает в Qdrant модели, у которых есть описание, но нет свежего вектора."""
    if not OPENAI_API_KEY:
        return {"ok": False, "error": "Set OPENAI_API_KEY", "processed": 0}

    client = _openai_client()
    rows = db.fetch_assets_for_embedding(max(1, min(limit, 1000)))
    processed = 0
    errors: list[str] = []

    def _embed_one(row: dict) -> tuple[str, str | None, bool]:
        path = row["path"]
        asset = db.get_asset_with_description(path)
        if not asset or not asset.get("description"):
            # Описание исчезло между выборкой и обработкой — не ошибка и не работа.
            return path, None, True
        try:
            _index_one(client, path, asset["name"], asset["description"], time.time())
            return path, None, False
        except Exception as e:  # noqa: BLE001
            log.warning("Embed failed %s: %s", path, e)
            return path, str(e), False

    with ThreadPoolExecutor(max_workers=max(1, WORKER_CONCURRENCY)) as pool:
        for path, error, skipped in pool.map(_embed_one, rows):
            if error:
                errors.append(f"{path}: {error}")
            elif not skipped:
                processed += 1
    return {
        "ok": True,
        "processed": processed,
        "candidates": len(rows),
        "errors": errors[:50],
        "error_count": len(errors),
    }


def _hydrate(hits: list[dict]) -> list[dict]:
    """Дополняет результаты Qdrant данными из БД (актуальные превью/теги/категория)."""
    paths = [h["path"] for h in hits if h.get("path")]
    tags_map = db.get_tags_bulk(paths)
    assets = db.get_assets_bulk(paths)  # одним запросом вместо N штук в цикле
    out: list[dict] = []
    for h in hits:
        path = h.get("path")
        asset = assets.get(path) if path else None
        if asset is None:
            continue  # модель удалена из каталога — пропускаем
        payload = h.get("payload") or {}
        preview_url = (
            f"/previews/{asset['preview_file']}"
            if asset.get("preview_status") == "ok" and asset.get("preview_file")
            else None
        )
        out.append(
            {
                "path": path,
                "name": asset.get("name"),
                "score": h.get("score"),
                "description": asset.get("description") or payload.get("description"),
                "tags": [t["tag"] for t in tags_map.get(path, [])],
                "category": asset.get("category"),
                "collection": asset.get("collection"),
                "ext": asset.get("ext"),
                "size": asset.get("size"),
                "age_rating": asset.get("age_rating"),
                "preview_url": preview_url,
            }
        )
    return out


def similar_to_path(path: str, *, limit: int = 12) -> dict:
    """Находит модели, похожие на заданную (по её сохранённому вектору)."""
    if not OPENAI_API_KEY:
        return {"ok": False, "error": "Set OPENAI_API_KEY", "results": []}
    asset = db.get_asset_with_description(path)
    if asset is None:
        return {"ok": False, "error": "Unknown model path", "results": []}
    if not asset.get("description"):
        return {
            "ok": False,
            "error": "Model has no AI description yet — run /api/describe",
            "results": [],
        }
    client = _openai_client()
    text = build_embed_text(asset["name"], _tags_for(path), asset["description"])
    vector = embed_text(client, text)
    hits = vector_store.search(vector, limit=limit, exclude_path=path)
    return {"ok": True, "query": {"path": path}, "results": _hydrate(hits)}


def semantic_search(
    query: str,
    *,
    limit: int = 12,
    filters: dict | None = None,
    score_threshold: float | None = None,
) -> dict:
    """Поиск моделей по свободному запросу с фильтрами по категории/тегам/рейтингу."""
    if not OPENAI_API_KEY:
        return {"ok": False, "error": "Set OPENAI_API_KEY", "results": []}
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "Empty query", "results": []}
    client = _openai_client()
    vector = embed_text(client, q)
    query_filter = vector_store.build_filter(**(filters or {}))
    hits = vector_store.search(
        vector,
        limit=limit,
        query_filter=query_filter,
        score_threshold=score_threshold,
    )
    return {
        "ok": True,
        "query": {"text": q, "filters": filters or {}},
        "results": _hydrate(hits),
    }
