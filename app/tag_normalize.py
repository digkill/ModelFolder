import re


def normalize_tag(raw: str) -> str | None:
    s = raw.strip().lower()
    if not s:
        return None
    s = s.replace("_", "-")
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s or len(s) > 48:
        return None
    return s


def normalize_tag_list(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        n = normalize_tag(t) if isinstance(t, str) else None
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out[:16]
