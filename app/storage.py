"""Абстракция хранилища моделей: локальная ФС или S3-совместимое облако.

Переключается флагом STORAGE_BACKEND (local|s3). Пути в каталоге/БД («ключи»)
всегда posix-относительные: для local — относительно MODELS_ROOT, для s3 —
относительно S3_PREFIX внутри бакета. Остальной код (сканер, превью, отдача)
работает через этот модуль и не знает, где реально лежит файл.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import stat as _stat
import threading
from pathlib import Path, PurePosixPath
from typing import Iterator

from app.paths import (
    CACHE_DIR,
    MODELS_ROOT,
    S3_ACCESS_KEY_ID,
    S3_ADDRESSING_STYLE,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_PREFIX,
    S3_REGION,
    S3_SECRET_ACCESS_KEY,
    STORAGE_BACKEND,
)

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024


def is_s3() -> bool:
    return STORAGE_BACKEND == "s3"


def backend_name() -> str:
    return "s3" if is_s3() else "local"


def safe_key(raw: str) -> str:
    """Нормализует и проверяет ключ, пришедший от клиента (защита от traversal)."""
    key = (raw or "").replace("\\", "/").lstrip("/")
    parts = PurePosixPath(key).parts
    if not parts or ".." in parts:
        raise ValueError("invalid key")
    return key


def guess_media_type(key: str) -> str:
    return mimetypes.guess_type(key)[0] or "application/octet-stream"


# --------------------------------------------------------------------------- #
# Local backend
# --------------------------------------------------------------------------- #
def _local_resolve(key: str) -> Path:
    base = MODELS_ROOT.resolve()
    candidate = (base / key).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as e:
        raise ValueError("key outside models root") from e
    return candidate


def _local_iter_objects() -> Iterator[tuple[str, int, float]]:
    root = MODELS_ROOT
    if not root.is_dir():
        return
    root_str = str(root)

    def _on_error(exc: OSError) -> None:
        # Внешний диск (FUSE) может отваливаться на отдельных папках — не роняем скан,
        # пропускаем недоступную ветку и идём дальше.
        log.warning("Scan skip unreadable dir: %s", exc)

    for dirpath, _dirnames, filenames in os.walk(root_str, onerror=_on_error):
        for fn in filenames:
            if fn.startswith("._"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
                if not _stat.S_ISREG(st.st_mode):
                    continue
                rel = os.path.relpath(full, root_str).replace(os.sep, "/")
            except OSError:
                log.debug("Skipping inaccessible file: %s", full)
                continue
            yield rel, int(st.st_size), float(st.st_mtime)


# --------------------------------------------------------------------------- #
# S3 backend
# --------------------------------------------------------------------------- #
_s3_client = None
_s3_lock = threading.Lock()


def _s3():
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    with _s3_lock:
        if _s3_client is None:
            import boto3
            from botocore.config import Config

            if not S3_BUCKET:
                raise RuntimeError("S3_BUCKET is not set (STORAGE_BACKEND=s3)")
            _s3_client = boto3.client(
                "s3",
                endpoint_url=S3_ENDPOINT_URL,
                region_name=S3_REGION,
                aws_access_key_id=S3_ACCESS_KEY_ID,
                aws_secret_access_key=S3_SECRET_ACCESS_KEY,
                config=Config(
                    s3={"addressing_style": S3_ADDRESSING_STYLE},
                    signature_version="s3v4",
                ),
            )
    return _s3_client


def _s3_full_key(key: str) -> str:
    return f"{S3_PREFIX}/{key}" if S3_PREFIX else key


def _s3_strip_prefix(full_key: str) -> str:
    if S3_PREFIX and full_key.startswith(S3_PREFIX + "/"):
        return full_key[len(S3_PREFIX) + 1 :]
    return full_key


def _s3_iter_objects() -> Iterator[tuple[str, int, float]]:
    client = _s3()
    paginator = client.get_paginator("list_objects_v2")
    kwargs = {"Bucket": S3_BUCKET}
    if S3_PREFIX:
        kwargs["Prefix"] = S3_PREFIX + "/"
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            full_key = obj["Key"]
            if full_key.endswith("/"):
                continue
            key = _s3_strip_prefix(full_key)
            if not key or PurePosixPath(key).name.startswith("._"):
                continue
            mtime = obj["LastModified"].timestamp()
            yield key, int(obj["Size"]), float(mtime)


def _s3_head(key: str) -> tuple[int, float] | None:
    from botocore.exceptions import ClientError

    try:
        resp = _s3().head_object(Bucket=S3_BUCKET, Key=_s3_full_key(key))
    except ClientError:
        return None
    return int(resp["ContentLength"]), float(resp["LastModified"].timestamp())


def _s3_cache_path(key: str, size: int, mtime: float) -> Path:
    digest = hashlib.sha1(f"{key}:{size}:{int(mtime)}".encode()).hexdigest()
    suffix = PurePosixPath(key).suffix
    return CACHE_DIR / f"{digest}{suffix}"


def _s3_local_path(key: str) -> Path:
    """Скачивает объект в локальный кэш (если ещё нет) и возвращает путь."""
    head = _s3_head(key)
    if head is None:
        raise FileNotFoundError(key)
    size, mtime = head
    cached = _s3_cache_path(key, size, mtime)
    if cached.is_file() and cached.stat().st_size == size:
        return cached
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = cached.with_suffix(cached.suffix + ".part")
    _s3().download_file(S3_BUCKET, _s3_full_key(key), str(tmp))
    tmp.replace(cached)
    return cached


def _s3_open_stream(key: str) -> Iterator[bytes]:
    body = _s3().get_object(Bucket=S3_BUCKET, Key=_s3_full_key(key))["Body"]
    try:
        while True:
            chunk = body.read(_CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        body.close()


def _s3_exists(key: str) -> bool:
    return _s3_head(key) is not None


# --------------------------------------------------------------------------- #
# Public API (backend-agnostic)
# --------------------------------------------------------------------------- #
def iter_objects() -> Iterator[tuple[str, int, float]]:
    """(key, size, mtime) по всем файлам-объектам в хранилище."""
    return _s3_iter_objects() if is_s3() else _local_iter_objects()


def local_path(key: str) -> Path:
    """Локальный путь к объекту (для рендера/распаковки). S3 — качает в кэш.

    Бросает FileNotFoundError, если объекта нет, ValueError — если ключ небезопасен.
    """
    key = safe_key(key)
    if is_s3():
        return _s3_local_path(key)
    p = _local_resolve(key)
    if not p.is_file():
        raise FileNotFoundError(key)
    return p


def open_stream(key: str) -> Iterator[bytes]:
    """Потоковая выдача байтов объекта (для HTTP)."""
    key = safe_key(key)
    if is_s3():
        return _s3_open_stream(key)

    def _gen() -> Iterator[bytes]:
        with open(_local_resolve(key), "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                yield chunk

    return _gen()


def object_exists(key: str) -> bool:
    try:
        key = safe_key(key)
    except ValueError:
        return False
    if is_s3():
        return _s3_exists(key)
    return _local_resolve(key).is_file()


def health() -> dict:
    info: dict = {"backend": backend_name()}
    if is_s3():
        info["bucket"] = S3_BUCKET
        info["prefix"] = S3_PREFIX or None
        info["endpoint"] = S3_ENDPOINT_URL
    else:
        info["models_dir"] = str(MODELS_ROOT)
        info["exists"] = MODELS_ROOT.is_dir()
    return info
