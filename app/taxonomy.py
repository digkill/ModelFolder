"""Таксономия каталога: фиксированные категории, возрастной рейтинг и словарь тегов.

Зачем фиксированный список, а не «что придумает модель»: на десятках тысяч моделей
свободные категории расползаются в синонимы (`car` / `cars` / `vehicle` / `auto`),
и фасетный поиск по ним перестаёт работать. Поэтому:

  * CATEGORIES — закрытый список, в него обязан попасть каждый ассет (иначе `other`);
  * CATEGORY_ALIASES — приведение синонимов и имён папок с диска к канону;
  * TAG_FACETS — свободные теги, но сгруппированные по осям (стиль, сеттинг, ...),
    чтобы UI мог показать их отдельными фильтрами, а не одной кучей.
"""

from __future__ import annotations

from app.tag_normalize import normalize_tag

# --------------------------------------------------------------------------- #
# Категории
# --------------------------------------------------------------------------- #
CATEGORIES: tuple[str, ...] = (
    "character",       # люди, гуманоиды, персонажи
    "creature",        # животные, монстры, динозавры
    "vehicle",         # транспорт: машины, корабли, самолёты
    "weapon",          # оружие и снаряжение
    "prop",            # предметы, мебель, реквизит
    "environment",     # локации, ландшафты, сцены
    "building",        # здания и архитектура
    "nature",          # растения, камни, органика
    "furniture",       # мебель и интерьер
    "food",            # еда и напитки
    "clothing",        # одежда, броня, аксессуары
    "vfx",             # эффекты, партиклы
    "animation",       # анимации и риги
    "material",        # материалы и текстуры
    "ui",              # интерфейсные элементы, иконки
    "vehicle-part",    # детали транспорта
    "scene-kit",       # наборы модульных ассетов
    "other",
)

CATEGORY_SET = frozenset(CATEGORIES)
FALLBACK_CATEGORY = "other"

# Синонимы → канон. Ключи нормализуются тем же normalize_tag, что и теги,
# поэтому «Modern War» и «modern-war» попадут в одну запись.
CATEGORY_ALIASES: dict[str, str] = {
    # персонажи
    "characters": "character",
    "hero": "character",
    "heroes": "character",
    "heroeslowpoly": "character",
    "people": "character",
    "human": "character",
    "humanoid": "character",
    "anime": "character",
    "animehero": "character",
    "npc": "character",
    "avatar": "character",
    # существа
    "creatures": "creature",
    "animal": "creature",
    "animals": "creature",
    "monster": "creature",
    "monsters": "creature",
    "dino": "creature",
    "dinosaur": "creature",
    "dinosaurs": "creature",
    "pet": "creature",
    "pets": "creature",
    "fish": "creature",
    "bird": "creature",
    "insect": "creature",
    # транспорт
    "vehicles": "vehicle",
    "car": "vehicle",
    "cars": "vehicle",
    "truck": "vehicle",
    "ship": "vehicle",
    "ships": "vehicle",
    "boat": "vehicle",
    "plane": "vehicle",
    "aircraft": "vehicle",
    "spaceship": "vehicle",
    "spacecraft": "vehicle",
    "tank": "vehicle",
    "bike": "vehicle",
    "motorcycle": "vehicle",
    # оружие
    "weapons": "weapon",
    "gun": "weapon",
    "guns": "weapon",
    "firearm": "weapon",
    "sword": "weapon",
    "melee": "weapon",
    "armor-set": "weapon",
    # реквизит
    "props": "prop",
    "object": "prop",
    "objects": "prop",
    "item": "prop",
    "items": "prop",
    "tool": "prop",
    "tools": "prop",
    "different": "prop",
    "download": "prop",
    "anyware": "prop",
    # окружение
    "environments": "environment",
    "location": "environment",
    "locations": "environment",
    "level": "environment",
    "map": "environment",
    "terrain": "environment",
    "landscape": "environment",
    "cities": "environment",
    "city": "environment",
    "rpg-location-low-poly": "environment",
    # здания
    "buildings": "building",
    "house": "building",
    "architecture": "building",
    "structure": "building",
    "interior": "building",
    # природа
    "tree": "nature",
    "trees": "nature",
    "plant": "nature",
    "plants": "nature",
    "rock": "nature",
    "stone": "nature",
    "foliage": "nature",
    # мебель
    "furnitures": "furniture",
    "chair": "furniture",
    "table": "furniture",
    "decor": "furniture",
    # еда
    "foods": "food",
    "drink": "food",
    "fruit": "food",
    # одежда
    "clothes": "clothing",
    "armor": "clothing",
    "outfit": "clothing",
    "accessory": "clothing",
    "hat": "clothing",
    "shoes": "clothing",
    # эффекты и прочее
    "effect": "vfx",
    "effects": "vfx",
    "particle": "vfx",
    "particles": "vfx",
    "animations": "animation",
    "animated": "animation",
    "rig": "animation",
    "materials": "material",
    "texture": "material",
    "textures": "material",
    "materialstextures": "material",
    "materials-textures": "material",
    "shader": "material",
    "icon": "ui",
    "icons": "ui",
    "gui": "ui",
    "hud": "ui",
    "kit": "scene-kit",
    "pack": "scene-kit",
    "modular": "scene-kit",
    "asset-pack": "scene-kit",
    "rpg-assets-low-poly": "scene-kit",
    "moba": "scene-kit",
    "modern-war": "scene-kit",
}


