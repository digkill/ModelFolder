"""Поиск файла превью для AI-классификации.

Превью существует в двух видах: уменьшённый PNG в PREVIEWS_DIR на машине, где
работал ingest, и исходная картинка рядом с моделью в хранилище. На сервере
первого нет, поэтому AI-эндпоинты обязаны уметь взять второй, иначе
`/api/tags/auto` и `/api/describe` там падают с «preview file missing».
"""

from __future__ import annotations

import logging
from pathlib import Path

from app import storage
from app.paths import PREVIEWS_DIR

log = logging.getLogger(__name__)


def resolve_preview_file(preview_file: str | None, preview_key: str | None) -> Path | None:
    """Локальный PNG, иначе скачанный из хранилища оригинал. None — если нет ни того, ни другого."""
    if preview_file:
        png = (PREVIEWS_DIR / preview_file).resolve()
        try:
            png.relative_to(PREVIEWS_DIR.resolve())
        except ValueError:
            png = None  # попытка выхода за каталог превью — игнорируем значение из БД
        if png is not None and png.is_file():
            return png
    if preview_key:
        try:
            # Для S3 это скачивание в локальный кэш (повторные вызовы бесплатны).
            return storage.local_path(preview_key)
        except (FileNotFoundError, ValueError, OSError) as e:
            log.debug("Preview %s unavailable in storage: %s", preview_key, e)
    return None
