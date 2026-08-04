"""Полная переиндексация каталога и генерация превью.

Запуск из корня проекта:
    .venv/bin/python -m scripts.reindex_and_previews
"""

import sys
import time

from app import db
from app.scanner import process_one_preview, sync_filesystem_to_db


def main() -> int:
    db.init_db()

    print("Scanning filesystem into DB...", flush=True)
    t0 = time.time()
    sync_filesystem_to_db()
    print(f"Scan done in {time.time() - t0:.1f}s", flush=True)

    conn = db._connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE preview_status = 'pending'"
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"Assets indexed: {total}, previews pending: {pending}", flush=True)

    done = 0
    while process_one_preview():
        done += 1
        if done % 10 == 0:
            print(f"Previews processed: {done}/{pending}", flush=True)

    conn = db._connect()
    try:
        ok = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE preview_status = 'ok'"
        ).fetchone()[0]
        err = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE preview_status = 'error'"
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"Finished: processed={done}, ok={ok}, error={err}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
