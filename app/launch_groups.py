"""Конфигурация «групп запуска» для внешних клиентов API."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.tag_normalize import normalize_tag_list

UNIT_ACTIONS = (
    "attack",
    "def",
    "walk",
    "run",
    "death",
    "skill1",
    "skill2",
    "skill3",
    "ult",
    "idle",
    "jump",
    "wakeup",
    "fall",
    "block",
)


def default_unit_main() -> dict[str, Any]:
    return {
        "position": [0, 0, 0],
        "scale": [1, 1, 1],
        "rotation": [0, 0, 0],
        "animations": {a: None for a in UNIT_ACTIONS},
    }


@dataclass(frozen=True)
class GroupFilters:
    tags: tuple[str, ...] = ()
    tags_all: tuple[str, ...] = ()
    path_prefix: str | None = None
    ext: tuple[str, ...] = ()
    name_contains: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> GroupFilters:
        if not raw:
            return cls()
        tags = normalize_tag_list(_as_str_list(raw.get("tags")))
        tags_all = normalize_tag_list(_as_str_list(raw.get("tags_all")))
        path_prefix = _clean_prefix(raw.get("path_prefix"))
        ext = tuple(
            x.lower().lstrip(".")
            for x in _as_str_list(raw.get("ext"))
            if x
        )
        name_contains = _clean_text(raw.get("name_contains"))
        return cls(
            tags=tuple(tags),
            tags_all=tuple(tags_all),
            path_prefix=path_prefix,
            ext=ext,
            name_contains=name_contains,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.tags:
            out["tags"] = list(self.tags)
        if self.tags_all:
            out["tags_all"] = list(self.tags_all)
        if self.path_prefix:
            out["path_prefix"] = self.path_prefix
        if self.ext:
            out["ext"] = list(self.ext)
        if self.name_contains:
            out["name_contains"] = self.name_contains
        return out


@dataclass(frozen=True)
class LaunchGroup:
    id: str
    title: str
    filters: GroupFilters
    unit_main: dict[str, Any] | None = None

    def to_public_dict(self, *, include_unit_main: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "filters": self.filters.to_dict(),
        }
        if include_unit_main and self.unit_main is not None:
            out["unit_main"] = self.unit_main
        return out


_lock = threading.Lock()
_cache_mtime: float | None = None
_cache_groups: dict[str, LaunchGroup] = {}


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def _clean_prefix(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip().replace("\\", "/")
    if not s:
        return None
    return s


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _normalize_group_id(raw: str) -> str:
    s = raw.strip()
    if not s or len(s) > 64:
        raise ValueError("group id must be 1..64 chars")
    return s


def _parse_unit_main(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("unit_main must be an object")
    base = default_unit_main()
    for key in ("position", "scale", "rotation"):
        val = raw.get(key)
        if val is None:
            continue
        if not isinstance(val, list) or len(val) != 3:
            raise ValueError(f"unit_main.{key} must be [x, y, z]")
        base[key] = [float(x) for x in val]
    anims = raw.get("animations")
    if anims is not None:
        if not isinstance(anims, dict):
            raise ValueError("unit_main.animations must be an object")
        merged = dict(base["animations"])
        for action in UNIT_ACTIONS:
            if action in anims:
                v = anims[action]
                merged[action] = None if v in (None, "") else str(v)
        base["animations"] = merged
    return base


def _parse_config(data: Any) -> dict[str, LaunchGroup]:
    if not isinstance(data, dict):
        raise ValueError("launch groups config root must be an object")
    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list):
        raise ValueError("launch groups config must contain groups[]")
    out: dict[str, LaunchGroup] = {}
    for i, item in enumerate(raw_groups):
        if not isinstance(item, dict):
            raise ValueError(f"groups[{i}] must be an object")
        gid = _normalize_group_id(str(item.get("id", "")))
        if gid in out:
            raise ValueError(f"duplicate group id: {gid}")
        title = _clean_text(item.get("title")) or gid
        filters = GroupFilters.from_dict(item.get("filters"))
        unit_main = _parse_unit_main(item.get("unit_main"))
        out[gid] = LaunchGroup(id=gid, title=title, filters=filters, unit_main=unit_main)
    return out


def load_launch_groups(path: Path) -> dict[str, LaunchGroup]:
    global _cache_mtime, _cache_groups
    if not path.is_file():
        with _lock:
            _cache_mtime = None
            _cache_groups = {}
        return {}
    mtime = path.stat().st_mtime
    with _lock:
        if _cache_mtime == mtime and _cache_groups:
            return dict(_cache_groups)
    text = path.read_text(encoding="utf-8")
    parsed = _parse_config(json.loads(text))
    with _lock:
        _cache_mtime = mtime
        _cache_groups = parsed
    return dict(parsed)


def get_launch_group(path: Path, group_id: str) -> LaunchGroup | None:
    groups = load_launch_groups(path)
    return groups.get(group_id.strip())


def list_launch_groups(path: Path) -> list[LaunchGroup]:
    groups = load_launch_groups(path)
    return sorted(groups.values(), key=lambda g: g.id.lower())
