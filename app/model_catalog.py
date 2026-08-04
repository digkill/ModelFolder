"""Общая логика выдачи моделей для UI и внешнего API."""

from __future__ import annotations

import json
from urllib.parse import quote

import app.db as db
from app.launch_groups import GroupFilters, LaunchGroup
from app.paths import MODELS_ROOT
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
        ext = r["ext"]
        preview_rel = None
        if r.get("preview_status") == "ok" and r.get("preview_file"):
            preview_rel = f"/previews/{r['preview_file']}"
        trows = tags_map.get(r["path"], [])
        tag_strings = [x["tag"] for x in trows]
        blend_path = r.get("blend_path")
        view_rel = _view_path(str(r["path"]))
        download_rel = f"/api/file?path={quote(str(r['path']), safe='')}"
        item = {
            "name": r["name"],
            "path": r["path"],
            "ext": ext,
            "size": int(r["size"]),
            "modified": int(r["mtime"]),
            "preview_url": preview_rel,
            "preview_status": r.get("preview_status"),
            "preview_source": r.get("preview_source"),
            "in_zip": ZIP_SEP in str(r["path"]),
            "description": r.get("description") or None,
            "tags": trows,
            "tag_list": tag_strings,
            "content": {
                "adult": _db_bool(r.get("content_adult")),
                "nudity": _db_bool(r.get("content_nudity")),
                "violence": _db_bool(r.get("content_violence")),
                "horror": _db_bool(r.get("content_horror")),
                "gore": _db_bool(r.get("content_gore")),
                "sensitive_tags": _json_list(r.get("content_sensitive_tags")),
                "checked_at": r.get("safety_checked_at"),
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
        items.append(item)

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
