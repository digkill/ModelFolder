"""Оплата счетов через ЮKassa: создание платежа, страница /pay, webhook.

Секрет магазина не светится в ответах API. Статус платежа из уведомления
не принимаем на веру — всегда перечитываем платёж GET /v3/payments/{id}.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import app.db as db
from app.paths import (
    API_BASE_URL,
    AUTH_SECRET,
    YOOKASSA_RECEIPT,
    YOOKASSA_RETURN_URL,
    YOOKASSA_SECRET_KEY,
    YOOKASSA_SHOP_ID,
    YOOKASSA_VAT_CODE,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["yookassa"])

API_ROOT = "https://api.yookassa.ru/v3"
_PAY_PAGE = Path(__file__).resolve().parent.parent / "static" / "pay.html"


class YooKassaError(Exception):
    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload or {}


def configured() -> bool:
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)


def _token_secret() -> bytes:
    raw = YOOKASSA_SECRET_KEY or AUTH_SECRET or "modelfolder-pay"
    return hashlib.sha256(raw.encode()).digest()


def pay_token(invoice_id: int) -> str:
    msg = f"invoice:{int(invoice_id)}".encode()
    return hmac.new(_token_secret(), msg, hashlib.sha256).hexdigest()[:32]


def verify_pay_token(invoice_id: int, token: str | None) -> bool:
    expected = pay_token(invoice_id)
    given = (token or "").strip()
    if len(given) != len(expected):
        return False
    return hmac.compare_digest(expected, given)


def public_pay_url(invoice_id: int, base: str | None = None) -> str:
    root = (base or API_BASE_URL or "").rstrip("/")
    path = f"/pay/{int(invoice_id)}?t={pay_token(invoice_id)}"
    return f"{root}{path}" if root else path


def amount_value(cents: int) -> str:
    return f"{(cents or 0) / 100:.2f}"


def _api(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    idempotence_key: str | None = None,
) -> dict:
    if not configured():
        raise YooKassaError(
            "ЮKassa не настроена: задайте YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY"
        )
    import base64

    url = f"{API_ROOT}{path}"
    headers = {
        "Authorization": "Basic "
        + base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = UrlRequest(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"description": raw or str(e)}
        message = payload.get("description") or payload.get("code") or f"HTTP {e.code}"
        raise YooKassaError(str(message), payload=payload) from e
    except URLError as e:
        raise YooKassaError(f"ЮKassa недоступна: {e.reason}") from e


def get_payment(payment_id: str) -> dict:
    return _api("GET", f"/payments/{payment_id}")


def cancel_payment(payment_id: str) -> dict:
    return _api("POST", f"/payments/{payment_id}/cancel", body={}, idempotence_key=str(uuid.uuid4()))


def _confirmation_url(payment: dict) -> str | None:
    conf = payment.get("confirmation") or {}
    return conf.get("confirmation_url")


def _store_payment(invoice_id: int, payment: dict) -> None:
    db.update_invoice_yookassa(
        invoice_id,
        payment_id=payment.get("id"),
        status=payment.get("status"),
        confirmation_url=_confirmation_url(payment),
    )


def apply_payment(payment: dict) -> dict | None:
    """Синхронизирует счёт с платежом ЮKassa. При succeeded закрывает счёт."""
    import time

    payment_id = payment.get("id") or ""
    metadata = payment.get("metadata") or {}
    invoice_id_raw = metadata.get("invoice_id")
    invoice = None
    if invoice_id_raw not in (None, ""):
        try:
            invoice = db.get_billing_invoice(int(invoice_id_raw))
        except (TypeError, ValueError):
            invoice = None
    if invoice is None and payment_id:
        invoice = db.get_invoice_by_yookassa_id(str(payment_id))
    if invoice is None:
        log.warning("ЮKassa: платёж %s без известного счёта", payment_id)
        return None
    _store_payment(invoice["id"], payment)
    status = payment.get("status")
    if status == "succeeded" and invoice.get("status") == "issued":
        db.pay_invoice(invoice["id"], time.time())
        log.info("ЮKassa: счёт %s оплачен платежом %s", invoice["id"], payment_id)
    elif status == "canceled":
        log.info("ЮKassa: платёж %s по счёту %s отменён", payment_id, invoice["id"])
    return db.get_billing_invoice(invoice["id"])


def _return_url(invoice_id: int, base: str) -> str:
    override = YOOKASSA_RETURN_URL
    root = (override or base).rstrip("/")
    if override and override.endswith("/pay/return"):
        return f"{root}?invoice_id={invoice_id}&t={pay_token(invoice_id)}"
    return f"{root}/pay/return?invoice_id={invoice_id}&t={pay_token(invoice_id)}"


def _receipt(invoice: dict, customer: dict | None) -> dict | None:
    if not YOOKASSA_RECEIPT:
        return None
    email = ((customer or {}).get("email") or "").strip()
    if not email or "@" not in email:
        return None
    description = (invoice.get("description") or f"Счёт №{invoice['id']}")[:128]
    return {
        "customer": {"email": email},
        "items": [
            {
                "description": description,
                "quantity": "1.00",
                "amount": {
                    "value": amount_value(invoice.get("amount_cents") or 0),
                    "currency": (invoice.get("currency") or "RUB").upper(),
                },
                "vat_code": YOOKASSA_VAT_CODE,
                "payment_subject": "service",
                "payment_mode": "full_payment",
            }
        ],
    }


def create_payment_for_invoice(invoice: dict, *, return_base: str) -> dict:
    if invoice.get("status") == "paid":
        return {
            "id": invoice["id"],
            "status": "paid",
            "already": True,
            "pay_url": public_pay_url(invoice["id"], return_base),
        }
    if invoice.get("status") != "issued":
        raise YooKassaError("Счёт нельзя оплатить в текущем статусе")
    if int(invoice.get("amount_cents") or 0) <= 0:
        raise YooKassaError("Нулевой счёт не требует оплаты через ЮKassa")

    existing_id = invoice.get("yookassa_payment_id")
    if existing_id:
        payment = get_payment(str(existing_id))
        status = payment.get("status")
        if status == "succeeded":
            updated = apply_payment(payment)
            return {
                "id": invoice["id"],
                "status": "paid",
                "already": True,
                "yookassa_payment_id": payment.get("id"),
                "pay_url": public_pay_url(invoice["id"], return_base),
                "invoice": updated,
            }
        if status in ("pending", "waiting_for_capture"):
            _store_payment(invoice["id"], payment)
            url = _confirmation_url(payment)
            return {
                "id": invoice["id"],
                "status": invoice["status"],
                "yookassa_payment_id": payment.get("id"),
                "yookassa_status": status,
                "confirmation_url": url,
                "pay_url": public_pay_url(invoice["id"], return_base),
            }

    customer = db.get_billing_customer(int(invoice["customer_id"]))
    description = (invoice.get("description") or f"Счёт №{invoice['id']}")[:128]
    body: dict = {
        "amount": {
            "value": amount_value(invoice.get("amount_cents") or 0),
            "currency": (invoice.get("currency") or "RUB").upper(),
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": _return_url(int(invoice["id"]), return_base),
        },
        "description": description,
        "metadata": {"invoice_id": str(invoice["id"])},
    }
    receipt = _receipt(invoice, customer)
    if receipt:
        body["receipt"] = receipt
    payment = _api("POST", "/payments", body=body, idempotence_key=str(uuid.uuid4()))
    _store_payment(invoice["id"], payment)
    return {
        "id": invoice["id"],
        "status": invoice["status"],
        "yookassa_payment_id": payment.get("id"),
        "yookassa_status": payment.get("status"),
        "confirmation_url": _confirmation_url(payment),
        "pay_url": public_pay_url(invoice["id"], return_base),
    }


def sync_invoice(invoice: dict) -> dict:
    payment_id = invoice.get("yookassa_payment_id")
    if not payment_id or not configured():
        return invoice
    try:
        payment = get_payment(str(payment_id))
    except YooKassaError as e:
        log.warning("ЮKassa: не удалось перечитать платёж %s: %s", payment_id, e)
        return invoice
    return apply_payment(payment) or invoice


def request_base(request: Request) -> str:
    if API_BASE_URL:
        return API_BASE_URL
    forwarded = request.headers.get("x-forwarded-proto")
    scheme = forwarded or request.url.scheme
    return str(request.base_url).replace(f"{request.url.scheme}://", f"{scheme}://", 1).rstrip("/")


def _pay_html(
    *,
    title: str,
    lead: str,
    amount: str = "",
    meta: str = "",
    action: str = "",
    note: str = "",
    state: str = "wait",
) -> HTMLResponse:
    page = _PAY_PAGE
    if not page.is_file():
        return HTMLResponse(f"<p>{escape(lead)}</p>", status_code=200)
    html = page.read_text(encoding="utf-8")
    html = (
        html.replace("__TITLE__", escape(title))
        .replace("__LEAD__", escape(lead))
        .replace("__AMOUNT__", escape(amount))
        .replace("__META__", escape(meta))
        .replace("__ACTION__", action)
        .replace("__NOTE__", escape(note))
        .replace("__STATE__", escape(state))
    )
    return HTMLResponse(html)


def _invoice_or_404(invoice_id: int, token: str) -> dict:
    if not verify_pay_token(invoice_id, token):
        raise HTTPException(status_code=403, detail="Недействительная ссылка на оплату")
    invoice = db.get_billing_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return invoice


@router.post("/api/billing/yookassa/webhook")
async def yookassa_webhook(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Ожидался JSON") from None
    event = payload.get("event") if isinstance(payload, dict) else None
    obj = payload.get("object") if isinstance(payload, dict) else None
    if not isinstance(obj, dict) or not obj.get("id"):
        return {"ok": False, "reason": "no_payment"}
    if not configured():
        log.warning("ЮKassa webhook %s, но магазин не настроен", event)
        return {"ok": False, "reason": "not_configured"}
    try:
        payment = get_payment(str(obj["id"]))
    except YooKassaError as e:
        log.warning("ЮKassa webhook: не удалось получить платёж %s: %s", obj.get("id"), e)
        raise HTTPException(status_code=502, detail="Не удалось проверить платёж") from e
    invoice = apply_payment(payment)
    return {
        "ok": True,
        "event": event,
        "payment_id": payment.get("id"),
        "status": payment.get("status"),
        "invoice_id": None if invoice is None else invoice["id"],
        "invoice_status": None if invoice is None else invoice.get("status"),
    }


@router.get("/pay/return", response_class=HTMLResponse)
def pay_return(
    request: Request,
    invoice_id: int = Query(...),
    t: str = Query(""),
) -> HTMLResponse:
    invoice = _invoice_or_404(invoice_id, t)
    invoice = sync_invoice(invoice)
    if invoice.get("status") == "paid":
        return _pay_html(
            title="Оплата прошла",
            lead="Счёт оплачен, доступ по API восстановлен.",
            amount=_amount_label(invoice),
            meta=invoice.get("description") or "",
            state="ok",
        )
    if invoice.get("yookassa_status") == "canceled":
        action = (
            f'<form method="post" action="/pay/{invoice["id"]}?t={escape(t)}">'
            '<button type="submit">Оплатить снова</button></form>'
        )
        return _pay_html(
            title="Платёж не завершён",
            lead="Оплата отменена или не прошла. Можно попробовать ещё раз.",
            amount=_amount_label(invoice),
            meta=invoice.get("description") or "",
            action=action,
            state="err",
        )
    return _pay_html(
        title="Ждём подтверждение",
        lead="ЮKassa ещё не подтвердила платёж. Страницу можно закрыть — статус обновится сам.",
        amount=_amount_label(invoice),
        meta=invoice.get("description") or "",
        note="Обычно это занимает несколько секунд.",
        state="wait",
    )


@router.get("/pay/{invoice_id}", response_class=HTMLResponse)
def pay_page(invoice_id: int, request: Request, t: str = "") -> HTMLResponse:
    invoice = _invoice_or_404(invoice_id, t)
    invoice = sync_invoice(invoice)
    if invoice.get("status") == "paid":
        return _pay_html(
            title="Счёт уже оплачен",
            lead="Повторная оплата не нужна.",
            amount=_amount_label(invoice),
            meta=invoice.get("description") or "",
            state="ok",
        )
    if invoice.get("status") != "issued":
        return _pay_html(
            title="Счёт недоступен",
            lead="Этот счёт аннулирован или закрыт.",
            state="err",
        )
    if not configured():
        return _pay_html(
            title="Оплата недоступна",
            lead="Приём платежей через ЮKassa сейчас выключен.",
            state="err",
        )
    action = (
        f'<form method="post" action="/pay/{invoice["id"]}?t={escape(t)}">'
        '<button type="submit">Оплатить через ЮKassa</button></form>'
    )
    return _pay_html(
        title="Оплата счёта",
        lead="После оплаты доступ по API включается автоматически.",
        amount=_amount_label(invoice),
        meta=invoice.get("description") or invoice.get("plan_name") or "",
        action=action,
        note="Откроется защищённая страница ЮKassa.",
        state="wait",
    )


@router.post("/pay/{invoice_id}")
def pay_start(invoice_id: int, request: Request, t: str = "") -> RedirectResponse:
    invoice = _invoice_or_404(invoice_id, t)
    if invoice.get("status") == "paid":
        return RedirectResponse(
            f"/pay/return?invoice_id={invoice_id}&t={t}", status_code=303
        )
    try:
        result = create_payment_for_invoice(invoice, return_base=request_base(request))
    except YooKassaError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    url = result.get("confirmation_url")
    if result.get("status") == "paid" or not url:
        return RedirectResponse(
            f"/pay/return?invoice_id={invoice_id}&t={t}", status_code=303
        )
    return RedirectResponse(url, status_code=303)


def _amount_label(invoice: dict) -> str:
    from app.billing import format_money

    return format_money(invoice.get("amount_cents") or 0, invoice.get("currency") or "RUB")