def canonical_category(raw: str | None) -> str | None:
    """Приводит произвольную строку (имя папки, ответ модели) к канонической категории.

    Возвращает None, если сопоставить не удалось — вызывающий решает, ставить ли
    FALLBACK_CATEGORY или попробовать другой источник (папку вместо ответа AI).
    """
    if not raw:
        return None
    key = normalize_tag(str(raw))
    if not key:
        return None
    if key in CATEGORY_SET:
        return key
    if key in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[key]
    # «rpg-assets-low-poly» → пробуем по последнему и первому слову.
    parts = key.split("-")
    for candidate in (parts[-1], parts[0]):
        if candidate in CATEGORY_SET:
            return candidate
        if candidate in CATEGORY_ALIASES:
            return CATEGORY_ALIASES[candidate]
    return None


# --------------------------------------------------------------------------- #
# Возрастной рейтинг
# --------------------------------------------------------------------------- #
# Порядок важен: рейтинг монотонно ужесточается, сравнение идёт по индексу.
AGE_RATINGS: tuple[str, ...] = ("everyone", "teen", "mature", "adult")
AGE_RATING_INDEX = {name: i for i, name in enumerate(AGE_RATINGS)}
DEFAULT_AGE_RATING = "everyone"


def canonical_age_rating(raw: str | None) -> str | None:
    if not raw:
        return None
    key = normalize_tag(str(raw))
    if not key:
        return None
    aliases = {
        "e": "everyone",
        "all": "everyone",
        "kids": "everyone",
        "child": "everyone",
        "children": "everyone",
        "g": "everyone",
        "t": "teen",
        "teenager": "teen",
        "pg-13": "teen",
        "m": "mature",
        "17-plus": "mature",
        "r": "mature",
        "18-plus": "adult",
        "nsfw": "adult",
        "x": "adult",
        "explicit": "adult",
    }
    if key in AGE_RATING_INDEX:
        return key
    return aliases.get(key)


def ratings_up_to(maximum: str | None) -> list[str]:
    """Рейтинги не жёстче указанного — для фильтра «покажи до teen включительно»."""
    if not maximum:
        return []
    canon = canonical_age_rating(maximum)
    if canon is None:
        return []
    top = AGE_RATING_INDEX[canon]
    return [name for name, i in AGE_RATING_INDEX.items() if i <= top]


def at_least(rating: str | None, minimum: str) -> bool:
    """True, если rating не мягче minimum (для фильтра «не жёстче чем»)."""
    a = AGE_RATING_INDEX.get(rating or DEFAULT_AGE_RATING, 0)
    b = AGE_RATING_INDEX.get(minimum, 0)
    return a >= b


# --------------------------------------------------------------------------- #
# Фасеты тегов
# --------------------------------------------------------------------------- #
# Теги остаются свободными (AI может придумать новый), но известные раскладываются
# по осям — UI показывает их как отдельные группы фильтров.
TAG_FACETS: dict[str, tuple[str, ...]] = {
    "style": (
        "low-poly",
        "high-poly",
        "realistic",
        "stylized",
        "cartoon",
        "pixel-art",
        "voxel",
        "hand-painted",
        "pbr",
        "toon",
        "flat-shaded",
        "photogrammetry",
        "sculpt",
    ),
    "setting": (
        "fantasy",
        "sci-fi",
        "medieval",
        "modern",
        "historical",
        "post-apocalyptic",
        "cyberpunk",
        "steampunk",
        "horror",
        "western",
        "space",
        "underwater",
        "military",
        "urban",
        "rural",
        "anime",
    ),
    "usage": (
        "game-ready",
        "rigged",
        "animated",
        "modular",
        "vr-ready",
        "mobile-ready",
        "printable",
        "static",
    ),
    "content": (
        "18-plus",
        "nudity",
        "violence",
        "gore",
        "weapons",
        "drugs",
        "self-harm",
        "suggestive",
        "blood",
    ),
}

# tag -> facet (обратный индекс, строится один раз)
TAG_TO_FACET: dict[str, str] = {
    tag: facet for facet, tags in TAG_FACETS.items() for tag in tags
}

# Теги, из-за которых модель нельзя показывать в детском режиме, даже если
# возрастной рейтинг почему-то не проставлен.
KID_UNSAFE_TAGS = frozenset(
    {"18-plus", "nudity", "gore", "self-harm", "drugs", "suggestive", "horror"}
)


def facet_of(tag: str) -> str | None:
    return TAG_TO_FACET.get(tag)


def split_by_facet(tags: list[str]) -> dict[str, list[str]]:
    """Раскладывает список тегов по фасетам; неизвестные попадают в `free`."""
    out: dict[str, list[str]] = {facet: [] for facet in TAG_FACETS}
    out["free"] = []
    for tag in tags:
        out.setdefault(TAG_TO_FACET.get(tag, "free"), []).append(tag)
    return out
