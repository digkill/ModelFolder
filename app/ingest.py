"""Заливка каталога моделей с локального диска в хранилище (S3) и в БД.

Единица заливки — **папка модели**: она содержит саму модель, одноимённое превью
и всё, без чего модель не откроется (`textures/`, `.bin`, `.mtl`). Папки без
превью пропускаются — по договорённости такие ассеты в каталог не берём.

Дубли отсекаются по sha256 файла модели: одна и та же модель часто лежит в
нескольких категориях под разными именами, и путь для этого не показатель.

Команды:

    python -m app.ingest scan                 # что будет залито, ничего не меняя
    python -m app.ingest upload --limit 1000  # залить не более 1000 моделей
    python -m app.ingest verify               # сверить залитое с хранилищем
    python -m app.ingest purge --yes          # удалить с диска то, что сверено

`purge` — единственная разрушающая команда, она удаляет папки с диска и требует
явного `--yes`. Перед удалением каждый файл папки сверяется с объектом в
хранилище по размеру, поэтому неполная заливка ничего не удалит.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import app.db as db
from app import storage
from app.model_meta import complexity_bucket, extract_metadata, sha256_file
from app.paths import (
    INGEST_IMAGE_EXTENSIONS,
    INGEST_MAX_FILE_MB,
    INGEST_ROOT,
    INGEST_PREVIEW_MATCH,
    INGEST_WORKERS,
    MODEL_EXTENSIONS,
    PREVIEW_PIXEL_SIZE,
    PREVIEWS_DIR,
)
from app.taxonomy import FALLBACK_CATEGORY, canonical_category

log = logging.getLogger("ingest")

# Файлы, которые не нужны в хранилище: служебный мусор ОС и ссылки-источники
# (URL сохраняем отдельным полем в БД).
_SKIP_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}
_SKIP_SUFFIXES = {".url", ".lnk"}


class Progress:
    """Индикатор долгих проходов: проценты, счётчик и оценка остатка.

    Заливка и очистка идут по тысячам папок часами. Без такого вывода в логе
    тишина, и невозможно отличить работу от зависания.
    """

    def __init__(self, total: int, label: str, *, every: int = 25):
        self.total = max(0, total)
        self.label = label
        self.every = max(1, every)
        self.done = 0
        self.bytes = 0
        self.started = time.monotonic()

    def step(self, *, size: int = 0, force: bool = False) -> None:
        self.done += 1
        self.bytes += size
        if not force and self.done % self.every and self.done != self.total:
            return
        self.report()

    def report(self) -> None:
        elapsed = time.monotonic() - self.started
        pct = (self.done / self.total * 100) if self.total else 0.0
        rate = self.done / elapsed if elapsed > 0 else 0
        remaining = (self.total - self.done) / rate if rate > 0 else 0
        bar_len = 24
        filled = int(bar_len * pct / 100)
        bar = "#" * filled + "." * (bar_len - filled)
        log.info(
            "%s [%s] %5.1f%%  %d/%d  %.1f ГБ  осталось ~%s",
            self.label,
            bar,
            pct,
            self.done,
            self.total,
            self.bytes / 1e9,
            _fmt_eta(remaining),
        )


def _fmt_eta(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{int(seconds)} с"
    if seconds < 3600:
        return f"{int(seconds // 60)} мин"
    return f"{seconds / 3600:.1f} ч"


@dataclass
class ModelCandidate:
    """Одна модель, готовая к заливке, вместе со своей папкой."""

    model_file: Path
    preview_file: Path
    dir_path: Path
    rel_dir: PurePosixPath
    files: list[Path] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Ключ модели в хранилище (он же path в БД)."""
        return str(self.rel_dir / self.model_file.name)

    @property
    def dir_key(self) -> str:
        return str(self.rel_dir)

    @property
    def category(self) -> str:
        parts = self.rel_dir.parts
        return canonical_category(parts[0] if parts else None) or FALLBACK_CATEGORY

    @property
    def collection(self) -> str:
        return self.dir_path.name


