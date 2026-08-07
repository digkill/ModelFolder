"""Общая логика выдачи моделей для UI и внешнего API."""

from __future__ import annotations

import json
from urllib.parse import quote

import app.db as db
from app.launch_groups import GroupFilters, LaunchGroup
from app.model_meta import complexity_bucket
from app.paths import MODELS_ROOT
from app.taxonomy import split_by_facet
from app.vpath import ZIP_SEP


def _split_query_tags(s: str | None) -> list[str] | None:
    if not s or not str(s).strip():
        return None
    from app.tag_normalize import normalize_tag_list

    return normalize_tag_list([x.strip() for x in str(s).split(",")])


def _abs_url(base_url: str | None, path: str) -> str | None:
    if not base_url:
        return None
    base = base_url.rstrip("/")
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}"


def _view_path(relative: str) -> str:
    if ZIP_SEP in relative:
        left, right = relative.split(ZIP_SEP, 1)
        disk = "/".join(quote(part, safe="") for part in left.split("/"))
        inner = "/".join(quote(part, safe="") for part in right.split("/"))
        return f"/files/{disk}::{inner}"
    enc = "/".join(quote(part, safe="") for part in relative.split("/"))
    return f"/files/{enc}"


def _db_bool(value) -> bool | None:
    if value is None:
        return None
    return bool(int(value))


def _json_list(value) -> list[str]:
    if not value:
        return []
    try:
        raw = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x).strip()]


def _matches_filters(row: dict, filters: GroupFilters) -> bool:
    path = str(row["path"])
    if filters.path_prefix and not path.startswith(filters.path_prefix):
        return False
    if filters.ext and str(row.get("ext", "")).lower() not in filters.ext:
        return False
    if filters.name_contains:
        needle = filters.name_contains.casefold()
        hay = f"{row.get('name', '')} {path}".casefold()
        if needle not in hay:
            return False
    return True


def filters_from_query(
    *,
    tags: str | None = None,
    tags_all: str | None = None,
    path_prefix: str | None = None,
    ext: str | None = None,
    name_contains: str | None = None,
) -> GroupFilters:
    ext_list = tuple(
        x.lower().lstrip(".")
        for x in (ext or "").split(",")
        if x.strip()
    )
    return GroupFilters(
        tags=tuple(_split_query_tags(tags) or ()),
        tags_all=tuple(_split_query_tags(tags_all) or ()),
        path_prefix=(path_prefix or "").strip().replace("\\", "/") or None,
        ext=ext_list,
        name_contains=(name_contains or "").strip() or None,
    )


def _preview_url(row: dict) -> str | None:
    """Ссылка на превью.

    Идёт через API, а не напрямую в /previews: PNG-файл лежит на машине, где
    работал ingest, а на других инстансах есть только оригинал в хранилище —
    эндпоинт сам выберет доступный источник.
    """
    if row.get("preview_status") == "ok" and row.get("preview_file"):
        return f"/api/preview?path={quote(str(row['path']), safe='')}"
    if row.get("preview_key"):
        return f"/api/preview?path={quote(str(row['path']), safe='')}"
    return None


