#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from app.paths import MODEL_EXTENSIONS, MODELS_ROOT
except Exception:
    MODEL_EXTENSIONS = {".fbx", ".glb", ".gltf", ".usdz", ".flb"}
    MODELS_ROOT = Path(
        os.environ.get("MODELS_DIR", PROJECT_ROOT / "models")
    ).resolve()

DEFAULT_PREFERRED_EXTENSIONS = [
    ".glb",
    ".gltf",
    ".fbx",
    ".usdz",
    ".flb",
]

_COPY_SUFFIX_RE = re.compile(
    r"""
    (?:
        [\s._-]*
        (?:
            copy|копия|
            \(\d+\)|\[\d+\]|
            v\d+|ver\d+|version\d+
        )
    )+$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_SEPARATORS_RE = re.compile(r"[_\W]+", re.UNICODE)


@dataclass(frozen=True)
class ModelFile:
    path: Path
    ext: str
    size: int
    mtime: float
    group_key: tuple[str, str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ищет похожие файлы моделей, которые отличаются в основном расширением "
            "и незначительными суффиксами, и удаляет лишние дубликаты."
        )
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=str(MODELS_ROOT),
        help="Папка с моделями. По умолчанию берётся MODELS_DIR или ./models.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реально удалить найденные дубликаты. Без флага скрипт только показывает план.",
    )
    parser.add_argument(
        "--extensions",
        default=",".join(sorted(MODEL_EXTENSIONS)),
        help="Список расширений через запятую, например .glb,.gltf,.fbx,.usdz.",
    )
    parser.add_argument(
        "--prefer-ext",
        default=",".join(DEFAULT_PREFERRED_EXTENSIONS),
        help=(
            "Приоритет расширений через запятую. Первый формат предпочтительнее и будет "
            "оставлен при прочих равных."
        ),
    )
    parser.add_argument(
        "--cross-directories",
        action="store_true",
        help="Считать дубликатами файлы из разных папок. По умолчанию сравнение только внутри одной папки.",
    )
    return parser.parse_args()


def parse_extensions(value: str) -> list[str]:
    out: list[str] = []
    for part in value.split(","):
        ext = part.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        out.append(ext)
    if not out:
        raise ValueError("Список расширений пуст.")
    return out


def normalize_stem(stem: str) -> str:
    normalized = unicodedata.normalize("NFKC", stem).strip().lower()
    normalized = _COPY_SUFFIX_RE.sub("", normalized)
    normalized = _SEPARATORS_RE.sub(" ", normalized)
    return " ".join(normalized.split())


def build_group_key(path: Path, *, cross_directories: bool) -> tuple[str, str]:
    folder_key = "" if cross_directories else str(path.parent.resolve()).lower()
    return folder_key, normalize_stem(path.stem)


def collect_model_files(
    root: Path,
    *,
    allowed_extensions: set[str],
    cross_directories: bool,
) -> list[ModelFile]:
    items: list[ModelFile] = []
    for path in root.rglob("*"):
        if path.name.startswith("._"):
            continue
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in allowed_extensions:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        group_key = build_group_key(path, cross_directories=cross_directories)
        if not group_key[1]:
            continue
        items.append(
            ModelFile(
                path=path,
                ext=ext,
                size=int(stat.st_size),
                mtime=float(stat.st_mtime),
                group_key=group_key,
            )
        )
    return items


def choose_keeper(files: list[ModelFile], ext_priority: dict[str, int]) -> ModelFile:
    return min(
        files,
        key=lambda item: (
            ext_priority.get(item.ext, len(ext_priority)),
            -item.size,
            -item.mtime,
            str(item.path).lower(),
        ),
    )


def group_duplicates(files: list[ModelFile], ext_priority: dict[str, int]) -> list[tuple[ModelFile, list[ModelFile]]]:
    grouped: dict[tuple[str, str], list[ModelFile]] = {}
    for item in files:
        grouped.setdefault(item.group_key, []).append(item)

    duplicates: list[tuple[ModelFile, list[ModelFile]]] = []
    for group in grouped.values():
        if len(group) < 2:
            continue
        exts = {item.ext for item in group}
        if len(exts) < 2:
            continue
        keeper = choose_keeper(group, ext_priority)
        to_delete = sorted(
            (item for item in group if item.path != keeper.path),
            key=lambda item: str(item.path).lower(),
        )
        duplicates.append((keeper, to_delete))

    duplicates.sort(key=lambda pair: str(pair[0].path).lower())
    return duplicates


def render_report(duplicates: list[tuple[ModelFile, list[ModelFile]]], *, root: Path) -> None:
    total_delete = sum(len(items) for _, items in duplicates)
    total_reclaim = sum(item.size for _, items in duplicates for item in items)
    print(f"Папка: {root}")
    print(f"Групп дубликатов: {len(duplicates)}")
    print(f"Файлов к удалению: {total_delete}")
    print(f"Освободится: {total_reclaim / (1024 * 1024):.2f} MiB")
    if not duplicates:
        return

    for index, (keeper, to_delete) in enumerate(duplicates, start=1):
        print()
        print(f"[{index}] Оставить: {keeper.path}")
        for item in to_delete:
            print(f"    удалить: {item.path}")


def delete_duplicates(duplicates: list[tuple[ModelFile, list[ModelFile]]]) -> tuple[int, int]:
    removed = 0
    reclaimed = 0
    for _, items in duplicates:
        for item in items:
            try:
                item.path.unlink()
            except OSError as exc:
                print(f"Не удалось удалить {item.path}: {exc}", file=sys.stderr)
                continue
            removed += 1
            reclaimed += item.size
    return removed, reclaimed


def main() -> int:
    args = parse_args()

    try:
        allowed_extensions = set(parse_extensions(args.extensions))
        preferred_extensions = parse_extensions(args.prefer_ext)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    root = Path(args.target).expanduser().resolve()
    if not root.exists():
        print(f"Папка не найдена: {root}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"Это не папка: {root}", file=sys.stderr)
        return 2

    ext_priority = {ext: index for index, ext in enumerate(preferred_extensions)}
    files = collect_model_files(
        root,
        allowed_extensions=allowed_extensions,
        cross_directories=args.cross_directories,
    )
    duplicates = group_duplicates(files, ext_priority)
    render_report(duplicates, root=root)

    if not args.apply:
        print()
        print("Режим предпросмотра. Для удаления запустите с флагом --apply.")
        return 0

    removed, reclaimed = delete_duplicates(duplicates)
    print()
    print(f"Удалено файлов: {removed}")
    print(f"Освобождено: {reclaimed / (1024 * 1024):.2f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
