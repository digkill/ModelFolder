"""Авторизация витрины: форма входа + подписанная cookie сессии.

Каталог выложен в интернет, поэтому закрыт целиком — и UI, и API. Реализация
намеренно без внешних зависимостей и без серверного хранилища сессий: cookie
несёт имя пользователя и срок годности, подписанные HMAC-SHA256. Подделать её
без секрета нельзя, а сервер остаётся stateless (переживает рестарт контейнера).

Помимо cookie принимается HTTP Basic — чтобы скрипты и внешние приложения могли
ходить в /api/... без прохождения формы.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time

from app.paths import (
    AUTH_PASSWORD,
    AUTH_SECRET,
    AUTH_SESSION_TTL_SEC,
    AUTH_USERNAME,
)

log = logging.getLogger(__name__)

COOKIE_NAME = "mf_session"

# Пути, доступные без авторизации: сама форма входа и health-check (по нему
# docker/traefik определяют живость контейнера — он не должен требовать логина).
# Оплата ЮKassa тоже публичная: webhook приходит с их серверов, а /pay —
# страница, которую открывает клиент по ссылке из письма.
PUBLIC_PATHS = frozenset({"/login", "/logout", "/api/health", "/favicon.ico", "/api/webhooks/kie"})


def is_enabled() -> bool:
    """Авторизация включается автоматически, когда заданы логин и пароль."""
    return bool(AUTH_USERNAME and AUTH_PASSWORD)


def _secret() -> bytes:
    if AUTH_SECRET:
        return AUTH_SECRET.encode()
    # Запасной вариант: детерминированно выводим секрет из пароля, чтобы
    # сессии переживали рестарт даже когда AUTH_SECRET не задан явно.
    return hashlib.sha256(f"{AUTH_USERNAME}:{AUTH_PASSWORD}".encode()).digest()


def _sign(payload: bytes) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(_secret(), payload, hashlib.sha256).digest()
    ).decode().rstrip("=")


def verify_credentials(username: str, password: str) -> bool:
    """Сравнение в постоянном времени — иначе логин уязвим к timing-атаке."""
    if not is_enabled():
        return False
    ok_user = hmac.compare_digest((username or "").encode(), AUTH_USERNAME.encode())
    ok_pass = hmac.compare_digest((password or "").encode(), AUTH_PASSWORD.encode())
    return ok_user and ok_pass


def make_session_token(username: str) -> str:
    payload = json.dumps(
        {"u": username, "exp": int(time.time()) + AUTH_SESSION_TTL_SEC},
        separators=(",", ":"),
    ).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(payload)}"


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_session_token(token: str | None) -> str | None:
    """Возвращает имя пользователя из валидной cookie, иначе None."""
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    try:
        payload = _b64decode(body)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    username = str(data.get("u") or "")
    # Логин мог смениться в конфиге — старые cookie тогда недействительны.
    return username if username == AUTH_USERNAME else None


def _basic_auth_user(header: str | None) -> str | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8", "replace")
    except (ValueError, IndexError):
        return None
    username, _, password = raw.partition(":")
    return username if verify_credentials(username, password) else None


def authenticated_user(request) -> str | None:
    """Пользователь запроса: сначала cookie сессии, затем HTTP Basic."""
    user = validate_session_token(request.cookies.get(COOKIE_NAME))
    if user:
        return user
    return _basic_auth_user(request.headers.get("authorization"))


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path == "/pay" or path.startswith("/pay/"):
        return True
    if path == "/api/billing/yookassa/webhook":
        return True
    return False


def is_service_path(path: str) -> bool:
    """Сервисный API выдачи моделей: ключ Bearer, не логин витрины."""
    return path == "/v1" or path.startswith("/v1/")


def set_session_cookie(response, username: str, *, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        make_session_token(username),
        max_age=AUTH_SESSION_TTL_SEC,
        httponly=True,       # недоступна из JS — защита от кражи через XSS
        samesite="lax",
        secure=secure,       # по HTTPS не даём отправлять cookie в открытом виде
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")