# --------------------------------------------------------------------------- #
# Обход диска
# --------------------------------------------------------------------------- #
def _dir_files(dir_path: Path) -> list[Path]:
    """Все файлы папки модели рекурсивно, кроме служебного мусора."""
    out: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(dir_path):
        for fn in filenames:
            if fn.startswith("._") or fn.lower() in _SKIP_NAMES:
                continue
            if Path(fn).suffix.lower() in _SKIP_SUFFIXES:
                continue
            out.append(Path(dirpath) / fn)
    return out


def _read_source_url(dir_path: Path, stem: str) -> str | None:
    """`.url`-файл рядом с моделью хранит ссылку на источник — забираем её в БД."""
    for candidate in (dir_path / f"{stem}.url", *sorted(dir_path.glob("*.url"))):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.lower().startswith("url="):
                url = line[4:].strip()
                if url:
                    return url[:2000]
    return None


# Имена, которыми обычно называют превью, когда оно не совпадает с именем модели.
_PREVIEW_HINTS = ("preview", "thumb", "thumbnail", "render", "screenshot", "cover", "icon")


def _pick_preview(stem: str, images: dict[str, Path], model_count: int) -> Path | None:
    """Подбирает превью, когда одноимённой картинки нет.

    Осторожно с папками, где несколько моделей: приписать всем одну картинку —
    значит испортить и выдачу, и AI-классификацию, которая смотрит именно на неё.
    Поэтому там берём только картинку, чьё имя явно связано с именем модели.
    """
    if not images:
        return None
    low = {name.lower(): path for name, path in images.items()}
    target = stem.lower()

    # Имя картинки содержит имя модели или наоборот — связь однозначная.
    related = [p for name, p in low.items() if target in name or name in target]
    if related:
        return sorted(related)[0]

    if model_count > 1:
        return None  # несколько моделей и ни одной явной пары — не гадаем

    hinted = [p for name, p in low.items() if any(h in name for h in _PREVIEW_HINTS)]
    if hinted:
        return sorted(hinted)[0]

    # Единственная модель в папке: самая крупная картинка — почти всегда рендер,
    # а не иконка материала или логотип автора.
    return max(images.values(), key=lambda p: p.stat().st_size)


def iter_candidates(root: Path, *, preview_match: str | None = None) -> list[ModelCandidate]:
    """Модели, для которых нашлось превью. Остальные пропускаются."""
    preview_match = (preview_match or INGEST_PREVIEW_MATCH).lower()
    root = root.resolve()
    found: list[ModelCandidate] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        dir_path = Path(dirpath)
        models = [f for f in filenames if Path(f).suffix.lower() in MODEL_EXTENSIONS]
        if not models:
            continue
        images = {
            Path(f).stem: dir_path / f
            for f in filenames
            if Path(f).suffix.lower() in INGEST_IMAGE_EXTENSIONS
        }
        rel = PurePosixPath(dir_path.relative_to(root).as_posix())
        cached_files: list[Path] | None = None
        for model_name in sorted(models):
            stem = Path(model_name).stem
            preview = images.get(stem)
            if preview is None and preview_match == "any":
                preview = _pick_preview(stem, images, len(models))
            if preview is None:
                continue  # превью не нашли — модель в каталог не берём
            if cached_files is None:
                cached_files = _dir_files(dir_path)
            found.append(
                ModelCandidate(
                    model_file=dir_path / model_name,
                    preview_file=preview,
                    dir_path=dir_path,
                    rel_dir=rel,
                    files=cached_files,
                )
            )
    found.sort(key=lambda c: c.key.lower())
    return found


# --------------------------------------------------------------------------- #
# Превью
# --------------------------------------------------------------------------- #
def _preview_basename(key: str, mtime: float) -> str:
    import hashlib

    digest = hashlib.sha1(f"{key}:{int(mtime)}".encode()).hexdigest()
    return f"{digest}.png"


def _make_local_preview(src: Path, out_path: Path) -> bool:
    """Кладёт уменьшенную PNG-копию превью в PREVIEWS_DIR (её читают AI-воркеры)."""
    try:
        from PIL import Image, ImageOps

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            if getattr(img, "is_animated", False):
                img.seek(0)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            img.thumbnail((PREVIEW_PIXEL_SIZE, PREVIEW_PIXEL_SIZE), Image.Resampling.LANCZOS)
            img.save(out_path, format="PNG", optimize=True)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Preview conversion failed for %s: %s", src, e)
        return False


