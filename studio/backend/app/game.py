from __future__ import annotations

import json
import math
import re
from pathlib import Path

GAMES_DIR = Path("/tmp/studio-games")
_TEMPLATE = Path(__file__).with_name("game_template.html")
MESH_EXT = {".glb", ".gltf"}
BUILD_RE = re.compile(
    r"(реализ|собери|сделай|доделай|пересобр|пересобери|заново|rebuild|implement|build|assemble)"
    r".{0,80}(игр|game|сцен|мини|гонк|трасс|машин)|"
    r"мини[- ]?игр|playable game|запусти игр",
    re.IGNORECASE | re.DOTALL,
)
REBUILD_RE = re.compile(
    r"хуе|херн|фигн|ерунд|говно|мусор|не то\b|не гонк|не игр|это не|"
    r"сломан|переделай|исправ|шутер|fps|пушк|пистолет|динозавр|акул|"
    r"кристал|сеточк|пустая сетк",
    re.IGNORECASE,
)
REVIEW_RE = re.compile(
    r"(ревью|review|playtest|плейтест|протест|проверь|сверк|тз|геймдизайн|"
    r"как игрок|как геймер|qa\b|приёмк)",
    re.IGNORECASE,
)

ROLE_HINTS: dict[str, tuple[str, ...]] = {
    "hero": (
        "knight", "warrior", "robot", "soldier", "character", "hero", "human",
        "person", "male", "female", "girl", "boy", "anime", "paladin", "mage",
        "archer", "ninja", "samurai", "pilot", "android", "рыцар", "воин", "робот",
    ),
    "car": (
        "car", "vehicle", "racer", "kart", "buggy", "truck", "coupe", "sedan",
        "supercar", "sportscar", "wheel", "машин", "автомоб", "байк", "bike",
    ),
    "track": ("track", "circuit", "road", "asphalt", "highway", "racetrack", "трасс", "дорог"),
    "barrier": ("barrier", "fence", "cone", "tire", "railing", "guardrail", "bollard", "барьер"),
    "castle": ("castle", "palace", "fortress", "keep", "cathedral", "замок", "дворц"),
    "tower": ("tower", "spire", "turret", "башн"),
    "tree": ("tree", "pine", "oak", "palm", "bush", "hedge", "forest", "дерев", "изгород"),
    "rock": ("rock", "boulder", "cliff", "stone", "скала", "камен"),
    "building": ("house", "building", "ruin", "wall", "gate", "arch", "bridge", "temple", "grandstand", "hangar"),
    "prop": (
        "chest", "crystal", "sword", "shield", "barrel", "crate", "gem", "lamp",
        "torch", "flag", "banner", "weapon", "staff", "statue", "lantern",
        "checkpoint", "finish", "gun", "rifle", "pistol",
    ),
}
ROLE_ORDER = (
    "hero", "car", "track", "barrier", "castle", "tower", "tree", "rock", "building", "prop",
)
ROLE_HEIGHT = {
    "hero": 2.45,
    "car": 1.15,
    "track": 1.2,
    "barrier": 1.4,
    "castle": 14.0,
    "tower": 9.0,
    "building": 7.0,
    "tree": 4.4,
    "rock": 2.1,
    "prop": 1.5,
    "env": 6.0,
}
TALL_ROLES = {"hero", "tree", "tower", "castle", "building"}
LONG_ROLES = {"car", "track", "barrier"}
POSE_ROT = {
    "ok": (0.0, 0.0),
    "needs_x90": (math.pi / 2, 0.0),
    "needs_x-90": (-math.pi / 2, 0.0),
    "needs_z90": (0.0, math.pi / 2),
    "upside_down": (math.pi, 0.0),
}
SLOT_ADVENTURE = {"hero": 1, "castle": 2, "tower": 3, "tree": 6, "rock": 4, "building": 2, "prop": 4}
SLOT_RACING = {"car": 6, "track": 2, "barrier": 6, "tree": 6, "building": 2, "prop": 3}
SLOT_MAZE = {"hero": 1, "tree": 8, "prop": 4}
ROLE_EXPECT = {
    "hero": "a playable character or creature matching the project. Guns, cars, and props are NOT this. If unsure, fit=false.",
    "car": "a complete land vehicle you could drive (sports car, sedan, rally car, motorcycle). Animals, characters, guns, trees, buildings are NOT this. If unsure, fit=false.",
    "track": "a road or race circuit piece. Characters and room interiors are NOT this. If unsure, fit=false.",
    "barrier": "a crash barrier, traffic cone, tire wall, or metal guardrail. Balconies, brick walls, houses, cars are NOT this. If unsure, fit=false.",
    "castle": "a castle, palace, or fortress exterior. If unsure, fit=false.",
    "tower": "a tower or spire, not a character. If unsure, fit=false.",
    "tree": "a standalone tree or bush. Houses, dioramas, wall vines, characters, cars are NOT this. If unsure, fit=false.",
    "rock": "a rock, boulder, or cliff. Creatures are NOT this. If unsure, fit=false.",
    "building": "a building exterior, grandstand, hangar, or house. Empty interiors and cars are NOT this. If unsure, fit=false.",
    "prop": "a small set dressing prop (chest, flag, lantern, statue, cone). Characters, guns, sharks, trees are NOT this. If unsure, fit=false.",
}
TOKEN_RE: dict[str, re.Pattern[str]] = {
    role: re.compile(
        r"(?:^|[^a-zа-я0-9])(?:"
        + "|".join(re.escape(token) for token in tokens)
        + r")(?:[^a-zа-я0-9]|$)",
        re.IGNORECASE,
    )
    for role, tokens in ROLE_HINTS.items()
}
CAR_RE = re.compile(
    r"(?:^|[^a-zа-я0-9])(?:car|cars|bmw|ferrari|porsche|lambo|lamborghini|mustang|"
    r"sedan|coupe|supercar|sportscar|vehicle|moto|motorcycle|buggy|kart|truck|"
    r"widebody|vecarz|nissan|toyota|honda|subaru)(?:[^a-zа-я0-9]|$)",
    re.IGNORECASE,
)
NOT_CAR_RE = re.compile(
    r"scar-|cartoon|karate|character|ninja|dino|saur|shark|rifle|pistol|fps|"
    r"golem|knight|girl|queen|crab|butterfly|astronaut|plant|alien|idle_animation",
    re.IGNORECASE,
)
FOREIGN = {
    "racing": re.compile(
        r"knight|castle|crystal|shooter|gun|fps|sword|rifle|pistol|smg|shotgun|"
        r"shark|dino|saur|golem|paladin|mage|ninja|samurai|idle_animation|"
        r"рыцар|замок|кристал|пушк|пистолет|акул",
        re.I,
    ),
    "adventure": re.compile(
        r"bmw|ferrari|porsche|lambo|widebody|vecarz|racetrack|crash.?barrier|"
        r"traffic.?cone|grandstand|need for speed|supercar|sports.?car|"
        r"rally.?car|asphalt race",
        re.I,
    ),
    "maze": re.compile(
        r"bmw|ferrari|porsche|lambo|widebody|vecarz|racetrack|crash.?barrier|"
        r"need for speed|supercar|sports.?car|knight|castle|рыцар|замок|"
        r"gun|fps|rifle|pistol|shotgun|shark|dino|saur|crystal hunt|"
        r"jump.?and.?run",
        re.I,
    ),
}
INTERIOR_RE = re.compile(
    r"interior|indoor|room\b|balcony|diorama|supermarket|neighbor|vr[_ ]?room|"
    r"stairs|cross_straight|baked",
    re.I,
)
BIKE_RE = re.compile(r"motorcycle|moto\b|motorbike|akira|bike", re.I)


