"""Общий конструктор клиента OpenAI для vision_tags.py и ai_describe.py.

Локальная сеть разработчика периодически теряет прямой маршрут до api.openai.com
(DNS/TCP таймауты), хотя тот же прод-сервер видит OpenAI без проблем. Поэтому
опционально пускаем трафик через SOCKS5-туннель до сервера (см. OPENAI_PROXY_URL) —
без этого воркер молча висит на таймаутах и ничего не классифицирует.
"""

from __future__ import annotations

from app.paths import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_ORG_ID, OPENAI_PROXY_URL


def make_openai_client():
    from openai import OpenAI

    client_kw: dict = {"api_key": OPENAI_API_KEY}
    if OPENAI_BASE_URL:
        client_kw["base_url"] = OPENAI_BASE_URL
    if OPENAI_ORG_ID:
        client_kw["organization"] = OPENAI_ORG_ID
    if OPENAI_PROXY_URL:
        import httpx

        client_kw["http_client"] = httpx.Client(proxy=OPENAI_PROXY_URL, timeout=60.0)
    return OpenAI(**client_kw)
