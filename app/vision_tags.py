"""Авто-теги по PNG-превью через OpenAI Vision."""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

import app.db as db
from app.paths import (
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_ORG_ID,
    OPENAI_TAG_MODEL,
    PREVIEWS_DIR,
)
from app.tag_normalize import normalize_tag_list

log = logging.getLogger(__name__)

SOURCE_OPENAI = "openai"


def _bool_or_none(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().casefold()
        if v in {"true", "yes", "1"}:
            return True
        if v in {"false", "no", "0"}:
            return False
    return None


def _analysis_from_openai_image(png_path: Path) -> dict:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set (добавьте в .env или окружение)")

    from openai import OpenAI

    data = png_path.read_bytes()
    b64 = base64.standard_b64encode(data).decode("ascii")

    client_kw: dict = {"api_key": OPENAI_API_KEY}
    if OPENAI_BASE_URL:
        client_kw["base_url"] = OPENAI_BASE_URL
    if OPENAI_ORG_ID:
        client_kw["organization"] = OPENAI_ORG_ID
    client = OpenAI(**client_kw)
    prompt = (
        "You see a 3D model thumbnail. Reply with JSON only: "
        '{"tags":["keyword",...],"content":{"adult":false,"nudity":false,'
        '"violence":false,"horror":false,"gore":false,"weapons":false,'
        '"drugs":false,"self_harm":false},"sensitive_tags":["label",...]}. '
        "Tags: 4 to 12 short English keywords, lowercase, hyphen phrases "
        "(for example sci-fi, female-character, vehicle). "
        "Content flags are mandatory booleans. Mark adult true for visible nudity "
        "or clearly sexual 18+ content. Mark violence/horror/gore/weapons/drugs/"
        "self_harm when visible or strongly implied by the model. "
        "sensitive_tags must contain only true sensitive labels, lowercase."
    )

    resp = client.chat.completions.create(
        model=OPENAI_TAG_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                            "detail": "low",
                        },
                    },
                ],
            }
        ],
        response_format={"type": "json_object"},
        max_tokens=300,
    )
    text = (resp.choices[0].message.content or "").strip()
    obj = json.loads(text)
    raw = obj.get("tags")
    tags = normalize_tag_list([str(x) for x in raw]) if isinstance(raw, list) else []

    content = obj.get("content")
    if not isinstance(content, dict):
        content = {}
    flags = {
        "adult": _bool_or_none(content.get("adult")),
        "nudity": _bool_or_none(content.get("nudity")),
        "violence": _bool_or_none(content.get("violence")),
        "horror": _bool_or_none(content.get("horror")),
        "gore": _bool_or_none(content.get("gore")),
        "weapons": _bool_or_none(content.get("weapons")),
        "drugs": _bool_or_none(content.get("drugs")),
        "self_harm": _bool_or_none(content.get("self_harm")),
    }
    if flags["adult"] is None:
        flags["adult"] = bool(flags["nudity"])
    for name, value in list(flags.items()):
        if value is None:
            flags[name] = False

    raw_sensitive = obj.get("sensitive_tags")
    sensitive = (
        normalize_tag_list([str(x) for x in raw_sensitive])
        if isinstance(raw_sensitive, list)
        else []
    )
    for name, value in flags.items():
        if value is True:
            sensitive.append("18-plus" if name == "adult" else name.replace("_", "-"))
    sensitive = normalize_tag_list(sensitive)
    return {"tags": tags, "content": flags, "sensitive_tags": sensitive}


def run_auto_tag_batch(
    *,
    limit: int = 30,
    only_missing: bool = True,
) -> dict:
    """
    Для моделей с готовым превью вызывает OpenAI и пишет теги source=openai.
    """
    if not OPENAI_API_KEY:
        return {
            "ok": False,
            "error": "Set OPENAI_API_KEY to enable vision tagging",
            "processed": 0,
        }

    processed = 0
    errors: list[str] = []
    now = time.time()

    rows = db.list_preview_assets_for_tagging(
        limit=max(1, min(limit, 200)), only_missing_openai=only_missing
    )

    for row in rows:
        path = row["path"]
        prev = row["preview_file"]
        png = (PREVIEWS_DIR / prev).resolve()
        try:
            png.relative_to(PREVIEWS_DIR.resolve())
        except ValueError:
            errors.append(f"{path}: bad preview path")
            continue
        if not png.is_file():
            errors.append(f"{path}: preview file missing")
            continue
        try:
            analysis = _analysis_from_openai_image(png)
        except Exception as e:
            log.warning("Vision tag failed %s: %s", path, e)
            errors.append(f"{path}: {e}")  # noqa: PERF401
            continue
        tags = normalize_tag_list(
            list(analysis.get("tags") or []) + list(analysis.get("sensitive_tags") or [])
        )
        if not tags:
            errors.append(f"{path}: no tags returned")
            continue
        content = analysis.get("content") or {}
        sensitive_tags = list(analysis.get("sensitive_tags") or [])
        with db.write_transaction() as conn:
            db.delete_tags_for_source(conn, path, SOURCE_OPENAI)
            db.add_tags(conn, path, tags, SOURCE_OPENAI, now)
            db.set_content_safety(
                conn,
                path,
                adult=content.get("adult"),
                nudity=content.get("nudity"),
                violence=content.get("violence"),
                horror=content.get("horror"),
                gore=content.get("gore"),
                sensitive_tags_json=json.dumps(sensitive_tags, ensure_ascii=False),
                checked_at=now,
            )
        processed += 1

    return {
        "ok": True,
        "processed": processed,
        "candidates": len(rows),
        "errors": errors[:50],
        "error_count": len(errors),
    }