def _mentions(hay: str, role: str) -> bool:
    rx = TOKEN_RE.get(role)
    return bool(rx.search(hay)) if rx else False


def looks_like_car(asset: dict) -> bool:
    hay = _hay(asset) if "title" in asset or "meta" in asset else hit_hay(asset)
    if NOT_CAR_RE.search(hay) and not CAR_RE.search(hay):
        return False
    return bool(CAR_RE.search(hay) or _mentions(hay, "car"))


def looks_like_race_car(asset: dict) -> bool:
    if not looks_like_car(asset):
        return False
    hay = _hay(asset) if "title" in asset or "meta" in asset else hit_hay(asset)
    if INTERIOR_RE.search(hay):
        return False
    if BIKE_RE.search(hay) and not re.search(r"bmw|ferrari|porsche|lambo|sedan|coupe|supercar|widebody|vecarz", hay, re.I):
        return False
    return True


GENRE_RE = re.compile(
    r"гонк|race|racing|drift|kart|трасс|circuit|rally|need for speed|автосимул|машинк",
    re.IGNORECASE,
)
MAZE_RE = re.compile(
    r"pac-?man|пакман|пакетман|maze|labyrinth|лабиринт|пеллет|привидени|"
    r"ghosts?|вид сверху|top[- ]?down|аркад.*лабир|собирать точки",
    re.IGNORECASE,
)


def detect_genre(project: dict) -> str:
    plan = project.get("plan") or {}
    gdd = plan.get("gdd") if isinstance(plan.get("gdd"), dict) else {}
    blob = " ".join(
        str(part or "")
        for part in (
            plan.get("genre"),
            gdd.get("genre"),
            gdd.get("core_loop"),
            gdd.get("win_condition"),
            project.get("title"),
            project.get("prompt"),
            plan.get("title"),
            plan.get("logline"),
        )
    )
    named = str(plan.get("genre") or gdd.get("genre") or "").strip().lower()
    if named == "racing" or GENRE_RE.search(blob):
        return "racing"
    if named == "maze" or MAZE_RE.search(blob):
        return "maze"
    return "adventure"


FPS_RE = re.compile(r"\bfps\b|шутер|стрелял|first.?person|от первого лица", re.I)
JUMP_RE = re.compile(r"прыж|platformer|платформер", re.I)
WALK_RE = re.compile(r"прогул|walking.?sim|исследуй|экскурси", re.I)

PLAY_CAMERAS = {"chase", "orbit", "top", "first"}
PLAY_MOVES = {"walk", "drive", "grid"}
PLAY_WINS = {"collect", "laps", "reach", "survive"}
PLAY_ARENAS = {"open", "ring", "maze", "path"}
PLAY_HAZARDS = {"none", "ghosts", "traffic"}


