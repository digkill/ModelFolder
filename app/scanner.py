import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

import app.db as db
from app import storage
from app.paths import (
    MODEL_EXTENSIONS,
    MODELS_ROOT,
    PREVIEW_BATCH_PER_CYCLE,
    PREVIEW_EXTENSIONS,
    PREVIEW_PIXEL_SIZE,
    PREVIEW_SUBPROCESS_TIMEOUT_SEC,
    PREVIEWS_DIR,
    RUN_SCANNER,
    SCAN_INTERVAL_SEC,
)
from app.blend_companion import find_blend_companion
from app.preview_render import preview_basename_for
from app.vpath import ZIP_SEP, is_safe_zip_member, iter_zip_model_entries, split_vpath

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger(__name__)

_stop = threading.Event()
_thread: threading.Thread | None = None


def _drop_from_vector_index(path: str) -> None:
    """Best-effort удаление модели из Qdrant при удалении из каталога."""
    try:
        from app import vector_store

        vector_store.delete_model(path)
    except Exception as e:  # Qdrant может быть недоступен — сканер не должен падать
        log.debug("Vector index cleanup skipped for %s: %s", path, e)

COMPANION_PREVIEW_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
COMPANION_PREVIEW_WORDS = {
    "cover",
    "image",
    "poster",
    "preview",
    "render",
    "screenshot",
    "thumb",
    "thumbnail",
}
COMPANION_PREVIEW_DIRS = {
    "cover",
    "covers",
    "image",
    "images",
    "poster",
    "posters",
    "preview",
    "previews",
    "render",
    "renders",
    "screenshot",
    "screenshots",
    "thumb",
    "thumbnail",
    "thumbnails",
    "превью",
    "скриншот",
    "скриншоты",
}
TEXTURE_WORDS = {
    "albedo",
    "ao",
    "basecolor",
    "bump",
    "diff",
    "diffuse",
    "metallic",
    "normal",
    "opacity",
    "roughness",
    "spec",
    "texture",
}


def _ext_token(suffix: str) -> str:
    return suffix.lower().lstrip(".")


def _wants_preview(ext_with_dot: str) -> bool:
    return ext_with_dot.lower() in PREVIEW_EXTENSIONS


def _initial_preview_status(ext_with_dot: str) -> str:
    return "pending" if _wants_preview(ext_with_dot) else "skip"


def _name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9а-яё]+", "", value.casefold())


def _find_companion_preview(model_path: Path) -> Path | None:
    """
    Ищет готовое превью рядом с моделью: same-stem, имя с названием модели
    или явные preview/thumbnail/render-файлы.
    """
    folder = model_path.parent
    if not folder.is_dir():
        return None

    model_key = _name_key(model_path.stem)
    if not model_key:
        return None

    candidates: list[tuple[int, int, str, Path]] = []
    try:
        search_dirs = [(0, folder)]
        for child in folder.iterdir():
            if child.is_dir() and child.name.casefold() in COMPANION_PREVIEW_DIRS:
                search_dirs.append((1, child))
    except OSError:
        return None

    for dir_priority, search_dir in search_dirs:
        try:
            children = list(search_dir.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_file() or child.name.startswith("._"):
                    continue
            except OSError:
                continue
            ext = child.suffix.lower()
            if ext not in COMPANION_PREVIEW_EXTENSIONS:
                continue

            stem_key = _name_key(child.stem)
            if stem_key == model_key:
                priority = 0
            elif model_key in stem_key or stem_key in model_key:
                if any(word in child.stem.casefold() for word in TEXTURE_WORDS):
                    continue
                priority = 1
            elif child.stem.casefold() in COMPANION_PREVIEW_WORDS:
                priority = 2
            elif any(word in child.stem.casefold() for word in COMPANION_PREVIEW_WORDS):
                priority = 3
            else:
                continue
            candidates.append((priority, dir_priority, child.name.casefold(), child))

    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][3] if candidates else None


def _copy_companion_preview(src: Path, out_path: Path) -> tuple[bool, str | None]:
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
        return True, None
    except Exception as e:
        return False, f"companion preview failed: {e}"