def build_item(row: dict, tag_rows: list[dict], base_url: str | None = None) -> dict:
    """Единое представление модели для всех эндпоинтов каталога."""
    path = str(row["path"])
    tag_strings = [x["tag"] for x in tag_rows]
    blend_path = row.get("blend_path")
    view_rel = _view_path(path)
    download_rel = f"/api/file?path={quote(path, safe='')}"
    preview_rel = _preview_url(row)

    item = {
        "name": row["name"],
        "path": path,
        "ext": row["ext"],
        "size": int(row["size"]),
        "modified": int(row["mtime"]),
        "category": row.get("category"),
        "collection": row.get("collection"),
        "source_url": row.get("source_url"),
        "content_hash": row.get("content_hash"),
        "preview_url": preview_rel,
        "preview_status": row.get("preview_status"),
        "preview_source": row.get("preview_source"),
        "in_zip": ZIP_SEP in path,
        "description": row.get("description") or None,
        "tags": tag_rows,
        "tag_list": tag_strings,
        "tag_facets": split_by_facet(tag_strings),
        "age_rating": row.get("age_rating"),
        "kid_friendly": _db_bool(row.get("kid_friendly")),
        "nsfw": _db_bool(row.get("nsfw")),
        # Флаги верхнего уровня: по ним фильтрует UI, поэтому дублируем из geometry.
        "animated": bool(row.get("animation_count") or 0),
        "rigged": bool(_db_bool(row.get("has_rig"))),
        "geometry": {
            "vertices": row.get("vertex_count"),
            "faces": row.get("face_count"),
            "meshes": row.get("mesh_count"),
            "materials": row.get("material_count"),
            "textures": row.get("texture_count"),
            "animations": row.get("animation_count"),
            "rigged": _db_bool(row.get("has_rig")),
            "bbox": [row.get("bbox_x"), row.get("bbox_y"), row.get("bbox_z")],
            "complexity": complexity_bucket(row.get("face_count")),
        },
        "content": {
            "adult": _db_bool(row.get("content_adult")),
            "nudity": _db_bool(row.get("content_nudity")),
            "violence": _db_bool(row.get("content_violence")),
            "horror": _db_bool(row.get("content_horror")),
            "gore": _db_bool(row.get("content_gore")),
            "sensitive_tags": _json_list(row.get("content_sensitive_tags")),
            "checked_at": row.get("safety_checked_at"),
        },
        "blend_path": blend_path,
        "has_blend": bool(blend_path),
        "blend_download_url": (
            f"/api/file?path={quote(blend_path, safe='')}" if blend_path else None
        ),
        "view_url": view_rel,
        "download_url": download_rel,
    }
    if base_url:
        item["preview_url_abs"] = _abs_url(base_url, preview_rel) if preview_rel else None
        item["view_url_abs"] = _abs_url(base_url, view_rel)
        item["download_url_abs"] = _abs_url(base_url, download_rel)
        if item["blend_download_url"]:
            item["blend_download_url_abs"] = _abs_url(base_url, item["blend_download_url"])
    return item


def search_model_items(
    *,
    limit: int = 60,
    offset: int = 0,
    sort: str | None = None,
    with_facets: bool = False,
    base_url: str | None = None,
    **filters,
) -> dict:
    """Постраничная выдача каталога: фильтрация и счёт идут в БД, не в Python."""
    rows, total = db.search_assets(limit=limit, offset=offset, sort=sort, **filters)
    tags_map = db.get_tags_bulk([r["path"] for r in rows])
    payload = {
        "total": total,
        "limit": limit,
        "offset": offset,
        "sort": sort or "name",
        "items": [build_item(r, tags_map.get(r["path"], []), base_url) for r in rows],
    }
    if with_facets:
        payload["facets"] = db.facet_counts(**filters)
    return payload


def list_model_items(
    *,
    filters: GroupFilters | None = None,
    base_url: str | None = None,
    group: LaunchGroup | None = None,
) -> dict:
    active = filters or (group.filters if group else GroupFilters())
    any_tags = list(active.tags) or None
    all_tags = list(active.tags_all) or None
    allowed = db.paths_matching_tags(any_tags, all_tags)

    rows = db.list_assets()
    paths = [r["path"] for r in rows]
    tags_map = db.get_tags_bulk(paths)

    items = []
    for r in rows:
        if allowed is not None and r["path"] not in allowed:
            continue
        if not _matches_filters(r, active):
            continue
        items.append(build_item(r, tags_map.get(r["path"], []), base_url))

    payload: dict = {
        "root": str(MODELS_ROOT),
        "count": len(items),
        "items": items,
    }
    if group is not None:
        payload["group"] = group.to_public_dict()
    elif active != GroupFilters():
        payload["filters"] = active.to_dict()
    return payload