def default_play(genre: str) -> dict:
    if genre == "racing":
        return {
            "camera": "chase",
            "move": "drive",
            "jump": False,
            "mouse_look": False,
            "sprint": False,
            "win": "laps",
            "goal_count": 2,
            "goal_label": "круги",
            "arena": "ring",
            "hazards": "traffic",
            "hint": "W — газ, S — тормоз, A/D — руль",
        }
    if genre == "maze":
        return {
            "camera": "top",
            "move": "grid",
            "jump": False,
            "mouse_look": False,
            "sprint": False,
            "win": "collect",
            "goal_count": 0,
            "goal_label": "точки",
            "arena": "maze",
            "hazards": "ghosts",
            "hint": "WASD — ход по лабиринту",
        }
    return {
        "camera": "orbit",
        "move": "walk",
        "jump": False,
        "mouse_look": False,
        "sprint": True,
        "win": "collect",
        "goal_count": 8,
        "goal_label": "предметы",
        "arena": "open",
        "hazards": "none",
        "hint": "WASD — ходьба",
    }


def sanitize_play(project: dict) -> dict:
    plan = project.get("plan") or {}
    gdd = plan.get("gdd") if isinstance(plan.get("gdd"), dict) else {}
    genre = detect_genre(project)
    play = default_play(genre)
    blob = project_brief(project)
    if genre == "adventure":
        if FPS_RE.search(blob):
            play.update(camera="first", mouse_look=True, hint="WASD — ходьба, мышь — взгляд")
        if JUMP_RE.search(blob):
            play.update(jump=True, hint="WASD — ходьба, пробел — прыжок")
        if WALK_RE.search(blob):
            play.update(win="reach", jump=False, mouse_look=False, goal_label="цель")
    raw: dict = {}
    if isinstance(gdd.get("play"), dict):
        raw.update(gdd["play"])
    if isinstance(plan.get("play"), dict):
        raw.update(plan["play"])
    cam = str(raw.get("camera") or "").strip().lower()
    if cam in PLAY_CAMERAS:
        play["camera"] = cam
    move = str(raw.get("move") or "").strip().lower()
    if move in PLAY_MOVES:
        play["move"] = move
    win = str(raw.get("win") or "").strip().lower()
    if win in PLAY_WINS:
        play["win"] = win
    arena = str(raw.get("arena") or "").strip().lower()
    if arena in PLAY_ARENAS:
        play["arena"] = arena
    hazards = str(raw.get("hazards") or "").strip().lower()
    if hazards in PLAY_HAZARDS:
        play["hazards"] = hazards
    if "jump" in raw:
        play["jump"] = bool(raw["jump"])
    if "mouse_look" in raw:
        play["mouse_look"] = bool(raw["mouse_look"])
    if "sprint" in raw:
        play["sprint"] = bool(raw["sprint"])
    try:
        n = int(raw.get("goal_count"))
        if 1 <= n <= 80:
            play["goal_count"] = n
    except (TypeError, ValueError):
        pass
    label = str(raw.get("goal_label") or "").strip()
    if 0 < len(label) < 24:
        play["goal_label"] = label
    hint = str(raw.get("hint") or "").strip()
    if 0 < len(hint) < 80:
        play["hint"] = hint
    if genre == "maze":
        play.update(
            camera="top",
            move="grid",
            jump=False,
            mouse_look=False,
            arena="maze",
            win="collect",
        )
    elif genre == "racing":
        play.update(
            camera="chase",
            move="drive",
            jump=False,
            mouse_look=False,
            arena="ring",
            win="laps",
        )
    return play


def project_brief(project: dict) -> str:
    plan = project.get("plan") or {}
    gdd = plan.get("gdd") if isinstance(plan.get("gdd"), dict) else {}
    return " ".join(
        str(part or "")
        for part in (
            project.get("id"),
            project.get("title"),
            project.get("prompt"),
            plan.get("title"),
            plan.get("logline"),
            plan.get("genre"),
            gdd.get("hero"),
            gdd.get("setting"),
            gdd.get("core_loop"),
        )
    )


def foreign_hay(hay: str, genre: str) -> bool:
    text = hay or ""
    rx = FOREIGN.get(genre)
    if not rx:
        return False
    if genre == "racing" and looks_like_car({"title": text, "meta": {}}):
        return False
    return bool(rx.search(text))


def in_project_context(asset: dict, project: dict) -> bool:
    genre = detect_genre(project)
    hay = _hay(asset)
    if foreign_hay(hay, genre):
        return False
    if genre == "racing":
        if INTERIOR_RE.search(hay):
            return False
        if looks_like_car(asset):
            return True
        role = _role(asset, "racing")
        if role == "hero" or _mentions(hay, "hero"):
            return False
        if role == "prop" and re.search(
            r"animat|idle|creature|girl|boy|fish|crab|butterfly|elephant|panther|kong|abomination|astronaut|karate",
            hay,
            re.I,
        ):
            return False
        if role not in {"track", "barrier", "tree", "building", "prop", "rock"}:
            return False
    elif genre in {"adventure", "maze"} and looks_like_car(asset):
        return False
    if genre == "maze" and INTERIOR_RE.search(hay):
        return False
    return True


