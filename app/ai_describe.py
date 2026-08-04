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
from pathlib import Path

import app.db as db
from app import vector_store
from app.paths import (
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_DESCRIBE_MODEL,
    OPENAI_EMBED_MODEL,
    OPENAI_ORG_ID,
    PREVIEWS_DIR,
)

log = logging.getLogger(__name__)

SOURCE_OPENAI = "openai"


def _openai_client():
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set (добавьте в .env или окружение)")
    from openai import OpenAI

    kw: dict = {"api_key": OPENAI_API_KEY}
    if OPENAI_BASE_URL:
        kw["base_url"] = OPENAI_BASE_URL
    if OPENAI_ORG_ID:
        kw["organization"] = OPENAI_ORG_ID
    return OpenAI(**kw)


def _safe_preview_path(preview_file: str) -> Path | None:
    png = (PREVIEWS_DIR / preview_file).resolve()
    try:
        png.relative_to(PREVIEWS_DIR.resolve())
    except ValueError:
        return None
    return png if png.is_file() else None


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


def build_embed_text(name: str, tags: list[str], description: str) -> str:
    parts: list[str] = []
    if name:
        parts.append(f"Name: {name}")
    if tags:
        parts.append(f"Tags: {', '.join(tags)}")
    if description:
        parts.append(description)
    return "\n".join(parts).strip()


def _tags_for(path: str) -> list[str]:
    trows = db.get_tags_bulk([path]).get(path, [])
    return [t["tag"] for t in trows]


def _index_one(client, path: str, name: str, description: str, now: float) -> None:
    """Строит эмбеддинг и апсертит модель в Qdrant, помечает embedded_at."""
    tags = _tags_for(path)
    text = build_embed_text(name, tags, description)
    vector = embed_text(client, text)
    vector_store.upsert_model(
        path,
        vector,
        {"name": name, "description": description, "tags": tags},
    )
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

    for row in rows:
        path = row["path"]
        png = _safe_preview_path(row["preview_file"])
        if png is None:
            errors.append(f"{path}: preview file missing")
            continue
        try:
            now = time.time()
            description = describe_image(client, png)
            if not description:
                errors.append(f"{path}: empty description")
                continue
            with db.write_transaction() as conn:
                db.set_description(
                    conn, path, description=description, source=SOURCE_OPENAI, now=now
                )
            _index_one(client, path, row["name"], description, now)
            processed += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Describe/embed failed %s: %s", path, e)
            errors.append(f"{path}: {e}")

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
    for row in rows:
        path = row["path"]
        asset = db.get_asset_with_description(path)
        if not asset or not asset.get("description"):
            continue
        try:
            _index_one(client, path, asset["name"], asset["description"], time.time())
            processed += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Embed failed %s: %s", path, e)
            errors.append(f"{path}: {e}")
    return {
        "ok": True,
        "processed": processed,
        "candidates": len(rows),
        "errors": errors[:50],
        "error_count": len(errors),
    }


def _hydrate(hits: list[dict]) -> list[dict]:
    """Дополняет результаты Qdrant данными из SQLite (актуальные превью/теги)."""
    tags_map = db.get_tags_bulk([h["path"] for h in hits if h.get("path")])
    out: list[dict] = []
    for h in hits:
        path = h.get("path")
        if not path:
            continue
        asset = db.get_asset_with_description(path)
        if asset is None:
            continue  # модель удалена из каталога — пропускаем
        payload = h.get("payload") or {}
        out.append(
            {
                "path": path,
                "name": asset.get("name"),
                "score": h.get("score"),
                "description": asset.get("description") or payload.get("description"),
                "tags": [t["tag"] for t in tags_map.get(path, [])],
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


def semantic_search(query: str, *, limit: int = 12) -> dict:
    """Поиск моделей по свободному текстовому запросу на естественном языке."""
    if not OPENAI_API_KEY:
        return {"ok": False, "error": "Set OPENAI_API_KEY", "results": []}
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "Empty query", "results": []}
    client = _openai_client()
    vector = embed_text(client, q)
    hits = vector_store.search(vector, limit=limit)
    return {"ok": True, "query": {"text": q}, "results": _hydrate(hits)}
