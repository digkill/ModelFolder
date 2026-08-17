from __future__ import annotations

import json
from typing import Any


def _truncate(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    return text[:n] + "…"


def _extract_json(text: str) -> str:
    text = (text or "").strip()
    if "```" in text:
        text = text.split("```", 1)[1]
        text = text.removeprefix("json").strip()
        if "```" in text:
            text = text.split("```", 1)[0]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return ""

GDD_SYSTEM = """Ты геймдизайнер студии. По запросу игрока напиши короткое ТЗ (GDD) для веб-прототипа.
Верни ТОЛЬКО JSON без markdown:
title, genre, fantasy, player_fantasy, core_loop, win_condition, lose_condition,
controls (array), hero, setting, tone, session_length,
must_have (array of 5-8 конкретных требований к прототипу),
nice_to_have (array),
success_criteria (array of measurable checks),
play (object).
play MUST contain: camera (chase|orbit|top|first), move (walk|drive|grid),
jump (bool), mouse_look (bool), sprint (bool), win (collect|laps|reach|survive),
goal_count (int), goal_label (short noun), arena (open|ring|maze|path),
hazards (none|ghosts|traffic), hint (short controls).
Пиши на языке пользователя.
Это ТЗ только для ЭТОГО запроса. Не копируй чужие проекты.
Придумай механики ПОД ЭТОТ запрос. Запрещено по умолчанию: FPS, прыжок, мышь-взгляд, охота за кристаллами, автополный экран.
Шутер/мышь — только если игрок просит шутер. Прыжок — только платформер. Pac-Man/лабиринт — top+grid+collect+maze.
Гонка — chase+drive+laps+ring. Прогулка — orbit+walk+reach без прыжка.
Прототип живёт в панели студии, не на весь экран."""

PLAYTEST_SYSTEM = """Ты игрок, который только что прошёл веб-прототип. Не выдумывай то, чего нет в инспекции сборки.
Оцени как геймер: весело ли, понятно ли, похоже ли на обещание трейлера/концепта.
Верни ТОЛЬКО JSON:
score (1-10), fun (1-10), clarity (1-10), would_play_again (bool),
session_notes (2-4 коротких абзаца от первого лица),
bugs (array of strings),
praise (array),
verdict (playable|rough|broken).
Пиши на русском."""

SPEC_SYSTEM = """Ты продюсер приёмки. Сверь инспекцию сборки с ТЗ геймдизайнера.
Не засчитывай то, чего нет в инспекции. Голос/музыка — только если has_voice/has_music true.
Верни ТОЛЬКО JSON:
match_percent (0-100),
passed (array of must_have items that are satisfied),
failed (array of {requirement, reason}),
blockers (array),
verdict (accept|rework),
summary (3-6 sentences in Russian for the team)."""


def default_gdd(project: dict, plan: dict | None = None) -> dict[str, Any]:
    from app.game import default_play, detect_genre

    plan = plan or project.get("plan") or {}
    prompt = str(project.get("prompt") or "")
    genre = detect_genre({**project, "plan": plan})
    play = default_play(genre)
    if isinstance(plan.get("play"), dict):
        play = {**play, **{k: v for k, v in plan["play"].items() if v is not None}}
    if genre == "racing":
        return {
            "title": plan.get("title") or project.get("title") or "Гонка",
            "genre": "racing",
            "fantasy": plan.get("logline") or prompt,
            "player_fantasy": "сесть за руль машины из каталога и проехать круг",
            "core_loop": "газ, руль, чекпоинты, финиш",
            "win_condition": "пройти 1 круг через чекпоинты",
            "lose_condition": "нет, прототип без fail-state",
            "controls": ["W газ", "S тормоз", "A/D руль"],
            "hero": "гоночная машина из каталога",
            "setting": plan.get("logline") or prompt,
            "tone": "аркадная гонка",
            "session_length": "2-4 минуты",
            "play": play,
            "must_have": [
                "машина из каталога, вид сзади",
                "трасса (каталог или кольцо)",
                "барьеры/деревья из каталога",
                "чекпоинты и финиш",
                "управление W/S/AD",
                "музыка",
                "голос старта",
            ],
            "nice_to_have": ["соперник", "ускорение", "несколько кругов"],
            "success_criteria": [
                "это гонка, не шутер",
                "машина едет по трассе",
                "финиш засчитывается",
            ],
        }
    if genre == "maze":
        return {
            "title": plan.get("title") or project.get("title") or "Лабиринт",
            "genre": "maze",
            "fantasy": plan.get("logline") or prompt,
            "player_fantasy": "бегать по лабиринту сверху и собирать точки",
            "core_loop": "WASD по коридорам, собирать точки, не врезаться в стены",
            "win_condition": "собрать все точки",
            "lose_condition": "нет, прототип без fail-state",
            "controls": ["WASD"],
            "hero": "аркадный персонаж или жёлтый токен",
            "setting": plan.get("logline") or prompt,
            "tone": "аркадный лабиринт сверху",
            "session_length": "2-4 минуты",
            "play": play,
            "must_have": [
                "камера сверху, не от первого лица",
                "лабиринт со стенами",
                "точки для сбора",
                "управление только WASD, без прыжка и мыши",
                "победа: все точки",
                "музыка",
            ],
            "nice_to_have": ["привидения", "бонусные точки"],
            "success_criteria": [
                "это лабиринт, не шутер",
                "вид сверху",
                "точки собираются",
            ],
        }
    return {
        "title": plan.get("title") or project.get("title") or "Прототип",
        "genre": plan.get("genre") or "adventure",
        "fantasy": plan.get("logline") or prompt,
        "player_fantasy": "управлять героем в заявленной локации и довести сцену до победы",
        "core_loop": play.get("hint") or "ходить по локации и выполнить цель из запроса",
        "win_condition": str(gdd_win(play)),
        "lose_condition": "нет, прототип без fail-state",
        "controls": [play.get("hint") or "WASD"],
        "hero": "главный персонаж из каталога по смыслу промпта",
        "setting": plan.get("logline") or prompt,
        "tone": "прототип под этот запрос",
        "session_length": "2-4 минуты",
        "play": play,
        "must_have": [
            "играбельный герой из каталога",
            "локация в духе промпта (не пустая сетка)",
            "камера и управление из play spec, не шутер по умолчанию",
            "победа совпадает с win/goal_label",
            "концепт-арт или трейлер как визуальный референс",
            "музыка",
        ],
        "nice_to_have": ["диалог", "несколько сцен"],
        "success_criteria": [
            "герой виден и управляется в панели студии",
            "сцена узнаваема рядом с концепт-артом",
            "это не FPS, если запрос не просил шутер",
        ],
    }


def gdd_win(play: dict) -> str:
    win = str(play.get("win") or "collect")
    if win == "laps":
        return f"пройти {play.get('goal_count') or 2} круга"
    if win == "reach":
        return "дойти до цели"
    if win == "survive":
        return f"продержаться {play.get('goal_count') or 45} секунд"
    return f"собрать {play.get('goal_count') or 8} {play.get('goal_label') or 'предметов'}"


def parse_json_obj(text: str, fallback: dict[str, Any]) -> dict[str, Any]:
    raw = _extract_json(text or "")
    if not raw:
        return fallback
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return fallback
    if not isinstance(parsed, dict):
        return fallback
    out = dict(fallback)
    out.update(parsed)
    if isinstance(fallback.get("play"), dict) or isinstance(parsed.get("play"), dict):
        base = fallback.get("play") if isinstance(fallback.get("play"), dict) else {}
        extra = parsed.get("play") if isinstance(parsed.get("play"), dict) else {}
        out["play"] = {**base, **extra}
    return out


def format_review_note(kind: str, data: dict[str, Any]) -> str:
    if kind == "gdd":
        must = data.get("must_have") or []
        lines = [
            f"ТЗ геймдизайнера: {data.get('title') or ''} — {data.get('fantasy') or ''}",
            f"Цикл: {data.get('core_loop') or ''}",
            f"Победа: {data.get('win_condition') or ''}",
            "Must-have: " + "; ".join(str(x) for x in must[:8]),
        ]
        return "\n".join(lines)
    if kind == "playtest":
        return (
            f"Плейтест (как игрок): {data.get('score')}/10, fun {data.get('fun')}/10, "
            f"вердикт {data.get('verdict')}. {data.get('session_notes') or ''}"
        )[:1800]
    if kind == "spec":
        failed = data.get("failed") or []
        reasons = "; ".join(
            (item.get("requirement") if isinstance(item, dict) else str(item)) for item in failed[:6]
        )
        return (
            f"Сверка с ТЗ: {data.get('match_percent')}% — {data.get('verdict')}. "
            f"{data.get('summary') or ''} Не закрыто: {reasons or 'нет'}"
        )[:1800]
    return _truncate(json.dumps(data, ensure_ascii=False), 1200)


def build_review_prompt(kind: str, project: dict, gdd: dict, inspection: dict) -> list[dict[str, str]]:
    system = {"gdd": GDD_SYSTEM, "playtest": PLAYTEST_SYSTEM, "spec": SPEC_SYSTEM}[kind]
    payload = {
        "user_prompt": project.get("prompt"),
        "project_id": project.get("id"),
        "plan": project.get("plan") or {},
        "gdd": gdd,
        "inspection": inspection,
    }
    if kind == "gdd":
        user = (
            f"Project id: {project.get('id')}\n"
            f"Запрос игрока (единственный бриф):\n{project.get('prompt')}\n"
            f"План продюсера:\n{json.dumps(project.get('plan') or {}, ensure_ascii=False)[:3000]}"
        )
    else:
        user = json.dumps(payload, ensure_ascii=False)[:8000]
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
