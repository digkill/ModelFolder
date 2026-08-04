"""Поиск .blend с тем же базовым именем, что и у модели (рядом на диске или в том же zip)."""

from __future__ import annotations

import zipfile
from pathlib import Path, PurePosixPath

from app import storage
from app.vpath import ZIP_SEP, is_safe_zip_member, split_vpath


def _norm_zip_name(n: str) -> str:
    return n.replace("\\", "/").rstrip("/")


def find_blend_companion(model_rel: str) -> str | None:
    """
    Возвращает относительный путь к .blend (как в каталоге) или None.
    Для диска: та же папка, stem.blend.
    Для zip: тот же архив, в той же внутренней папке stem.blend.
    """
    if ZIP_SEP in model_rel:
        try:
            zip_rel, member = split_vpath(model_rel)
        except ValueError:
            return None
        stem = PurePosixPath(member).stem
        if not stem:
            return None
        if not is_safe_zip_member(member):
            return None
        inner = PurePosixPath(member)
        inner_dir = inner.parent
        if inner_dir.as_posix() in (".", ""):
            blend_inner = f"{stem}.blend"
        else:
            blend_inner = f"{inner_dir.as_posix()}/{stem}.blend"
        if not is_safe_zip_member(blend_inner):
            return None

        try:
            zip_abs = storage.local_path(zip_rel)
        except (FileNotFoundError, ValueError, OSError):
            return None
        if zip_abs.suffix.lower() != ".zip":
            return None

        try:
            with zipfile.ZipFile(zip_abs, "r") as zf:
                want = _norm_zip_name(blend_inner)
                for raw in zf.namelist():
                    if raw.endswith("/"):
                        continue
                    if _norm_zip_name(raw) == want:
                        return f"{zip_rel}{ZIP_SEP}{_norm_zip_name(raw)}"
        except (zipfile.BadZipFile, OSError):
            return None
        return None

    stem = PurePosixPath(model_rel).stem
    if not stem:
        return None

    rel_path = PurePosixPath(model_rel)
    parent = rel_path.parent
    if parent.as_posix() in (".", ""):
        candidate = f"{stem}.blend"
    else:
        candidate = f"{parent.as_posix()}/{stem}.blend"

    return candidate if storage.object_exists(candidate) else None
