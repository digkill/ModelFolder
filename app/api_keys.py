"""Ключи доступа к сервисному API выдачи моделей (/v1).

Полный ключ показывается один раз при создании. В БД хранится только SHA-256,
поэтому украсть действующий ключ из базы нельзя — только отозвать и выдать новый.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Any

ALL_SCOPES = ("catalog", "download", "search")
KEY_PREFIX = "mfk_"

# Скользящее окно на процесс: для нескольких реплик лимит приблизительный.
_rate_lock = threading.Lock()
_rate_hits: dict[int, list[float]] = {}


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def display_prefix(raw: str) -> str:
    return raw[:12]


def normalize_scopes(raw: list[str] | None) -> list[str]:
    wanted = {str(x).strip().lower() for x in (raw or []) if str(x).strip()}
    if not wanted:
        return list(ALL_SCOPES)
    out = [s for s in ALL_SCOPES if s in wanted]
    return out or list(ALL_SCOPES)


def extract_raw_key(request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        token = header.split(" ", 1)[1].strip()
        if token.startswith(KEY_PREFIX):
            return token
    api_key = (request.headers.get("x-api-key") or "").strip()
    if api_key.startswith(KEY_PREFIX):
        return api_key
    return None


def authenticate(request) -> dict[str, Any] | None:
    """Возвращает карточку ключа или None, если заголовок отсутствует/неверный."""
    raw = extract_raw_key(request)
    if not raw:
        return None
    import app.db as db

    row = db.get_api_key_by_hash(hash_key(raw))
    if not row or row.get("revoked_at"):
        return None
    from app.billing import attach_to_key

    return attach_to_key(row)


def has_scope(client: dict[str, Any] | None, scope: str) -> bool:
    """Без ключа (локальная разработка / сессия админа) — полный доступ."""
    if client is None:
        return True
    scopes = client.get("scopes") or []
    return scope in scopes


def check_rate_limit(client: dict[str, Any] | None) -> tuple[bool, int, int]:
    """(allowed, limit, remaining). Без ключа лимит не применяется."""
    if client is None:
        return True, 0, 0
    limit = int(client.get("rate_limit_per_min") or 120)
    if limit <= 0:
        return True, limit, limit
    key_id = int(client["id"])
    now = time.time()
    window = now - 60.0
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key_id, []) if t > window]
        remaining = max(0, limit - len(hits))
        if remaining <= 0:
            _rate_hits[key_id] = hits
            return False, limit, 0
        hits.append(now)
        _rate_hits[key_id] = hits
        return True, limit, remaining - 1


def constant_time_prefix_ok(raw: str) -> bool:
    """Грубая проверка формата без утечки по времени на длине префикса."""
    prefix = raw[: len(KEY_PREFIX)].encode()
    return hmac.compare_digest(prefix, KEY_PREFIX.encode())