# --------------------------------------------------------------------------- #
# Заливка
# --------------------------------------------------------------------------- #
def _upload_dir(candidate: ModelCandidate, *, workers: int) -> tuple[int, int, list[str]]:
    """Заливает все файлы папки. Возвращает (файлов, байт, ошибки)."""
    root_dir = candidate.dir_path
    max_bytes = INGEST_MAX_FILE_MB * 1024 * 1024 if INGEST_MAX_FILE_MB else 0
    errors: list[str] = []
    uploaded = 0
    total_bytes = 0

    def _one(local: Path) -> tuple[int, str | None]:
        rel = PurePosixPath(local.relative_to(root_dir).as_posix())
        key = str(candidate.rel_dir / rel)
        try:
            size = local.stat().st_size
        except OSError as e:
            return 0, f"{key}: {e}"
        if max_bytes and size > max_bytes:
            return 0, f"{key}: skipped, {size / 1e6:.0f} MB > INGEST_MAX_FILE_MB"
        remote = storage.stat_object(key)
        if remote is not None and remote[0] == size:
            return size, None  # уже залит (повторный запуск) — не платим за перезалив
        try:
            storage.upload_file(local, key)
        except Exception as e:  # noqa: BLE001
            return 0, f"{key}: {e}"
        return size, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for size, err in pool.map(_one, candidate.files):
            if err:
                errors.append(err)
            else:
                uploaded += 1
                total_bytes += size
    return uploaded, total_bytes, errors


def _asset_values(
    candidate: ModelCandidate,
    *,
    content_hash: str,
    now: float,
    record_local_dir: bool = True,
) -> dict:
    stat = candidate.model_file.stat()
    meta = extract_metadata(candidate.model_file)
    bbox = meta.get("bbox") or [None, None, None]
    preview_rel = PurePosixPath(
        candidate.preview_file.relative_to(candidate.dir_path).as_posix()
    )
    preview_key = str(candidate.rel_dir / preview_rel)

    preview_basename = _preview_basename(candidate.key, stat.st_mtime)
    ok = _make_local_preview(candidate.preview_file, PREVIEWS_DIR / preview_basename)

    meta_json = {
        k: v
        for k, v in meta.items()
        if k not in {"bbox"} and v is not None
    }
    meta_json["complexity"] = complexity_bucket(meta.get("face_count"))
    meta_json["source_dir"] = str(candidate.dir_path)
    meta_json["file_count"] = len(candidate.files)

    return {
        "name": candidate.model_file.name,
        "ext": candidate.model_file.suffix.lower().lstrip("."),
        "size": int(stat.st_size),
        "mtime": float(stat.st_mtime),
        "content_hash": content_hash,
        "category": candidate.category,
        "collection": candidate.collection,
        "dir_key": candidate.dir_key,
        # При ручной загрузке исходник лежит во временной папке, которая тут же
        # удаляется — записывать её как «оригинал на диске» нельзя, иначе purge
        # будет считать её пропавшей.
        "local_dir": str(candidate.dir_path) if record_local_dir else None,
        "source_url": _read_source_url(candidate.dir_path, candidate.model_file.stem),
        "preview_file": preview_basename if ok else None,
        "preview_key": preview_key,
        "preview_source": "companion",
        "preview_status": "ok" if ok else "error",
        "vertex_count": meta.get("vertex_count"),
        "face_count": meta.get("face_count"),
        "mesh_count": meta.get("mesh_count"),
        "material_count": meta.get("material_count"),
        "texture_count": meta.get("texture_count"),
        "animation_count": meta.get("animation_count"),
        "has_rig": None if meta.get("has_rig") is None else int(bool(meta.get("has_rig"))),
        "bbox_x": bbox[0],
        "bbox_y": bbox[1],
        "bbox_z": bbox[2],
        "meta_json": json.dumps(meta_json, ensure_ascii=False),
        "ingested_at": now,
    }