def filter_queries(queries: list[str], genre: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in queries:
        text = str(raw or "").strip()
        key = text.lower()
        if not text or key in seen or len(text) > 80 or foreign_hay(text, genre):
            continue
        seen.add(key)
        out.append(text)
    return out


def slot_need(project: dict) -> dict[str, int]:
    genre = detect_genre(project)
    if genre == "racing":
        return dict(SLOT_RACING)
    if genre == "maze":
        return dict(SLOT_MAZE)
    return dict(SLOT_ADVENTURE)


def wants_review(message: str) -> bool:
    return bool(REVIEW_RE.search(message or ""))


def wants_game(message: str) -> bool:
    return bool(BUILD_RE.search(message or "") or REBUILD_RE.search(message or ""))


def game_url(project_id: str) -> str:
    return f"/app/api/v1/games/{project_id}.html"


def is_mesh_url(url: str, meta: dict | None = None) -> bool:
    text = (url or "").split("?", 1)[0].lower()
    ext = str((meta or {}).get("ext") or "").lower().lstrip(".")
    if ext in {"glb", "gltf"}:
        return True
    return any(text.endswith(suffix) for suffix in MESH_EXT)


def _tag_text(tags: object) -> str:
    if isinstance(tags, list):
        return " ".join(str(t) for t in tags)
    return str(tags or "")


def _hay(asset: dict) -> str:
    meta = asset.get("meta") or {}
    return (
        f"{asset.get('title') or ''} {meta.get('name') or ''} {meta.get('path') or ''} "
        f"{meta.get('category') or ''} {_tag_text(meta.get('tag_list'))}"
    ).lower()


def hit_hay(hit: dict) -> str:
    return (
        f"{hit.get('name') or ''} {hit.get('path') or ''} {hit.get('category') or ''} "
        f"{_tag_text(hit.get('tag_list'))}"
    ).lower()


def rank_catalog_hits(hits: list[dict], role: str) -> list[dict]:
    def key(hit: dict) -> tuple[int, float]:
        hay = hit_hay(hit)
        if role == "car":
            fit = looks_like_race_car(hit) or looks_like_car(hit)
        else:
            fit = _mentions(hay, role)
        if role not in {"car", "hero"} and CAR_RE.search(hay):
            return (2, 0.0)
        hero = _mentions(hay, "hero")
        if role != "hero" and hero and not fit:
            return (2, 0.0)
        return (0 if fit else 1, -float(hit.get("score") or 0))

    ranked = [hit for hit in sorted(hits, key=key) if key(hit)[0] < 2]
    if role in {"car", "track", "hero"}:
        fitted = [hit for hit in ranked if key(hit)[0] == 0]
        if fitted or role == "car":
            return fitted
    return ranked


def _role(asset: dict, genre: str | None = None) -> str:
    meta = asset.get("meta") or {}
    tagged = str(meta.get("scene_role") or "").strip().lower()
    hay = _hay(asset)
    if tagged == "car" and not looks_like_car(asset):
        tagged = ""
    if tagged in ROLE_HINTS:
        return tagged
    order = ("car", "track", "barrier", "building", "tree", "rock", "prop", "hero") if genre == "racing" else ROLE_ORDER
    for role in order:
        if role == "car":
            if looks_like_car(asset):
                return "car"
            continue
        if _mentions(hay, role):
            return role
    return "prop"


def _as_int(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def vision_ok(asset: dict) -> bool:
    meta = asset.get("meta") or {}
    if str(meta.get("vision_pose") or "") == "unusable":
        return False
    if meta.get("vision_size_ok") is False:
        return False
    if "vision_fit" not in meta:
        return True
    return bool(meta.get("vision_fit")) and _as_int(meta.get("vision_score")) >= 7


def _bbox_xyz(raw: object) -> tuple[float, float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return None
    try:
        x, y, z = (abs(float(raw[0] or 0)), abs(float(raw[1] or 0)), abs(float(raw[2] or 0)))
    except (TypeError, ValueError):
        return None
    if x + y + z < 1e-6:
        return None
    return x, y, z


def pose_from_bbox(role: str, bbox: object) -> dict[str, float]:
    dims = _bbox_xyz(bbox)
    if not dims:
        return {}
    x, y, z = dims
    rot_x = rot_z = 0.0
    if role in TALL_ROLES and y < max(x, z) * 0.7:
        if z >= x:
            rot_x = -math.pi / 2
        else:
            rot_z = math.pi / 2
    elif role in LONG_ROLES and y > max(x, z) * 1.1:
        rot_x = -math.pi / 2
    out: dict[str, float] = {}
    if rot_x:
        out["rot_x"] = rot_x
    if rot_z:
        out["rot_z"] = rot_z
    return out


def asset_pose(asset: dict, kind: str) -> dict:
    meta = asset.get("meta") or {}
    geom = meta.get("geometry") if isinstance(meta.get("geometry"), dict) else {}
    bbox = geom.get("bbox") or meta.get("bbox")
    rot_x = rot_z = 0.0
    guessed = pose_from_bbox(kind, bbox)
    rot_x = float(guessed.get("rot_x") or 0)
    rot_z = float(guessed.get("rot_z") or 0)
    pose = str(meta.get("vision_pose") or "")
    if pose in POSE_ROT:
        rot_x, rot_z = POSE_ROT[pose]
    else:
        if meta.get("rot_x") is not None:
            rot_x = float(meta.get("rot_x") or 0)
        if meta.get("rot_z") is not None:
            rot_z = float(meta.get("rot_z") or 0)
    yaw_fix = 0.0
    try:
        yaw_fix = math.radians(float(meta.get("vision_yaw") or 0))
    except (TypeError, ValueError):
        yaw_fix = 0.0
    height = float(ROLE_HEIGHT.get(kind) or 2.6)
    try:
        meters = float(meta.get("vision_meters_h") or 0)
    except (TypeError, ValueError):
        meters = 0.0
    if height * 0.55 <= meters <= height * 2.2:
        height = meters
    return {"rot_x": rot_x, "rot_z": rot_z, "yaw_fix": yaw_fix, "height": height}


def _anim_score(asset: dict) -> int:
    meta = asset.get("meta") or {}
    geom = meta.get("geometry") if isinstance(meta.get("geometry"), dict) else {}
    count = _as_int(meta.get("animations") or geom.get("animations"))
    animated = bool(meta.get("animated") or count)
    rigged = bool(meta.get("rigged") or (geom.get("rigged") if geom else False))
    score = 0
    if animated:
        score += 120
    score += min(count, 24) * 4
    if rigged:
        score += 25
    ext = str(meta.get("ext") or "").lower()
    if ext in {"glb", "gltf"}:
        score += 15
    if _role(asset) == "hero":
        score += 20
    return score


def pick_cast(project: dict) -> dict:
    genre = detect_genre(project)
    meshes: list[dict] = []
    seen: set[str] = set()
    for asset in project.get("assets") or []:
        if asset.get("kind") not in {"model", "mesh"}:
            continue
        url = (asset.get("url") or "").strip()
        if not url or url in seen or not is_mesh_url(url, asset.get("meta") or {}):
            continue
        if not in_project_context(asset, project):
            continue
        seen.add(url)
        meshes.append(asset)
    buckets: dict[str, list[dict]] = {role: [] for role in ROLE_ORDER}
    for asset in meshes:
        buckets[_role(asset, genre)].append(asset)
    for role, items in buckets.items():
        if role in {"hero", "car"}:
            items.sort(key=_anim_score, reverse=True)
        else:
            items.sort(
                key=lambda a: (
                    0 if vision_ok(a) else 1,
                    -_as_int((a.get("meta") or {}).get("vision_score")),
                    -_as_int((a.get("meta") or {}).get("score")),
                    a.get("title") or "",
                )
            )
    if genre == "racing":
        cars = [asset for asset in meshes if looks_like_race_car(asset)] or [
            asset for asset in meshes if looks_like_car(asset)
        ]

        def car_rank(asset: dict) -> tuple[int, int]:
            hay = _hay(asset)
            bonus = 0
            if re.search(r"bmw|ferrari|porsche|lambo|widebody|vecarz|sedan|coupe|supercar", hay, re.I):
                bonus += 50
            if BIKE_RE.search(hay):
                bonus -= 40
            if INTERIOR_RE.search(hay):
                bonus -= 80
            if re.search(r"mech|catfish|montage|scene with three", hay, re.I):
                bonus -= 60
            return (bonus, _as_int((asset.get("meta") or {}).get("score")))

        cars.sort(key=car_rank, reverse=True)
        buckets["car"] = cars
        hero = (cars or [None])[0]
    else:
        hero = (buckets["hero"] or meshes[:1] or [None])[0]
    hero_url = (hero or {}).get("url") or ""
    placements = _layout_placements(genre, buckets, hero_url)
    hero_kind = "car" if genre == "racing" else "hero"
    hero_out: dict = {}
    if hero:
        hero_out = {
            "title": hero.get("title") or "",
            "url": hero_url,
            **asset_pose(hero, hero_kind),
        }
    return {
        "genre": genre,
        "hero": hero_out,
        "placements": placements,
        "hero_animated": bool(hero and _anim_score(hero) >= 120),
        "counts": {role: len(items) for role, items in buckets.items()},
    }


def _place(
    asset: dict,
    *,
    x: float,
    z: float,
    height: float,
    yaw: float,
    kind: str,
    fit: str = "height",
    fit_size: float = 0,
) -> dict:
    pose = asset_pose(asset, kind)
    return {
        "title": asset.get("title") or "",
        "url": asset.get("url") or "",
        "kind": kind,
        "x": round(x, 2),
        "z": round(z, 2),
        "y": 0,
        "height": round(float(pose.get("height") or height), 3),
        "yaw": round(yaw + float(pose.get("yaw_fix") or 0), 3),
        "rot_x": round(float(pose.get("rot_x") or 0), 4),
        "rot_z": round(float(pose.get("rot_z") or 0), 4),
        "fit": fit,
        "fit_size": fit_size,
    }


def _layout_placements(genre: str, buckets: dict[str, list[dict]], hero_url: str) -> list[dict]:
    if genre == "racing":
        return _layout_racing(buckets, hero_url)
    if genre == "maze":
        return _layout_maze(buckets, hero_url)
    return _layout_adventure(buckets, hero_url)


def _take(
    buckets: dict[str, list[dict]],
    used: set[str],
    role: str,
    n: int,
    *,
    reuse: bool = False,
    skip: tuple[str, ...] = (),
) -> list[dict]:
    picked: list[dict] = []
    pool = list(buckets.get(role) or [])
    verified = [a for a in pool if (a.get("meta") or {}).get("vision_fit") is True]
    if verified:
        pool = verified
    pool.sort(key=lambda a: (0 if vision_ok(a) else 1, -_as_int((a.get("meta") or {}).get("vision_score"))))
    for asset in pool:
        url = asset.get("url") or ""
        if not url or url in used:
            continue
        if not vision_ok(asset):
            continue
        hay = _hay(asset)
        if skip and any(token in hay for token in skip):
            continue
        if INTERIOR_RE.search(hay):
            continue
        used.add(url)
        picked.append(asset)
        if len(picked) >= n:
            break
    unique = list(picked)
    if reuse and unique:
        while len(picked) < n:
            picked.append(unique[len(picked) % len(unique)])
    return picked


def _layout_maze(buckets: dict[str, list[dict]], hero_url: str) -> list[dict]:
    out: list[dict] = []
    used: set[str] = {hero_url} if hero_url else set()
    trees = _take(buckets, used, "tree", SLOT_MAZE["tree"], reuse=True)
    for i, asset in enumerate(trees):
        ang = (i / max(len(trees), 1)) * math.pi * 2
        r = 26.5 + (i % 3) * 1.6
        out.append(_place(
            asset, x=math.cos(ang) * r, z=math.sin(ang) * r,
            height=3.4, yaw=-ang, kind="tree",
        ))
    props = _take(buckets, used, "prop", SLOT_MAZE["prop"], reuse=True)
    for i, asset in enumerate(props):
        ang = 0.5 + i * 1.4
        out.append(_place(
            asset, x=math.cos(ang) * 22.5, z=math.sin(ang) * 22.5,
            height=1.35, yaw=ang, kind="prop",
        ))
    return out[:16]


def _layout_adventure(buckets: dict[str, list[dict]], hero_url: str) -> list[dict]:
    out: list[dict] = []
    used: set[str] = {hero_url} if hero_url else set()
    need = SLOT_ADVENTURE
    for i, asset in enumerate(_take(buckets, used, "castle", need["castle"])):
        out.append(_place(
            asset, x=(-14 if i else 3), z=-26 - i * 2,
            height=ROLE_HEIGHT["castle"], yaw=0.08 * i, kind="castle",
        ))
    for i, asset in enumerate(_take(buckets, used, "building", need["building"])):
        ang = -0.7 + i * 0.85
        out.append(_place(
            asset, x=math.sin(ang) * 18, z=-18 + math.cos(ang) * 2,
            height=ROLE_HEIGHT["building"], yaw=ang + math.pi, kind="building",
        ))
    for i, asset in enumerate(_take(buckets, used, "tower", need["tower"])):
        ang = -1.05 + i * 0.7
        out.append(_place(
            asset, x=math.sin(ang) * 21, z=-16 + math.cos(ang) * -4,
            height=ROLE_HEIGHT["tower"], yaw=ang + math.pi, kind="tower",
        ))
    trees = _take(buckets, used, "tree", need["tree"], reuse=True)
    for i, asset in enumerate(trees):
        ang = (i / max(len(trees), 1)) * math.pi * 1.7 + 0.35
        r = 12.5 + (i % 3) * 2.2
        out.append(_place(
            asset, x=math.cos(ang) * r, z=math.sin(ang) * r - 1,
            height=ROLE_HEIGHT["tree"], yaw=-ang, kind="tree",
        ))
    rocks = _take(buckets, used, "rock", need["rock"], reuse=True)
    for i, asset in enumerate(rocks):
        ang = (i / max(len(rocks), 1)) * math.pi * 2 + 0.8
        r = 9 + (i % 2) * 3
        out.append(_place(
            asset, x=math.cos(ang) * r, z=math.sin(ang) * r + 1,
            height=ROLE_HEIGHT["rock"], yaw=ang, kind="rock",
        ))
    props = _take(buckets, used, "prop", need["prop"], reuse=True)
    for i, asset in enumerate(props):
        ang = (i / max(len(props), 1)) * math.pi * 2
        out.append(_place(
            asset, x=math.cos(ang) * 7.5, z=math.sin(ang) * 7.5,
            height=ROLE_HEIGHT["prop"], yaw=ang, kind="prop",
        ))
    return out[:24]


def _layout_racing(buckets: dict[str, list[dict]], hero_url: str) -> list[dict]:
    out: list[dict] = []
    used: set[str] = {hero_url} if hero_url else set()
    need = SLOT_RACING
    skip = ROLE_HINTS["hero"] + (
        "gun", "rifle", "pistol", "smg", "shotgun", "weapon", "shark",
        "interior", "room", "balcony", "diorama", "supermarket", "neighbor", "stairs",
    )
    barriers = _take(buckets, used, "barrier", need["barrier"], reuse=True, skip=skip)
    for i, asset in enumerate(barriers):
        inner = i % 2 == 0
        ang = (i / max(len(barriers), 1)) * math.pi * 2
        r = 33.4 if inner else 48.8
        out.append(_place(
            asset, x=math.cos(ang) * r, z=math.sin(ang) * r,
            height=ROLE_HEIGHT["barrier"], yaw=ang + math.pi / 2, kind="barrier",
        ))
    trees = _take(buckets, used, "tree", need["tree"], reuse=True, skip=skip)
    for i, asset in enumerate(trees):
        ang = (i / max(len(trees), 1)) * math.pi * 2 + 0.2
        r = 58 + (i % 3) * 2.4
        out.append(_place(
            asset, x=math.cos(ang) * r, z=math.sin(ang) * r,
            height=ROLE_HEIGHT["tree"], yaw=-ang, kind="tree",
        ))
    for i, asset in enumerate(_take(buckets, used, "building", need["building"], skip=skip)):
        ang = 0.5 + i * 1.2
        out.append(_place(
            asset, x=math.cos(ang) * 72, z=math.sin(ang) * 72,
            height=ROLE_HEIGHT["building"], yaw=ang + math.pi, kind="building",
        ))
    props = _take(buckets, used, "prop", need["prop"], reuse=True, skip=skip)
    for i, asset in enumerate(props):
        ang = 0.3 + i * 0.9
        out.append(_place(
            asset, x=math.cos(ang) * 54, z=math.sin(ang) * 54,
            height=ROLE_HEIGHT["prop"], yaw=ang, kind="prop",
        ))
    racers = [
        asset
        for asset in (buckets.get("car") or [])
        if (asset.get("url") or "") != hero_url and looks_like_race_car(asset)
    ][:5]
    for i, asset in enumerate(racers):
        ang = 0.55 + i * 1.05
        out.append(_place(
            asset,
            x=math.cos(ang) * 41,
            z=math.sin(ang) * 41,
            height=ROLE_HEIGHT["car"],
            yaw=ang + math.pi / 2,
            kind="car",
        ))
    return out[:24]


def scene_search_plan(project: dict) -> list[tuple[str, bool | None, list[str]]]:
    plan = project.get("plan") or {}
    gdd = plan.get("gdd") if isinstance(plan.get("gdd"), dict) else {}
    prompt = str(project.get("prompt") or "")
    brief = project_brief(project)
    setting = str(gdd.get("setting") or plan.get("logline") or prompt)[:80]
    genre = detect_genre(project)
    queries = filter_queries(
        [str(q).strip() for q in (plan.get("catalog_queries") or []) if str(q).strip()],
        genre,
    )
    if genre == "racing":
        car = str(gdd.get("hero") or "sports race car")
        if foreign_hay(car, "racing"):
            car = "sports race car"
        return [
            ("car", False, filter_queries(["bmw", "ferrari", "sedan", "coupe", "car", "van", "widebody", "vecarz", *queries[:4]], "racing")),
            ("track", False, ["asphalt race circuit", "highway road loop"]),
            ("barrier", False, ["crash barrier", "traffic cone", "tire wall"]),
            ("tree", False, ["pine tree", "palm tree", "forest tree"]),
            ("building", False, ["grandstand stadium", "hangar warehouse", "race pit building"]),
            ("prop", False, ["finish line flag", "checkpoint arch", "street light"]),
        ]
    if genre == "maze":
        hero = str(gdd.get("hero") or "").strip()
        if not hero or foreign_hay(hero, "maze"):
            hero = next((q for q in queries if q), "arcade character")
        return [
            ("hero", True, filter_queries([hero, "arcade character", "yellow character", *queries[:3]], "maze")),
            ("tree", False, ["hedge bush", "maze hedge", "green garden bush", "boxwood hedge"]),
            ("prop", False, ["garden lantern", "stone lamp", "torch light"]),
        ]
    hero = str(gdd.get("hero") or "").strip()
    if not hero or foreign_hay(hero, "adventure"):
        hero = next((q for q in queries if q), "game character")
    wants_castle = bool(re.search(r"castle|замок|дворц|keep|fortress|palace|knight|рыцар", brief, re.I))
    rows: list[tuple[str, bool | None, list[str]]] = [
        ("hero", True, filter_queries([hero, f"{hero} animated", *queries[:3], prompt[:80]], "adventure")),
    ]
    if wants_castle:
        rows.extend(
            [
                ("castle", False, [f"{setting} castle", "fantasy castle keep", "medieval palace fortress"]),
                ("tower", False, ["stone tower keep", "castle turret", "fantasy spire tower"]),
                ("building", False, ["fantasy house building", "ruined stone wall gate", "stone arch bridge"]),
            ]
        )
    else:
        rows.extend(
            [
                ("building", False, [f"{setting} building", "game environment house", "stone ruin wall"]),
                ("tower", False, [f"{setting} tower", "watchtower spire"]),
            ]
        )
    rows.extend(
        [
            ("tree", False, ["pine tree", "oak tree forest", f"{setting} tree"]),
            ("rock", False, ["boulder rock", "cliff stone"]),
            ("prop", False, [f"{setting} prop", "lantern torch", "stone statue", "barrel crate"]),
        ]
    )
    return rows


def inspect_build(project: dict) -> dict:
    cast = pick_cast(project)
    assets = project.get("assets") or []
    jobs = project.get("jobs") or []
    kinds = {str(a.get("kind")) for a in assets if a.get("url")}
    html = ""
    path = GAMES_DIR / f"{project.get('id')}.html"
    if path.is_file():
        html = path.read_text(encoding="utf-8", errors="ignore")
    failed = [
        {"agent": j.get("agent"), "error": (j.get("error") or "")[:240]}
        for j in jobs
        if j.get("status") == "error"
    ]
    genre = detect_genre(project)
    play = sanitize_play(project)
    racing = play.get("arena") == "ring" or genre == "racing"
    maze = play.get("arena") == "maze" or genre == "maze"
    controls = [play.get("hint") or "WASD"]
    win = str(play.get("win") or "")
    if win == "collect":
        win = f"collect {play.get('goal_count') or 'all'} {play.get('goal_label') or 'items'}"
    elif win == "laps":
        win = f"complete {play.get('goal_count') or 2} laps"
    elif win == "reach":
        win = "reach the goal marker"
    elif win == "survive":
        win = f"survive {play.get('goal_count') or 45}s"
    mode = "racing" if racing else "maze" if maze else "adventure"
    return {
        "title": project.get("title") or "",
        "prompt": project.get("prompt") or "",
        "genre": genre,
        "game_ready": bool(html),
        "game_url": game_url(str(project.get("id") or "")),
        "hero": cast.get("hero") or {},
        "hero_prefers_animation": bool(cast.get("hero_animated")),
        "scenery": [s.get("title") for s in (cast.get("placements") or [])],
        "catalog_counts": cast.get("counts") or {},
        "has_image": "image" in kinds,
        "has_video": "video" in kinds,
        "has_music": "music" in kinds,
        "has_voice": "audio" in kinds,
        "failed_jobs": failed,
        "controls": controls,
        "win_condition": win,
        "play": play,
        "session": "2-4 minutes web prototype",
        "html_features": {
            "mode": mode,
            "play": play,
            "catalog_props": "placements" in html or bool(cast.get("placements")),
            "sky": "skyTex" in html or "SphereGeometry" in html,
            "terrain": "CircleGeometry" in html or "RingGeometry" in html or "stoneTex" in html or "MAZE" in html,
            "track": racing,
            "maze": maze,
            "car": racing and bool((cast.get("hero") or {}).get("url")),
            "animations": "AnimationMixer" in html,
            "fullscreen": False,
            "concept_backdrop": "CFG.image" in html,
            "splash": "splash" in html and bool(pick_url(project, ("image",))),
            "trailer": "splash-vid" in html and bool(pick_url(project, ("video",))),
        },
    }


def pick_url(project: dict, kinds: tuple[str, ...]) -> str:
    want_audio = any(kind in {"music", "audio"} for kind in kinds)
    want_video = any(kind == "video" for kind in kinds)
    for asset in reversed(project.get("assets") or []):
        if asset.get("kind") not in kinds or not asset.get("url"):
            continue
        url = str(asset["url"])
        lower = url.lower()
        is_image = any(lower.endswith(ext) or f"{ext}?" in lower for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
        if (want_audio or want_video) and is_image:
            continue
        return url
    return ""


def write_game(project: dict) -> str:
    GAMES_DIR.mkdir(parents=True, exist_ok=True)
    project_id = str(project["id"])
    path = GAMES_DIR / f"{project_id}.html"
    path.write_text(render_game(project), encoding="utf-8")
    return game_url(project_id)


def render_game(project: dict) -> str:
    plan = project.get("plan") or {}
    title = str(plan.get("title") or project.get("title") or "Mini game")
    logline = str(plan.get("logline") or project.get("prompt") or "")
    cast = pick_cast(project)
    play = sanitize_play(project)
    arena = str(play.get("arena") or "")
    mode = {"ring": "racing", "maze": "maze"}.get(arena) or (
        detect_genre(project) if detect_genre(project) in {"racing", "maze"} else "adventure"
    )
    payload = {
        "title": title,
        "logline": logline,
        "mode": mode,
        "play": play,
        "hero": cast["hero"],
        "placements": cast.get("placements") or [],
        "image": pick_url(project, ("image",)),
        "video": pick_url(project, ("video",)),
        "music": pick_url(project, ("music", "audio")),
    }
    html = _TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("%%CONFIG%%", json.dumps(payload, ensure_ascii=False))
    html = html.replace("%%TITLE%%", _esc(title))
    return html


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def context_blob(project: dict) -> str:
    plan = project.get("plan") or {}
    genre = detect_genre(project)
    lines = [
        "SCOPE LOCK: you know ONLY this one studio project. Other projects do not exist.",
        f"Project id: {project.get('id') or ''}",
        f"Title: {project.get('title') or ''}",
        f"Status: {project.get('status') or ''}",
        f"Platform: {project.get('platform') or 'web'}",
        f"Genre: {genre}",
        f"Play spec: {json.dumps(sanitize_play(project), ensure_ascii=False)}",
        "The HTML prototype FOLLOWS this play spec (camera/move/win/jump/mouse). Do not describe a shooter unless play.mouse_look and play.camera are first.",
        "The game stays in the studio panel. Fullscreen is a user button, never automatic.",
        f"Original prompt: {project.get('prompt') or ''}",
        f"Plan JSON: {json.dumps(plan, ensure_ascii=False)[:4000]}",
        "Catalog models in THIS project (on-theme only; ignore anything else):",
    ]
    models = [
        a
        for a in (project.get("assets") or [])
        if a.get("kind") in {"model", "mesh"} and in_project_context(a, project)
    ]
    if not models:
        lines.append("- (none yet)")
    ranked = sorted(models, key=_anim_score, reverse=True)
    for asset in ranked[:16]:
        meta = asset.get("meta") or {}
        lines.append(
            f"- {asset.get('title')} role={_role(asset, genre)} anim={_anim_score(asset)} "
            f"ext={meta.get('ext') or ''} animated={meta.get('animated')} url={asset.get('url')}"
        )
    cast = pick_cast(project)
    lines.append(f"Scene placements ({len(cast.get('placements') or [])}):")
    for item in (cast.get("placements") or [])[:20]:
        lines.append(f"- {item.get('kind')} {item.get('title')} at ({item.get('x')},{item.get('z')})")
    lines.append("Generated media:")
    media = [
        a
        for a in (project.get("assets") or [])
        if a.get("kind") in {"image", "video", "audio", "music", "game"}
    ]
    if not media:
        lines.append("- (none yet)")
    for asset in media[:12]:
        lines.append(f"- {asset.get('kind')}: {asset.get('title')} -> {asset.get('url')}")
    jobs = project.get("jobs") or []
    if jobs:
        lines.append("Jobs:")
        for job in jobs[-12:]:
            err = f" error={job.get('error')}" if job.get("error") else ""
            lines.append(f"- {job.get('agent')} {job.get('status')}{err}")
    return "\n".join(lines)


def history_messages(project: dict, limit: int = 24) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for msg in (project.get("messages") or [])[-limit:]:
        role = msg.get("role") or "assistant"
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            out.append({"role": "user", "content": content[:4000]})
            continue
        prefix = "" if role == "assistant" else f"[{role}] "
        out.append({"role": "assistant", "content": (prefix + content)[:4000]})
    return out
