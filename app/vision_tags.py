"""Авто-теги и классификация модели по PNG-превью через OpenAI Vision.

Один вызов Vision отдаёт сразу всё, что нужно каталогу: свободные теги, категорию
из закрытого списка, возрастной рейтинг, флаги чувствительного контента и вердикт
«годится ли в детскую игру». Разбивать это на несколько запросов дороже и даёт
рассогласованные ответы (adult=true при age_rating=everyone).

Часть тегов не угадывается по картинке и берётся из метаданных файла: low-poly
считается по числу треугольников, rigged/animated — по наличию скелета и клипов.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import app.db as db
from app.model_meta import complexity_bucket
from app.openai_client import make_openai_client
from app.paths import OPENAI_API_KEY, OPENAI_TAG_MODEL, WORKER_CONCURRENCY
from app.preview_access import resolve_preview_file
from app.tag_normalize import normalize_tag_list
from app.taxonomy import (
    AGE_RATINGS,
    CATEGORIES,
    DEFAULT_AGE_RATING,
    FALLBACK_CATEGORY,
    KID_UNSAFE_TAGS,
    canonical_age_rating,
    canonical_category,
)

log = logging.getLogger(__name__)

SOURCE_OPENAI = "openai"
SOURCE_META = "meta"

# Теги плотности сетки считаются по метаданным и не должны дублироваться догадкой AI.
_POLY_TAGS = frozenset({"very-low-poly", "low-poly", "mid-poly", "high-poly"})


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

    data = png_path.read_bytes()
    b64 = base64.standard_b64encode(data).decode("ascii")

    client = make_openai_client()
    prompt = (
        "You see a 3D model thumbnail. Reply with JSON only: "
        '{"category":"...","tags":["keyword",...],"age_rating":"...",'
        '"kid_friendly":true,"content":{"adult":false,"nudity":false,'
        '"violence":false,"horror":false,"gore":false,"weapons":false,'
        '"drugs":false,"self_harm":false},"sensitive_tags":["label",...]}. '
        f"category MUST be exactly one of: {', '.join(CATEGORIES)}. "
        "tags: 6 to 14 short English keywords, lowercase, hyphen phrases. Always "
        "cover what the object is, its visual style (low-poly, realistic, stylized, "
        "cartoon, pixel-art, voxel, hand-painted...) and its setting when readable "
        "(fantasy, sci-fi, medieval, modern, horror, cyberpunk, military...). "
        f"age_rating MUST be one of: {', '.join(AGE_RATINGS)}. "
        "kid_friendly: true only if the model is safe and appealing in a game for "
        "children under 12 — no sexual content, no gore, no realistic weapons, no "
        "drugs, nothing frightening. "
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
        max_tokens=400,
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

    category = canonical_category(obj.get("category"))
    age_rating = canonical_age_rating(obj.get("age_rating")) or DEFAULT_AGE_RATING
    # Флаги важнее самооценки рейтинга: модель нередко ставит everyone рядом с adult=true.
    if flags["adult"] or flags["nudity"]:
        age_rating = "adult"
    elif flags["gore"] or flags["self_harm"] or flags["drugs"]:
        age_rating = "mature" if age_rating in {"everyone", "teen"} else age_rating
    elif (flags["violence"] or flags["horror"]) and age_rating == "everyone":
        age_rating = "teen"

    kid_friendly = _bool_or_none(obj.get("kid_friendly"))
    if kid_friendly is None:
        kid_friendly = age_rating == "everyone"
    if age_rating != "everyone" or set(sensitive) & KID_UNSAFE_TAGS:
        kid_friendly = False

    return {
        "tags": tags,
        "content": flags,
        "sensitive_tags": sensitive,
        "category": category,
        "age_rating": age_rating,
        "kid_friendly": kid_friendly,
        "nsfw": bool(flags["adult"] or flags["nudity"]),
    }


def _metadata_tags(row: dict | None) -> list[str]:
    """Теги, которые честнее взять из файла модели, чем угадывать по картинке."""
    if not row:
        return []
    out: list[str] = []
    bucket = complexity_bucket(row.get("face_count"))
    if bucket:
        # very-low-poly/low-poly оба должны находиться по запросу «low poly».
        out.append("low-poly" if bucket in {"very-low-poly", "low-poly"} else bucket)
    if row.get("has_rig"):
        out.append("rigged")
    if (row.get("animation_count") or 0) > 0:
        out.append("animated")
    if (row.get("texture_count") or 0) > 0:
        out.append("textured")
    if (row.get("material_count") or 0) > 1:
        out.append("multi-material")
    ext = (row.get("ext") or "").lower()
    if ext:
        out.append(f"format-{ext}")
    return normalize_tag_list(out)


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
    # Метаданные геометрии для производных тегов — одним запросом на всю пачку.
    assets = db.get_assets_bulk([r["path"] for r in rows])

    def _analyze(row: dict) -> tuple[dict, dict | None, str | None]:
        """Сетевая часть: выполняется параллельно, в БД ничего не пишет."""
        path = row["path"]
        png = resolve_preview_file(row["preview_file"], (assets.get(path) or {}).get("preview_key"))
        if png is None:
            return row, None, "preview file missing"
        try:
            return row, _analysis_from_openai_image(png), None
        except Exception as e:  # noqa: BLE001
            log.warning("Vision tag failed %s: %s", path, e)
            return row, None, str(e)

    # Запросы к OpenAI параллельно, запись в БД — последовательно в этом потоке:
    # так пачка ускоряется на порядок и при этом нет конкурентных транзакций.
    with ThreadPoolExecutor(max_workers=max(1, WORKER_CONCURRENCY)) as pool:
        analyzed = list(pool.map(_analyze, rows))

    for row, analysis, error in analyzed:
        path = row["path"]
        if analysis is None:
            errors.append(f"{path}: {error}")
            continue
        tags = normalize_tag_list(
            list(analysis.get("tags") or []) + list(analysis.get("sensitive_tags") or [])
        )
        if not tags:
            errors.append(f"{path}: no tags returned")
            continue
        content = analysis.get("content") or {}
        sensitive_tags = list(analysis.get("sensitive_tags") or [])
        asset = assets.get(path)
        meta_tags = _metadata_tags(asset)
        if set(meta_tags) & _POLY_TAGS:
            # Число треугольников известно точно — оценка «на глаз» по картинке
            # только мешает (модель на 90k граней регулярно зовут low-poly).
            tags = [t for t in tags if t not in _POLY_TAGS]
        # Папка на диске говорит о происхождении, а не о содержимом: в «Animations»
        # лежат и драконы, и персонажи. Поэтому категорию определяет картинка,
        # а раскладка каталога остаётся запасным вариантом.
        category = (
            analysis.get("category") or (asset or {}).get("category") or FALLBACK_CATEGORY
        )
        with db.write_transaction() as conn:
            db.delete_tags_for_source(conn, path, SOURCE_OPENAI)
            db.add_tags(conn, path, tags, SOURCE_OPENAI, now)
            if meta_tags:
                db.delete_tags_for_source(conn, path, SOURCE_META)
                db.add_tags(conn, path, meta_tags, SOURCE_META, now)
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
            db.set_classification(
                conn,
                path,
                category=category,
                age_rating=analysis.get("age_rating"),
                kid_friendly=analysis.get("kid_friendly"),
                nsfw=analysis.get("nsfw"),
                now=now,
            )
        processed += 1

    return {
        "ok": True,
        "processed": processed,
        "candidates": len(rows),
        "errors": errors[:50],
        "error_count": len(errors),
    }
