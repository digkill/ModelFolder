"""Обёртка над Qdrant: коллекция эмбеддингов моделей для поиска похожих."""

from __future__ import annotations

import logging
import threading
import uuid

from app.paths import (
    EMBED_DIM,
    QDRANT_API_KEY,
    QDRANT_COLLECTION,
    QDRANT_HOST,
    QDRANT_PORT,
    QDRANT_URL,
)

log = logging.getLogger(__name__)

# Стабильный namespace, чтобы path модели детерминированно давал один и тот же point id.
_NAMESPACE = uuid.UUID("6f1f0b5e-2a3c-4c9a-9b0e-2b7c1d4e5f60")

_client = None
_client_lock = threading.Lock()
_collection_ready = False


def point_id_for(path: str) -> str:
    """Детерминированный UUID точки по пути модели."""
    return str(uuid.uuid5(_NAMESPACE, path))


def get_client():
    """Ленивая инициализация singleton-клиента Qdrant."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            from qdrant_client import QdrantClient

            if QDRANT_URL:
                _client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
            else:
                _client = QdrantClient(
                    host=QDRANT_HOST, port=QDRANT_PORT, api_key=QDRANT_API_KEY
                )
    return _client


def ensure_collection() -> None:
    """Создаёт коллекцию при первом обращении (idempotent)."""
    global _collection_ready
    if _collection_ready:
        return
    from qdrant_client.models import Distance, VectorParams

    client = get_client()
    with _client_lock:
        if _collection_ready:
            return
        if not client.collection_exists(QDRANT_COLLECTION):
            client.create_collection(
                collection_name=QDRANT_COLLECTION,
                vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
            )
        _collection_ready = True


def upsert_model(path: str, vector: list[float], payload: dict) -> None:
    from qdrant_client.models import PointStruct

    ensure_collection()
    client = get_client()
    client.upsert(
        collection_name=QDRANT_COLLECTION,
        points=[
            PointStruct(
                id=point_id_for(path),
                vector=vector,
                payload={"path": path, **payload},
            )
        ],
    )


def delete_model(path: str) -> None:
    try:
        ensure_collection()
        client = get_client()
        client.delete(
            collection_name=QDRANT_COLLECTION,
            points_selector=[point_id_for(path)],
        )
    except Exception as e:  # best-effort: удаление из индекса не должно ронять сканер
        log.warning("Qdrant delete failed for %s: %s", path, e)


def search(
    vector: list[float],
    *,
    limit: int = 12,
    exclude_path: str | None = None,
) -> list[dict]:
    ensure_collection()
    client = get_client()
    # Берём с запасом, чтобы после исключения самого себя осталось limit.
    hits = client.query_points(
        collection_name=QDRANT_COLLECTION,
        query=vector,
        limit=limit + (1 if exclude_path else 0),
        with_payload=True,
    ).points
    results: list[dict] = []
    for h in hits:
        payload = h.payload or {}
        path = payload.get("path")
        if exclude_path and path == exclude_path:
            continue
        results.append({"path": path, "score": h.score, "payload": payload})
        if len(results) >= limit:
            break
    return results


def collection_info() -> dict:
    """Диагностика для /api/health и /api/status."""
    try:
        client = get_client()
        if not client.collection_exists(QDRANT_COLLECTION):
            return {"exists": False, "points": 0}
        info = client.get_collection(QDRANT_COLLECTION)
        return {"exists": True, "points": info.points_count}
    except Exception as e:
        return {"exists": False, "error": str(e)}
