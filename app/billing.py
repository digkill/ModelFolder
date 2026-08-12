"""Тарифы, подписки и квоты сервисного API.

Счёт выставляется при подписке. Оплата — через ЮKassa (ссылка + webhook)
или вручную в админке. Лимиты тарифа применяются к ключам клиента на /v1.
"""

from __future__ import annotations

import time
from calendar import monthrange
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.api_keys import ALL_SCOPES, normalize_scopes

PERIODS = ("month", "year", "once")
SUB_STATUSES = ("trialing", "active", "past_due", "canceled")
INVOICE_STATUSES = ("issued", "paid", "void")

# 0 в лимитах = без ограничения.
DEFAULT_PLANS: list[dict[str, Any]] = [
    {
        "slug": "free",
        "name": "Free",
        "description": "Оценка API: поиск по каталогу и немного скачиваний.",
        "price_cents": 0,
        "currency": "RUB",
        "period": "month",
        "requests_per_period": 1_000,
        "downloads_per_period": 50,
        "bytes_per_period": 512 * 1024 * 1024,
        "searches_per_period": 0,
        "rate_limit_per_min": 30,
        "max_api_keys": 1,
        "max_page_size": 24,
        "scopes": ["catalog", "download"],
        "trial_days": 0,
        "sort_order": 10,
    },
    {
        "slug": "starter",
        "name": "Starter",
        "description": "Небольшой продакшен: семантический поиск и сотни выдач моделей.",
        "price_cents": 199_000,
        "currency": "RUB",
        "period": "month",
        "requests_per_period": 20_000,
        "downloads_per_period": 500,
        "bytes_per_period": 20 * 1024 * 1024 * 1024,
        "searches_per_period": 2_000,
        "rate_limit_per_min": 120,
        "max_api_keys": 3,
        "max_page_size": 50,
        "scopes": list(ALL_SCOPES),
        "trial_days": 7,
        "sort_order": 20,
    },
    {
        "slug": "pro",
        "name": "Pro",
        "description": "Боевая нагрузка студии или игры: тысячи скачиваний в месяц.",
        "price_cents": 799_000,
        "currency": "RUB",
        "period": "month",
        "requests_per_period": 100_000,
        "downloads_per_period": 5_000,
        "bytes_per_period": 100 * 1024 * 1024 * 1024,
        "searches_per_period": 20_000,
        "rate_limit_per_min": 300,
        "max_api_keys": 10,
        "max_page_size": 100,
        "scopes": list(ALL_SCOPES),
        "trial_days": 7,
        "sort_order": 30,
    },
    {
        "slug": "studio",
        "name": "Studio",
        "description": "Без квот по запросам и скачиваниям, повышенный rate limit.",
        "price_cents": 2_499_000,
        "currency": "RUB",
        "period": "month",
        "requests_per_period": 0,
        "downloads_per_period": 0,
        "bytes_per_period": 0,
        "searches_per_period": 0,
        "rate_limit_per_min": 600,
        "max_api_keys": 50,
        "max_page_size": 100,
        "scopes": list(ALL_SCOPES),
        "trial_days": 0,
        "sort_order": 40,
    },
]


def add_period(ts: float, period: str) -> float:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if period == "once":
        return dt.replace(year=min(dt.year + 100, 9999)).timestamp()
    if period == "year":
        year = dt.year + 1
        day = min(dt.day, monthrange(year, dt.month)[1])
        return dt.replace(year=year, day=day).timestamp()
    month = dt.month + 1
    year = dt.year
    if month > 12:
        month = 1
        year += 1
    day = min(dt.day, monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day).timestamp()


def format_money(cents: int, currency: str = "RUB") -> str:
    major = (cents or 0) / 100
    if currency.upper() == "RUB":
        if major == int(major):
            return f"{int(major)} ₽"
        return f"{major:.2f} ₽"
    return f"{major:.2f} {currency}"


def quota_view(used: int, limit: int) -> dict:
    unlimited = int(limit or 0) <= 0
    remaining = None if unlimited else max(0, int(limit) - int(used or 0))
    return {
        "used": int(used or 0),
        "limit": None if unlimited else int(limit),
        "remaining": remaining,
        "unlimited": unlimited,
    }


def attach_to_key(key: dict[str, Any] | None) -> dict[str, Any] | None:
    """Дополняет карточку ключа тарифом и подпиской. Без клиента ключ внутренний."""
    if not key:
        return key
    customer_id = key.get("customer_id")
    if not customer_id:
        key["billing"] = None
        return key
    import app.db as db

    ctx = db.resolve_billing(int(customer_id), time.time())
    key["billing"] = ctx
    plan = (ctx or {}).get("plan")
    if plan:
        plan_scopes = normalize_scopes(plan.get("scopes"))
        key["scopes"] = [s for s in (key.get("scopes") or []) if s in plan_scopes]
        if not key["scopes"]:
            key["scopes"] = list(plan_scopes)
        key["rate_limit_per_min"] = int(plan.get("rate_limit_per_min") or 0)
        key["max_page_size"] = int(plan.get("max_page_size") or 24)
    return key


def _quota_exceeded(used: int, limit: int, extra: int = 1) -> bool:
    if int(limit or 0) <= 0:
        return False
    return int(used or 0) + extra > int(limit)


