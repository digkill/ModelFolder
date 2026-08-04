"""Virtual paths: disk/zip.glb or disk/archive.zip::member/inside/model.glb"""

from __future__ import annotations

import zipfile
from pathlib import Path

ZIP_SEP = "::"


def split_vpath(rel: str) -> tuple[str, str | None]:
    if ZIP_SEP not in rel:
        return rel, None
    left, right = rel.split(ZIP_SEP, 1)
    if not left.strip() or not right.strip():
        raise ValueError("invalid virtual path")
    return left, right


def is_safe_zip_member(name: str) -> bool:
    if not name or name.startswith("/"):
        return False
    parts = Path(name).parts
    if ".." in parts:
        return False
    if parts and parts[0].startswith("\\"):
        return False
    return True


def iter_zip_model_entries(
    zip_abs: Path,
    zip_rel: str,
    model_exts: set[str],
    zip_mtime: float,
) -> list[tuple[str, str, str, int, float]]:
    out: list[tuple[str, str, str, int, float]] = []
    try:
        with zipfile.ZipFile(zip_abs, "r") as zf:
            for info in zf.infolist():
                if info.filename.endswith("/"):
                    continue
                name = info.filename
                if not is_safe_zip_member(name):
                    continue
                ext = Path(name).suffix.lower()
                if ext not in model_exts:
                    continue
                virt = f"{zip_rel}{ZIP_SEP}{name.replace(chr(92), '/')}"
                base = Path(name).name
                out.append(
                    (
                        virt,
                        base,
                        ext.lstrip("."),
                        int(info.file_size),
                        float(zip_mtime),
                    )
                )
    except (zipfile.BadZipFile, OSError):
        return []
    return out