def _try_adopt_companion_preview(
    conn,
    *,
    path: str,
    mtime: float,
    old_preview: str | None,
    now: float,
) -> bool:
    if ZIP_SEP in path:
        return False
    # Поиск картинки-компаньона рядом с моделью — эвристика по соседним файлам на диске.
    # Для S3 пропускаем: превью генерируется рендером.
    if storage.is_s3():
        return False
    abs_path = (MODELS_ROOT / path).resolve()
    try:
        abs_path.relative_to(MODELS_ROOT.resolve())
    except ValueError:
        return False
    if not abs_path.is_file():
        return False

    companion = _find_companion_preview(abs_path)
    if companion is None:
        return False

    basename = preview_basename_for(path, mtime)
    out_path = PREVIEWS_DIR / basename
    ok, err = _copy_companion_preview(companion, out_path)
    if not ok:
        log.warning("Existing preview failed for %s (%s): %s", path, companion, err)
        return False
    if old_preview and old_preview != basename:
        db.unlink_preview_file(old_preview)
    db.delete_tags_for_source(conn, path, "openai")
    db.set_preview_result(
        conn,
        path,
        preview_file=basename,
        status="ok",
        error=None,
        now=now,
        preview_source="companion",
    )
    return True


def _iter_model_files() -> list[tuple[str, str, str, int, float]]:
    """Обходит хранилище (local/S3): отдельные модели + модели внутри .zip."""
    out: list[tuple[str, str, str, int, float]] = []
    for rel, size, mtime in storage.iter_objects():
        name = PurePosixPath(rel).name
        ext = PurePosixPath(rel).suffix.lower()
        if ext == ".zip":
            try:
                zip_local = storage.local_path(rel)
            except (FileNotFoundError, ValueError, OSError):
                log.debug("Skipping unreadable zip: %s", rel)
                continue
            out.extend(iter_zip_model_entries(zip_local, rel, MODEL_EXTENSIONS, mtime))
            continue
        if ext not in MODEL_EXTENSIONS:
            continue
        out.append((rel, name, _ext_token(ext), int(size), mtime))
    out.sort(key=lambda x: x[0].lower())
    return out


def sync_filesystem_to_db() -> None:
    disk = _iter_model_files()
    disk_paths = {d[0] for d in disk}
    now = time.time()

    with db.write_transaction() as conn:
        # Все существующие модели одним запросом — без per-row SELECT в цикле
        # (критично для удалённого MySQL: 20k round-trip'ов иначе).
        index = db.load_existing_index(conn)
        existing = set(index.keys())
        removed = existing - disk_paths
        # Защита от катастрофы: если листинг пуст (например отвалился внешний диск/сеть),
        # но в БД есть модели — это почти наверняка временный сбой источника, а не
        # реальное удаление 20k файлов. Не трогаем каталог (и дорогие превью/описания).
        if not disk_paths and existing:
            log.warning(
                "Scan returned 0 files while DB has %d assets — источник недоступен? "
                "Пропускаю синхронизацию, чтобы не удалить каталог.",
                len(existing),
            )
            return
        for p in removed:
            prev = db.delete_asset(conn, p)
            db.unlink_preview_file(prev)

        new_rows: list[tuple] = []
        for path, name, ext, size, mtime in disk:
            blend = find_blend_companion(path)
            row = index.get(path)
            if row is None:
                # Новые — пакетно (executemany) в конце. Компаньон-превью подхватится
                # в очереди превью (там компаньон приоритетнее рендера) или на след. скане.
                new_rows.append(
                    (
                        path,
                        name,
                        ext,
                        int(size),
                        mtime,
                        None,  # preview_file
                        _initial_preview_status(f".{ext}"),
                        None,  # preview_error
                        now,
                        now,
                        blend,
                    )
                )
                continue
            if int(row["size"]) == size and float(row["mtime"]) == mtime:
                if row.get("blend_path") != blend:
                    db.update_blend_path_only(conn, path, blend, now)
                if row.get("preview_source") != "companion" and _wants_preview(f".{ext}"):
                    _try_adopt_companion_preview(
                        conn,
                        path=path,
                        mtime=mtime,
                        old_preview=row.get("preview_file"),
                        now=now,
                    )
                continue
            old_preview = row["preview_file"]
            st = _initial_preview_status(f".{ext}")
            db.unlink_preview_file(old_preview)
            db.delete_tags_for_source(conn, path, "openai")
            db.update_asset_meta(
                conn,
                path=path,
                name=name,
                ext=ext,
                size=size,
                mtime=mtime,
                preview_status=st,
                clear_preview=True,
                now=now,
                blend_path=blend,
            )

        db.insert_assets_bulk(conn, new_rows)
        db.set_last_scan_time(conn, now)

    for p in removed:
        _drop_from_vector_index(p)


