    """Ограничение памяти процесса превью (и типичных дочерних процессов) через rlimit."""

from __future__ import annotations

import sys


def apply_preview_process_memory_limit() -> None:
    """
    Жёсткий потолок виртуальной памяти для текущего процесса (Unix).

    Дочерние процессы, которые наследуюют лимит (Chromium из Playwright, часто — да),
    не смогут выделить сверх суммарного адресного пространства на процесс с тем же лимитом.

    На Windows не поддерживается. На macOS/Linux — best-effort (RLIMIT_AS / RLIMIT_DATA).
    """
    if sys.platform == "win32":
        return

    try:
        from app.paths import PREVIEW_MAX_MEMORY_MB
    except Exception:
        return

    if PREVIEW_MAX_MEMORY_MB <= 0:
        return

    try:
        import resource
    except ImportError:
        return

    limit = int(PREVIEW_MAX_MEMORY_MB) * 1024 * 1024

    for name in ("RLIMIT_AS", "RLIMIT_DATA"):
        r = getattr(resource, name, None)
        if r is None:
            continue
        try:
            resource.setrlimit(r, (limit, limit))
            return
        except (ValueError, OSError):
            continue

    print(
        "preview_worker: не удалось установить лимит памяти (RLIMIT_AS/DATA)",
        file=sys.stderr,
    )
