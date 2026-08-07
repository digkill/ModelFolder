"""Контрольная сумма и метаданные модели без полной загрузки геометрии.

Каталог измеряется сотнями гигабайт, поэтому метаданные читаются из заголовка
формата, а не через trimesh: у glTF/GLB вся нужная статистика (число вершин,
треугольников, материалов, анимаций, текстур, наличие скелета, габариты) лежит
в JSON-описании — грузить буферы для этого не нужно.

Форматы: .gltf, .glb — полностью; .obj — подсчётом строк; остальные (.fbx, .blend,
.usdz, .stl) — только размер и хеш.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
from pathlib import Path

log = logging.getLogger(__name__)

_HASH_CHUNK = 4 * 1024 * 1024
_GLB_MAGIC = 0x46546C67  # "glTF"
_GLB_JSON_CHUNK = 0x4E4F534A  # "JSON"


def sha256_file(path: Path) -> str:
    """sha256 файла потоком (файлы бывают по несколько гигабайт)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _read_glb_json(path: Path) -> dict | None:
    with open(path, "rb") as f:
        header = f.read(12)
        if len(header) < 12:
            return None
        magic, _version, _length = struct.unpack("<III", header)
        if magic != _GLB_MAGIC:
            return None
        chunk_header = f.read(8)
        if len(chunk_header) < 8:
            return None
        chunk_len, chunk_type = struct.unpack("<II", chunk_header)
        if chunk_type != _GLB_JSON_CHUNK:
            return None
        raw = f.read(chunk_len)
    return json.loads(raw.decode("utf-8", errors="replace"))


def _read_gltf_json(path: Path) -> dict | None:
    with open(path, "rb") as f:
        raw = f.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def _gltf_stats(doc: dict) -> dict:
    accessors = doc.get("accessors") or []
    meshes = doc.get("meshes") or []

    vertices = 0
    faces = 0
    bbox_min: list[float] | None = None
    bbox_max: list[float] | None = None
    primitives = 0

    def accessor_count(index) -> int:
        if not isinstance(index, int) or not (0 <= index < len(accessors)):
            return 0
        return int((accessors[index] or {}).get("count") or 0)

    for mesh in meshes:
        for prim in (mesh or {}).get("primitives") or []:
            primitives += 1
            attrs = prim.get("attributes") or {}
            pos_index = attrs.get("POSITION")
            vertices += accessor_count(pos_index)
            if "indices" in prim:
                faces += accessor_count(prim.get("indices")) // 3
            else:
                faces += accessor_count(pos_index) // 3
            # Габариты: POSITION-аксессор обязан нести min/max по спецификации glTF.
            if isinstance(pos_index, int) and 0 <= pos_index < len(accessors):
                acc = accessors[pos_index] or {}
                lo, hi = acc.get("min"), acc.get("max")
                if isinstance(lo, list) and isinstance(hi, list) and len(lo) >= 3 and len(hi) >= 3:
                    lo3 = [float(x) for x in lo[:3]]
                    hi3 = [float(x) for x in hi[:3]]
                    bbox_min = lo3 if bbox_min is None else [min(a, b) for a, b in zip(bbox_min, lo3)]
                    bbox_max = hi3 if bbox_max is None else [max(a, b) for a, b in zip(bbox_max, hi3)]

    bbox = None
    if bbox_min and bbox_max:
        bbox = [round(hi - lo, 6) for lo, hi in zip(bbox_min, bbox_max)]

    asset = doc.get("asset") or {}
    return {
        "vertex_count": vertices or None,
        "face_count": faces or None,
        "mesh_count": len(meshes) or None,
        "primitive_count": primitives or None,
        "material_count": len(doc.get("materials") or []) or None,
        "texture_count": len(doc.get("images") or []) or None,
        "animation_count": len(doc.get("animations") or []) or None,
        "node_count": len(doc.get("nodes") or []) or None,
        "has_rig": bool(doc.get("skins")),
        "bbox": bbox,
        "generator": (asset.get("generator") or None),
        "gltf_version": (asset.get("version") or None),
        "extensions": sorted(doc.get("extensionsUsed") or []) or None,
    }


def _obj_stats(path: Path) -> dict:
    vertices = 0
    faces = 0
    materials: set[str] = set()
    with open(path, "rb") as f:
        for line in f:
            if line.startswith(b"v "):
                vertices += 1
            elif line.startswith(b"f "):
                # Полигон из N вершин = N-2 треугольника.
                faces += max(1, len(line.split()) - 3)
            elif line.startswith(b"usemtl "):
                materials.add(line[7:].strip().decode("utf-8", "replace"))
    return {
        "vertex_count": vertices or None,
        "face_count": faces or None,
        "mesh_count": None,
        "material_count": len(materials) or None,
        "texture_count": None,
        "animation_count": None,
        "has_rig": False,
        "bbox": None,
    }


def extract_metadata(path: Path) -> dict:
    """Метаданные модели. Никогда не бросает — при ошибке вернёт пустой набор."""
    suffix = path.suffix.lower()
    empty = {
        "vertex_count": None,
        "face_count": None,
        "mesh_count": None,
        "material_count": None,
        "texture_count": None,
        "animation_count": None,
        "has_rig": None,
        "bbox": None,
    }
    try:
        if suffix == ".glb":
            doc = _read_glb_json(path)
            return _gltf_stats(doc) if doc else dict(empty)
        if suffix == ".gltf":
            doc = _read_gltf_json(path)
            return _gltf_stats(doc) if doc else dict(empty)
        if suffix == ".obj":
            return _obj_stats(path)
    except Exception as e:  # noqa: BLE001 — метаданные не должны ронять ingest
        log.debug("Metadata extraction failed for %s: %s", path, e)
        return dict(empty)
    return dict(empty)


def complexity_bucket(face_count: int | None) -> str | None:
    """Грубая шкала сложности — из неё выводится тег low-poly / high-poly."""
    if not face_count:
        return None
    if face_count < 2_000:
        return "very-low-poly"
    if face_count < 20_000:
        return "low-poly"
    if face_count < 150_000:
        return "mid-poly"
    return "high-poly"