def run_upload(
    root: Path,
    *,
    limit: int | None,
    workers: int,
    dry_run: bool,
    force: bool,
    shard: int = 0,
    shards: int = 1,
    record_local_dir: bool = True,
    preview_match: str | None = None,
) -> dict:
    candidates = iter_candidates(root, preview_match=preview_match)
    total_found = len(candidates)
    if shards > 1:
        # Делим детерминированно по индексу в отсортированном списке: каждый
        # процесс берёт свою часть, поэтому параллельные заливки не пересекаются
        # и не гоняют одни и те же папки в S3 по нескольку раз.
        candidates = [c for i, c in enumerate(candidates) if i % shards == shard]
        log.info(
            "Found %d model(s) under %s; shard %d/%d takes %d",
            total_found, root, shard + 1, shards, len(candidates),
        )
    else:
        log.info("Found %d model(s) with preview under %s", total_found, root)

    known_paths: set[str] = set()
    with db.write_transaction() as conn:
        known_paths = db.all_paths(conn)

    stats = {
        "candidates": len(candidates),
        "uploaded": 0,
        "skipped_existing": 0,
        "skipped_duplicate": 0,
        "failed": 0,
        "bytes": 0,
        "files": 0,
    }
    errors: list[str] = []
    seen_hashes: set[str] = set()
    planned = len(candidates) if limit is None else min(limit, len(candidates))
    progress = Progress(planned, "Заливка", every=10)

    for candidate in candidates:
        if limit is not None and stats["uploaded"] >= limit:
            break
        before = stats["bytes"]
        try:
            _process_candidate(
                candidate,
                known_paths=known_paths,
                seen_hashes=seen_hashes,
                stats=stats,
                errors=errors,
                workers=workers,
                dry_run=dry_run,
                force=force,
                limit=limit,
                record_local_dir=record_local_dir,
            )
        except Exception as e:  # noqa: BLE001
            # Обрыв сети до S3 или битый файл не должны обнулять многочасовой
            # прогон: помечаем модель как неудачную и идём дальше.
            stats["failed"] += 1
            errors.append(f"{candidate.key}: {e}")
            log.warning("Skipping %s after error: %s", candidate.key, e)
        progress.step(size=stats["bytes"] - before)

    progress.report()
    stats["errors"] = errors[:50]
    return stats


def _process_candidate(
    candidate: ModelCandidate,
    *,
    known_paths: set[str],
    seen_hashes: set[str],
    stats: dict,
    errors: list[str],
    workers: int,
    dry_run: bool,
    force: bool,
    limit: int | None,
    record_local_dir: bool = True,
) -> None:
    """Заливает одну модель. Исключения ловит вызывающий и продолжает обход."""
    key = candidate.key
    if key in known_paths and not force:
        stats["skipped_existing"] += 1
        return

    try:
        content_hash = sha256_file(candidate.model_file)
    except OSError as e:
        stats["failed"] += 1
        errors.append(f"{key}: hash failed: {e}")
        return

    if content_hash in seen_hashes:
        stats["skipped_duplicate"] += 1
        return
    with db.write_transaction() as conn:
        dupe = db.find_by_content_hash(conn, content_hash)
    if dupe and dupe["path"] != key:
        stats["skipped_duplicate"] += 1
        log.info("Duplicate of %s — skipping %s", dupe["path"], key)
        return
    seen_hashes.add(content_hash)

    if dry_run:
        size_mb = sum(f.stat().st_size for f in candidate.files if f.is_file()) / 1e6
        log.info(
            "[dry-run] %s (%s, %d file(s), %.1f MB)",
            key,
            candidate.category,
            len(candidate.files),
            size_mb,
        )
        stats["uploaded"] += 1
        return

    files, total_bytes, file_errors = _upload_dir(candidate, workers=workers)
    if file_errors:
        stats["failed"] += 1
        errors.extend(file_errors[:5])
        log.warning("Upload incomplete for %s: %s", key, file_errors[0])
        # Хеш освобождаем: при следующем запуске модель должна быть перезалита,
        # иначе неполная папка навсегда останется «уже виденной».
        seen_hashes.discard(content_hash)
        return

    now = time.time()
    values = _asset_values(
        candidate, content_hash=content_hash, now=now, record_local_dir=record_local_dir
    )
    with db.write_transaction() as conn:
        db.upsert_ingested_asset(conn, key, values, now)

    stats["uploaded"] += 1
    stats["files"] += files
    stats["bytes"] += total_bytes
    log.info(
        "%d/%s uploaded %s (%s, %d file(s), %.1f MB)",
        stats["uploaded"],
        limit if limit is not None else "?",
        key,
        candidate.category,
        files,
        total_bytes / 1e6,
    )