def _extract_zip_member_for_preview(zip_abs: Path, member: str) -> str | None:
    if not is_safe_zip_member(member):
        return None
    suffix = Path(member).suffix or ".bin"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="mgzip-")
    os.close(fd)
    try:
        with zipfile.ZipFile(zip_abs, "r") as zf:
            with zf.open(member, "r") as src:
                with open(tmp_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        return tmp_path
    except (OSError, zipfile.BadZipFile, KeyError):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None


def process_one_preview() -> bool:
    pending = db.fetch_pending_preview_paths(limit=1)
    if not pending:
        return False
    path = pending[0]
    row = db.fetch_asset_for_preview(path)
    if not row:
        return True

    abs_model: str | None = None
    tmp_extracted: str | None = None

    if ZIP_SEP in path:
        try:
            disk_rel, member = split_vpath(path)
        except ValueError:
            with db.write_transaction() as conn:
                db.set_preview_result(
                    conn, path, preview_file=None, status="error", error="bad path", now=time.time()
                )
            return True
    else:
        disk_rel, member = path, None

    if member is None:
        try:
            abs_path = storage.local_path(disk_rel)
        except ValueError:
            with db.write_transaction() as conn:
                db.set_preview_result(
                    conn, path, preview_file=None, status="error", error="bad path", now=time.time()
                )
            return True
        except (FileNotFoundError, OSError):
            with db.write_transaction() as conn:
                db.set_preview_result(
                    conn,
                    path,
                    preview_file=None,
                    status="error",
                    error="file missing",
                    now=time.time(),
                )
            return True
        abs_model = str(abs_path)
    else:
        try:
            zip_abs = storage.local_path(disk_rel)
        except ValueError:
            with db.write_transaction() as conn:
                db.set_preview_result(
                    conn, path, preview_file=None, status="error", error="bad zip path", now=time.time()
                )
            return True
        except (FileNotFoundError, OSError):
            zip_abs = None
        if zip_abs is None or zip_abs.suffix.lower() != ".zip":
            with db.write_transaction() as conn:
                db.set_preview_result(
                    conn,
                    path,
                    preview_file=None,
                    status="error",
                    error="zip missing",
                    now=time.time(),
                )
            return True
        tmp_extracted = _extract_zip_member_for_preview(zip_abs, member)
        if not tmp_extracted:
            with db.write_transaction() as conn:
                db.set_preview_result(
                    conn,
                    path,
                    preview_file=None,
                    status="error",
                    error="zip extract failed",
                    now=time.time(),
                )
            return True
        abs_model = tmp_extracted

    basename = preview_basename_for(path, row["mtime"])
    out_path = PREVIEWS_DIR / basename
    if out_path.is_file():
        out_path.unlink()
    now = time.time()

    if member is None:
        companion = _find_companion_preview(Path(abs_model))
        if companion is not None:
            ok, err = _copy_companion_preview(companion, out_path)
            if ok:
                with db.write_transaction() as conn:
                    db.set_preview_result(
                        conn,
                        path,
                        preview_file=basename,
                        status="ok",
                        error=None,
                        now=now,
                        preview_source="companion",
                    )
                return True
            log.warning("Existing preview failed for %s (%s): %s", path, companion, err)

    try:
        cp = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.preview_worker",
                abs_model,
                str(out_path),
            ],
            capture_output=True,
            text=True,
            timeout=PREVIEW_SUBPROCESS_TIMEOUT_SEC,
            cwd=str(_PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        ok, err = False, f"preview timeout ({PREVIEW_SUBPROCESS_TIMEOUT_SEC}s)"
    else:
        ok = cp.returncode == 0
        err = None if ok else (cp.stderr or cp.stdout or "renderer failed").strip()[:500]
    finally:
        if tmp_extracted:
            try:
                os.unlink(tmp_extracted)
            except OSError:
                pass

    with db.write_transaction() as conn:
        if ok:
            db.set_preview_result(
                conn,
                path,
                preview_file=basename,
                status="ok",
                error=None,
                now=now,
                preview_source="generated",
            )
        else:
            db.set_preview_result(
                conn, path, preview_file=None, status="error", error=err, now=now
            )
    return True


def run_scan_cycle() -> None:
    try:
        sync_filesystem_to_db()
    except Exception:
        log.exception("Catalog sync failed")
        return
    safety = 0
    while safety < PREVIEW_BATCH_PER_CYCLE:
        safety += 1
        if not process_one_preview():
            break


def _loop() -> None:
    run_scan_cycle()
    while not _stop.wait(SCAN_INTERVAL_SEC):
        run_scan_cycle()


def start_background_scanner() -> None:
    global _thread
    if not RUN_SCANNER:
        log.info("Background scanner disabled (RUN_SCANNER=0)")
        return
    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    _stop.clear()
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="model-scanner", daemon=True)
    _thread.start()


def stop_background_scanner() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5.0)