def enforce(client: dict[str, Any] | None, meter: str) -> None:
    """Проверяет статус подписки и квоту. meter: request | download | search."""
    if client is None:
        return
    ctx = client.get("billing")
    if not client.get("customer_id"):
        return
    if not ctx or ctx.get("status") in (None, "none", "canceled"):
        raise HTTPException(
            status_code=402,
            detail="No active subscription. Assign a plan to this customer.",
        )
    status = ctx.get("status")
    if status == "past_due":
        raise HTTPException(
            status_code=402,
            detail="Subscription past due. Pay the outstanding invoice to resume API access.",
        )
    if status not in ("active", "trialing"):
        raise HTTPException(status_code=402, detail=f"Subscription is {status}")
    plan = ctx.get("plan") or {}
    usage = ctx.get("usage") or {}
    if meter == "request" and _quota_exceeded(
        usage.get("requests", 0), plan.get("requests_per_period", 0)
    ):
        raise HTTPException(status_code=402, detail="Request quota exceeded for this billing period")
    if meter == "download" and _quota_exceeded(
        usage.get("downloads", 0), plan.get("downloads_per_period", 0)
    ):
        raise HTTPException(status_code=402, detail="Download quota exceeded for this billing period")
    if meter == "search" and _quota_exceeded(
        usage.get("searches", 0), plan.get("searches_per_period", 0)
    ):
        raise HTTPException(status_code=402, detail="Semantic search quota exceeded for this billing period")


def enforce_bytes(client: dict[str, Any] | None, extra_bytes: int) -> None:
    if client is None or not client.get("customer_id"):
        return
    ctx = client.get("billing") or {}
    plan = ctx.get("plan") or {}
    usage = ctx.get("usage") or {}
    if _quota_exceeded(usage.get("bytes", 0), plan.get("bytes_per_period", 0), extra_bytes):
        raise HTTPException(status_code=402, detail="Traffic quota exceeded for this billing period")


def meter(
    client: dict[str, Any] | None,
    *,
    requests: int = 0,
    downloads: int = 0,
    bytes_: int = 0,
    searches: int = 0,
) -> None:
    if client is None:
        return
    import app.db as db

    ctx = client.get("billing") or {}
    sub = ctx.get("subscription")
    if sub:
        db.meter_subscription(
            int(sub["id"]),
            requests=requests,
            downloads=downloads,
            bytes_=bytes_,
            searches=searches,
        )
        usage = ctx.setdefault("usage", {})
        usage["requests"] = int(usage.get("requests") or 0) + requests
        usage["downloads"] = int(usage.get("downloads") or 0) + downloads
        usage["bytes"] = int(usage.get("bytes") or 0) + bytes_
        usage["searches"] = int(usage.get("searches") or 0) + searches
    db.bump_usage_daily(
        customer_id=int(client.get("customer_id") or 0),
        key_id=int(client["id"]),
        requests=requests,
        downloads=downloads,
        bytes_=bytes_,
        searches=searches,
    )


def public_billing(client: dict[str, Any] | None) -> dict[str, Any] | None:
    if not client:
        return None
    import app.db as db

    ctx = client.get("billing")
    if not ctx:
        return {"mode": "internal", "note": "Ключ без клиента — квоты тарифа не применяются"}
    plan = ctx.get("plan")
    usage = ctx.get("usage") or {}
    sub = ctx.get("subscription")
    if not plan or not sub:
        return {"mode": "unsubscribed", "status": ctx.get("status") or "none"}
    out = {
        "mode": "subscription",
        "status": ctx.get("status"),
        "plan": {
            "id": plan["id"],
            "slug": plan["slug"],
            "name": plan["name"],
            "price": format_money(plan["price_cents"], plan.get("currency") or "RUB"),
            "period": plan["period"],
        },
        "period_start": sub.get("period_start"),
        "period_end": sub.get("period_end"),
        "auto_renew": bool(sub.get("auto_renew")),
        "quotas": {
            "requests": quota_view(usage.get("requests", 0), plan.get("requests_per_period", 0)),
            "downloads": quota_view(usage.get("downloads", 0), plan.get("downloads_per_period", 0)),
            "bytes": quota_view(usage.get("bytes", 0), plan.get("bytes_per_period", 0)),
            "searches": quota_view(usage.get("searches", 0), plan.get("searches_per_period", 0)),
        },
        "rate_limit_per_min": plan.get("rate_limit_per_min"),
        "max_page_size": plan.get("max_page_size"),
    }
    customer_id = client.get("customer_id")
    if customer_id:
        from app import yookassa

        invoice = db.get_open_invoice_for_customer(int(customer_id))
        if invoice:
            out["unpaid_invoice"] = {
                "id": invoice["id"],
                "amount": format_money(
                    invoice.get("amount_cents") or 0, invoice.get("currency") or "RUB"
                ),
                "pay_url": yookassa.public_pay_url(invoice["id"]) if yookassa.configured() else None,
            }
    return out


def cap_page_size(client: dict[str, Any] | None, limit: int) -> int:
    if client is None:
        return limit
    max_size = int(client.get("max_page_size") or 0)
    if max_size <= 0:
        return limit
    return max(1, min(limit, max_size))
