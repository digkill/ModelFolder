"""Фоновый воркер обогащения каталога.

Отдельный процесс/контейнер (`python -m app.worker`), который в цикле:
  1. проставляет авто-теги по превью (OpenAI Vision) — source=openai;
  2. генерирует AI-описания моделей и кладёт эмбеддинги в Qdrant;
  3. досылает в Qdrant описанные, но ещё не векторизованные модели.

Превью и сам каталог наполняет сервис `app` (сканер). Воркер лишь читает
готовые превью и обогащает БД, поэтому лёгок по памяти. Работает поверх той же
SQLite (WAL + busy_timeout) и того же Qdrant.
"""

from __future__ import annotations

import logging
import signal
import time

import app.db as db
from app.ai_describe import run_describe_batch, run_embed_batch
from app.paths import (
    OPENAI_API_KEY,
    WORKER_DESCRIBE_BATCH,
    WORKER_DO_DESCRIBE,
    WORKER_DO_TAGS,
    WORKER_INTERVAL_SEC,
    WORKER_TAG_BATCH,
)
from app.vision_tags import run_auto_tag_batch

log = logging.getLogger("worker")

_stop = False


def _handle_signal(signum, _frame) -> None:
    global _stop
    log.info("Received signal %s — stopping after current cycle", signum)
    _stop = True


def _interruptible_sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stop:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))


def _run_cycle() -> int:
    """Один проход обогащения. Возвращает число обработанных моделей."""
    total = 0
    if WORKER_DO_TAGS:
        r = run_auto_tag_batch(limit=WORKER_TAG_BATCH, only_missing=True)
        done = int(r.get("processed", 0))
        total += done
        if done or r.get("error_count"):
            log.info("tags: processed=%s errors=%s", done, r.get("error_count", 0))
    if WORKER_DO_DESCRIBE:
        r = run_describe_batch(limit=WORKER_DESCRIBE_BATCH, only_missing=True)
        done = int(r.get("processed", 0))
        total += done
        if done or r.get("error_count"):
            log.info("describe: processed=%s errors=%s", done, r.get("error_count", 0))
        # Досылаем описанные ранее, но не векторизованные (например после сбоя Qdrant).
        e = run_embed_batch(limit=max(WORKER_DESCRIBE_BATCH * 5, 100))
        if int(e.get("processed", 0)):
            log.info("embed backfill: processed=%s", e.get("processed"))
    return total


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    db.init_db()
    log.info(
        "Worker started: interval=%ss tags=%s describe=%s",
        WORKER_INTERVAL_SEC,
        WORKER_DO_TAGS,
        WORKER_DO_DESCRIBE,
    )
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY is not set — worker will idle until it is provided")

    while not _stop:
        try:
            if OPENAI_API_KEY:
                processed = _run_cycle()
                # Есть работа — сразу следующий проход (короткая пауза), иначе ждём интервал.
                _interruptible_sleep(2.0 if processed else WORKER_INTERVAL_SEC)
            else:
                _interruptible_sleep(WORKER_INTERVAL_SEC)
        except Exception:
            log.exception("Worker cycle failed")
            _interruptible_sleep(WORKER_INTERVAL_SEC)

    log.info("Worker stopped")


if __name__ == "__main__":
    main()