# --------------------------------------------------------------------------- #
# Сверка и удаление с диска
# --------------------------------------------------------------------------- #
def _verify_dir_against_index(
    local_dir: Path, dir_key: str, index: dict[str, int]
) -> tuple[bool, list[str]]:
    """То же, что _verify_dir, но по заранее собранному списку хранилища.

    Ради скорости: на тысячах папок поштучные HEAD-запросы дают часы работы,
    здесь же всё сводится к сравнению со словарём в памяти. Гарантия та же —
    каждый локальный файл обязан лежать в хранилище с тем же размером.
    """
    problems: list[str] = []
    if not local_dir.is_dir():
        return False, [f"{local_dir}: папки уже нет на диске"]
    for local in _dir_files(local_dir):
        rel = PurePosixPath(local.relative_to(local_dir).as_posix())
        key = str(PurePosixPath(dir_key) / rel)
        remote_size = index.get(key)
        if remote_size is None:
            problems.append(f"{key}: нет в хранилище")
        elif remote_size != local.stat().st_size:
            problems.append(f"{key}: размер {remote_size} ≠ {local.stat().st_size}")
        if len(problems) >= 10:
            break
    return not problems, problems


def _verify_dir(local_dir: Path, dir_key: str) -> tuple[bool, list[str]]:
    """Все ли файлы папки лежат в хранилище с тем же размером."""
    problems: list[str] = []
    if not local_dir.is_dir():
        return False, [f"{local_dir}: папки уже нет на диске"]
    for local in _dir_files(local_dir):
        rel = PurePosixPath(local.relative_to(local_dir).as_posix())
        key = str(PurePosixPath(dir_key) / rel)
        remote = storage.stat_object(key)
        if remote is None:
            problems.append(f"{key}: нет в хранилище")
        elif remote[0] != local.stat().st_size:
            problems.append(f"{key}: размер {remote[0]} ≠ {local.stat().st_size}")
        if len(problems) >= 10:
            break
    return not problems, problems


def _ingested_dirs(limit: int | None) -> list[tuple[str, str]]:
    """(local_dir, dir_key) уникальных залитых папок."""
    rows, _total = db.search_assets(limit=limit or 1_000_000, offset=0, sort="path")
    seen: dict[str, str] = {}
    for r in rows:
        local_dir, dir_key = r.get("local_dir"), r.get("dir_key")
        if r.get("ingested_at") and local_dir and dir_key:
            seen.setdefault(local_dir, dir_key)
    return list(seen.items())


def run_verify(limit: int | None) -> dict:
    ok = 0
    bad: list[str] = []
    dirs = _ingested_dirs(limit)
    progress = Progress(len(dirs), "Сверка с хранилищем")
    for local_dir, dir_key in dirs:
        good, problems = _verify_dir(Path(local_dir), dir_key)
        if good:
            ok += 1
        else:
            bad.extend(problems[:3])
        progress.step()
    return {"dirs": len(dirs), "verified": ok, "problems": bad[:50]}


def run_purge(limit: int | None, *, confirmed: bool, trash: Path | None) -> dict:
    """Удаляет (или переносит в корзину) папки, полностью подтверждённые в хранилище."""
    dirs = _ingested_dirs(limit)
    stats = {
        "dirs": len(dirs),
        "removed": 0,
        "kept": 0,
        "already_gone": 0,
        "freed_bytes": 0,
    }
    problems: list[str] = []
    progress = Progress(len(dirs), "Очистка диска")

    log.info("Читаю список хранилища для сверки…")
    index = storage.index_objects()
    log.info("В хранилище %d объектов, начинаю очистку", len(index))

    for local_dir, dir_key in dirs:
        path = Path(local_dir)
        if not path.exists():
            # Папку уже удалили прошлым запуском — это норма, а не проблема.
            stats["already_gone"] += 1
            progress.step()
            continue
        good, dir_problems = _verify_dir_against_index(path, dir_key, index)
        if not good:
            stats["kept"] += 1
            problems.extend(dir_problems[:2])
            progress.step()
            continue
        size = sum(f.stat().st_size for f in _dir_files(path))
        if not confirmed:
            log.info("[dry-run] удалить %s (%.1f MB)", path, size / 1e6)
            stats["removed"] += 1
            stats["freed_bytes"] += size
            progress.step(size=size)
            continue
        try:
            if trash is not None:
                dest = trash / dir_key
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(dest))
            else:
                shutil.rmtree(path)
        except OSError as e:
            stats["kept"] += 1
            problems.append(f"{path}: {e}")
            progress.step()
            continue
        stats["removed"] += 1
        stats["freed_bytes"] += size
        progress.step(size=size)

    stats["problems"] = problems[:50]
    return stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.ingest",
        description="Заливка каталога моделей с диска в хранилище",
    )
    p.add_argument("--root", type=Path, default=INGEST_ROOT, help="корень каталога на диске")
    p.add_argument("--limit", type=int, default=None, help="максимум моделей за запуск")
    p.add_argument("--workers", type=int, default=INGEST_WORKERS, help="параллельных заливок")
    p.add_argument(
        "--preview-match",
        choices=("strict", "any"),
        default=INGEST_PREVIEW_MATCH,
        help="strict — только одноимённое превью; any — подобрать картинку из папки",
    )
    p.add_argument(
        "--shards", type=int, default=1,
        help="на сколько частей разделить каталог (для нескольких процессов)",
    )
    p.add_argument(
        "--shard", type=int, default=0,
        help="номер части этого процесса, от 0 до shards-1",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="показать, что будет залито (ничего не меняет)")

    up = sub.add_parser("upload", help="залить модели в хранилище и зарегистрировать в БД")
    up.add_argument("--dry-run", action="store_true", help="только показать план")
    up.add_argument("--force", action="store_true", help="перезалить уже известные модели")

    sub.add_parser("verify", help="сверить залитое с хранилищем")

    pg = sub.add_parser("purge", help="удалить с диска то, что подтверждено в хранилище")
    pg.add_argument("--yes", action="store_true", help="действительно удалять (иначе dry-run)")
    pg.add_argument("--trash", type=Path, default=None, help="переносить сюда вместо удаления")
    return p


def main(argv: list[str] | None = None) -> int:
    # Консоль Windows по умолчанию cp1252 — кириллица в выводе иначе роняет запуск.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = _build_parser().parse_args(argv)

    root: Path = args.root.resolve()
    if args.command in {"scan", "upload"} and not root.is_dir():
        log.error("Каталог не найден: %s", root)
        return 2

    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()

    if args.command == "scan":
        candidates = iter_candidates(root, preview_match=args.preview_match)
        by_category: dict[str, int] = {}
        total_bytes = 0
        for c in candidates:
            by_category[c.category] = by_category.get(c.category, 0) + 1
            total_bytes += sum(f.stat().st_size for f in c.files if f.is_file())
        print(f"Моделей с превью: {len(candidates)}")
        print(f"Суммарный объём папок: {total_bytes / 1e9:.1f} ГБ")
        print("По категориям:")
        for cat, n in sorted(by_category.items(), key=lambda kv: -kv[1]):
            print(f"  {cat:<14} {n}")
        return 0

    if args.command == "upload":
        if not (0 <= args.shard < args.shards):
            log.error("--shard должен быть в диапазоне 0..%d", args.shards - 1)
            return 2
        stats = run_upload(
            root,
            limit=args.limit,
            workers=args.workers,
            dry_run=args.dry_run,
            force=args.force,
            shard=args.shard,
            shards=args.shards,
            preview_match=args.preview_match,
        )
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0 if not stats.get("failed") else 1

    if args.command == "verify":
        stats = run_verify(args.limit)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0 if not stats.get("problems") else 1

    if args.command == "purge":
        if not args.yes:
            log.warning("Без --yes это только предпросмотр, ничего не удаляется")
        stats = run_purge(args.limit, confirmed=args.yes, trash=args.trash)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
