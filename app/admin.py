"""Админка: ручная загрузка моделей архивом или папкой.

Загруженное проходит ровно тот же конвейер, что и локальный `python -m app.ingest`:
пара «модель + одноимённое превью», sha256 для отсечения дублей, метаданные из
заголовка glTF, заливка папки целиком в S3. Дублировать эту логику отдельной
веткой нельзя — разошлась бы с основной и дала бы каталог из двух сортов записей.

Файлы принимаются во временную папку вида <tmp>/<категория>/<имя модели>/…,
потому что категорию и ключ в хранилище ingest выводит из структуры каталога.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

import app.db as db
from app import api_keys, billing, yookassa
from app.ingest import iter_candidates, run_upload
from app.paths import INGEST_IMAGE_EXTENSIONS, MODEL_EXTENSIONS
from app.taxonomy import FALLBACK_CATEGORY, canonical_category
from app.vpath import is_safe_zip_member

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

_ARCHIVE_SUFFIXES = {".zip"}
_CHUNK = 4 * 1024 * 1024


def _safe_relative(raw: str) -> PurePosixPath | None:
    """Путь из браузера — недоверенный ввод: режем traversal и абсолютные пути."""
    cleaned = (raw or "").replace("\\", "/").strip().lstrip("/")
    if not cleaned:
        return None
    parts = [p for p in PurePosixPath(cleaned).parts if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return PurePosixPath(*parts)


async def _save_upload(upload: UploadFile, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(dest, "wb") as out:
        while True:
            chunk = await upload.read(_CHUNK)
            if not chunk:
                break
            out.write(chunk)
            size += len(chunk)
    return size


def _extract_archive(archive: Path, dest: Path) -> int:
    """Распаковка с защитой от zip-slip; возвращает число файлов."""
    count = 0
    with zipfile.ZipFile(archive, "r") as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            if not is_safe_zip_member(member):
                log.warning("Пропущен небезопасный путь в архиве: %s", member)
                continue
            rel = _safe_relative(member)
            if rel is None:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
            count += 1
    return count


def _normalize_layout(staged: Path, default_category: str) -> Path:
    """Приводит загруженное к <root>/<категория>/<папка модели>/файлы.

    Принимаем что угодно: одну папку модели, набор файлов или архив с целым
    деревом категорий как на диске. Во всех случаях ingest должен увидеть
    привычную ему раскладку.
    """
    root = staged.parent / "root"
    root.mkdir(parents=True, exist_ok=True)

    # Папки, где лежит хотя бы одна модель — это и есть «папки моделей»
    # вместе со своими текстурами. Вложенные архивы каталогов дают их десятками.
    model_dirs: list[Path] = []
    for path in staged.rglob("*"):
        if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS:
            if path.parent not in model_dirs:
                model_dirs.append(path.parent)

    if not model_dirs:
        return root

    for src_dir in model_dirs:
        # Если архив повторяет структуру каталога (Anime/Модель/...), берём
        # категорию из него — так одной загрузкой раскладывается сразу многое.
        rel = src_dir.relative_to(staged) if src_dir != staged else Path()
        category = default_category
        if len(rel.parts) >= 2:
            from_tree = canonical_category(rel.parts[0])
            if from_tree:
                category = from_tree

        target_parent = root / category
        target_parent.mkdir(parents=True, exist_ok=True)
        name = src_dir.name if src_dir != staged else _guess_name(src_dir)
        dest = target_parent / name
        suffix = 2
        while dest.exists():
            dest = target_parent / f"{name} ({suffix})"
            suffix += 1
        shutil.move(str(src_dir), str(dest))
    return root


def _guess_name(folder: Path) -> str:
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.suffix.lower() in MODEL_EXTENSIONS:
            return f.stem
    return folder.name or "model"


def _describe_skipped(root: Path) -> list[str]:
    """Почему часть моделей не попала в каталог — самая частая причина одна."""
    notes: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in MODEL_EXTENSIONS:
            continue
        stem = path.stem
        has_preview = any(
            (path.parent / f"{stem}{ext}").is_file() for ext in INGEST_IMAGE_EXTENSIONS
        )
        if not has_preview:
            notes.append(f"{path.name}: нет превью с тем же именем ({stem}.png/.jpg)")
    return notes[:20]


@router.post("/upload")
async def upload_models(
    files: list[UploadFile] = File(..., description="Файлы модели или zip-архив"),
    paths: list[str] = Form(default=[], description="Относительные пути файлов"),
    category: str = Form(default=""),
) -> dict:
    """Принимает папку модели или архив, заливает в S3 и регистрирует в каталоге."""
    if not files:
        raise HTTPException(status_code=400, detail="Нет файлов")

    canon_category = canonical_category(category) or FALLBACK_CATEGORY
    tmp_root = Path(tempfile.mkdtemp(prefix="mf-upload-"))
    staged = tmp_root / "staged"
    staged.mkdir(parents=True, exist_ok=True)

    try:
        total_bytes = 0
        for index, upload in enumerate(files):
            raw_name = upload.filename or f"file{index}"
            # paths приходит параллельным массивом: у загрузки папки браузер
            # отдаёт только имя файла, структуру приходится присылать отдельно.
            rel = _safe_relative(paths[index]) if index < len(paths) else None
            if rel is None:
                rel = _safe_relative(raw_name) or PurePosixPath(f"file{index}")

            if rel.suffix.lower() in _ARCHIVE_SUFFIXES:
                archive_path = tmp_root / f"archive{index}.zip"
                total_bytes += await _save_upload(upload, archive_path)
                extracted = _extract_archive(archive_path, staged)
                archive_path.unlink(missing_ok=True)
                if extracted == 0:
                    raise HTTPException(status_code=400, detail="Архив пуст или повреждён")
            else:
                total_bytes += await _save_upload(upload, staged / rel)

        root = _normalize_layout(staged, canon_category)
        candidates = iter_candidates(root)
        skipped = _describe_skipped(root)

        if not candidates:
            return {
                "ok": False,
                "uploaded": 0,
                "error": "Не найдено ни одной модели с одноимённым превью",
                "skipped": skipped,
            }

        stats = run_upload(
            root,
            limit=None,
            workers=4,
            dry_run=False,
            force=False,
            # Исходник лежит во временной папке и сейчас исчезнет — как «оригинал
            # на диске» его записывать нельзя.
            record_local_dir=False,
        )
        return {
            "ok": stats.get("failed", 0) == 0,
            "uploaded": stats.get("uploaded", 0),
            "duplicates": stats.get("skipped_duplicate", 0),
            "already_known": stats.get("skipped_existing", 0),
            "failed": stats.get("failed", 0),
            "bytes": stats.get("bytes", 0),
            "received_bytes": total_bytes,
            "category": canon_category,
            "errors": stats.get("errors", []),
            "skipped": skipped,
        }
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


@router.get("/stats")
def admin_stats() -> dict:
    """Сводка для админки: объём каталога и полнота обогащения."""
    rows, total = db.search_assets(limit=1)
    facets = db.facet_counts()
    with db.write_transaction() as conn:
        q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        classified = q("SELECT COUNT(*) FROM assets WHERE classified_at IS NOT NULL")
        described = q(
            "SELECT COUNT(*) FROM assets WHERE description IS NOT NULL AND description != ''"
        )
        embedded = q("SELECT COUNT(*) FROM assets WHERE embedded_at IS NOT NULL")
        size = q("SELECT COALESCE(SUM(size), 0) FROM assets")
    return {
        "total": total,
        "classified": classified,
        "described": described,
        "embedded": embedded,
        "pending_classification": total - classified,
        "total_bytes": int(size),
        "categories": facets["categories"],
    }


@router.delete("/model")
def delete_model(path: str) -> dict:
    """Убирает модель из каталога. Файлы в хранилище не трогаем — только запись."""
    with db.write_transaction() as conn:
        if not db.get_row(conn, path):
            raise HTTPException(status_code=404, detail="Модель не найдена")
        preview = db.delete_asset(conn, path)
    db.unlink_preview_file(preview)
    try:
        from app import vector_store

        vector_store.delete_model(path)
    except Exception as e:  # noqa: BLE001 — Qdrant не должен ронять удаление
        log.warning("Не удалось убрать из Qdrant %s: %s", path, e)
    return {"ok": True, "path": path}


class ApiKeyCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=list, description="catalog, download, search")
    rate_limit_per_min: int = Field(120, ge=0, le=10000)
    customer_id: int | None = Field(None, description="Клиент биллинга; лимиты берутся из тарифа")


def _public_api_key(row: dict, *, secret: str | None = None) -> dict:
    out = {
        "id": row["id"],
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "scopes": row["scopes"],
        "rate_limit_per_min": row["rate_limit_per_min"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "request_count": row["request_count"],
        "revoked": bool(row.get("revoked_at")),
        "revoked_at": row.get("revoked_at"),
        "customer_id": row.get("customer_id"),
        "customer_name": row.get("customer_name"),
    }
    if secret:
        out["key"] = secret
        out["warning"] = "Скопируйте ключ сейчас — повторно он не показывается"
    return out


@router.get("/api-keys")
def list_api_keys() -> dict:
    """Ключи сервисного API /v1. Полное значение ключа здесь не возвращается."""
    customers = {c["id"]: c["name"] for c in db.list_billing_customers()}
    keys = []
    for row in db.list_api_keys():
        item = _public_api_key(row)
        cid = row.get("customer_id")
        item["customer_name"] = customers.get(cid) if cid else None
        keys.append(item)
    return {
        "keys": keys,
        "scopes": list(api_keys.ALL_SCOPES),
        "customers": [{"id": c["id"], "name": c["name"]} for c in db.list_billing_customers()],
    }


@router.post("/api-keys")
def create_api_key(body: ApiKeyCreateBody) -> dict:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Укажите название ключа")
    customer_id = body.customer_id
    rate_limit = body.rate_limit_per_min
    scopes = api_keys.normalize_scopes(body.scopes)
    if customer_id:
        customer = db.get_billing_customer(customer_id)
        if not customer:
            raise HTTPException(status_code=404, detail="Клиент не найден")
        ctx = db.resolve_billing(customer_id, time.time())
        plan = ctx.get("plan")
        if not plan or ctx.get("status") in (None, "none", "canceled"):
            raise HTTPException(
                status_code=400,
                detail="У клиента нет активной подписки — сначала назначьте тариф",
            )
        active_keys = db.count_active_api_keys(customer_id)
        if plan["max_api_keys"] and active_keys >= plan["max_api_keys"]:
            raise HTTPException(
                status_code=400,
                detail=f"Лимит ключей тарифа исчерпан ({plan['max_api_keys']})",
            )
        rate_limit = int(plan["rate_limit_per_min"] or rate_limit)
        allowed = set(plan.get("scopes") or [])
        scopes = [s for s in scopes if s in allowed] or list(allowed)
    secret = api_keys.generate_key()
    row = db.create_api_key(
        name=name[:120],
        key_prefix=api_keys.display_prefix(secret),
        key_hash=api_keys.hash_key(secret),
        scopes=scopes,
        rate_limit_per_min=rate_limit,
        now=time.time(),
        customer_id=customer_id,
    )
    return _public_api_key(row, secret=secret)


@router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: int) -> dict:
    if not db.revoke_api_key(key_id, time.time()):
        raise HTTPException(status_code=404, detail="Ключ не найден или уже отозван")
    return {"ok": True, "id": key_id, "revoked": True}


class ApiKeyUpdateBody(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    scopes: list[str] | None = None
    rate_limit_per_min: int | None = Field(None, ge=0, le=10000)


@router.patch("/api-keys/{key_id}")
def update_api_key(key_id: int, body: ApiKeyUpdateBody) -> dict:
    scopes = api_keys.normalize_scopes(body.scopes) if body.scopes is not None else None
    row = db.update_api_key(
        key_id,
        name=(body.name or "").strip() or None,
        scopes=scopes,
        rate_limit_per_min=body.rate_limit_per_min,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Ключ не найден")
    return _public_api_key(row)


class PlanBody(BaseModel):
    id: int | None = None
    slug: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=120)
    description: str = ""
    price_rub: float = Field(0, ge=0, description="Цена за период в рублях")
    currency: str = "RUB"
    period: str = Field("month", description="month | year | once")
    requests_per_period: int = Field(0, ge=0, description="0 = без лимита")
    downloads_per_period: int = Field(0, ge=0)
    bytes_per_period: int = Field(0, ge=0)
    searches_per_period: int = Field(0, ge=0)
    rate_limit_per_min: int = Field(60, ge=0, le=10000)
    max_api_keys: int = Field(1, ge=0, le=1000)
    max_page_size: int = Field(24, ge=1, le=200)
    scopes: list[str] = Field(default_factory=list)
    trial_days: int = Field(0, ge=0, le=365)
    is_active: bool = True
    sort_order: int = 0


class CustomerBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    email: str | None = None
    notes: str | None = None


class SubscribeBody(BaseModel):
    plan_id: int
    auto_renew: bool = True
    mark_paid: bool = True
    use_trial: bool = True


def _plan_payload(body: PlanBody) -> dict:
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in body.slug.strip().lower())
    if not slug:
        raise HTTPException(status_code=400, detail="Некорректный slug тарифа")
    if body.period not in billing.PERIODS:
        raise HTTPException(status_code=400, detail="period: month, year или once")
    return {
        "id": body.id,
        "slug": slug[:64],
        "name": body.name.strip()[:120],
        "description": (body.description or "").strip(),
        "price_cents": int(round(body.price_rub * 100)),
        "currency": (body.currency or "RUB").upper()[:8],
        "period": body.period,
        "requests_per_period": body.requests_per_period,
        "downloads_per_period": body.downloads_per_period,
        "bytes_per_period": body.bytes_per_period,
        "searches_per_period": body.searches_per_period,
        "rate_limit_per_min": body.rate_limit_per_min,
        "max_api_keys": body.max_api_keys,
        "max_page_size": body.max_page_size,
        "scopes": api_keys.normalize_scopes(body.scopes),
        "trial_days": body.trial_days,
        "is_active": body.is_active,
        "sort_order": body.sort_order,
    }


def _plan_public(plan: dict) -> dict:
    out = dict(plan)
    out["price_rub"] = (plan.get("price_cents") or 0) / 100
    out["price_label"] = billing.format_money(plan.get("price_cents") or 0, plan.get("currency") or "RUB")
    return out


@router.get("/billing")
def billing_dashboard() -> dict:
    return {
        "overview": db.billing_overview(),
        "plans": [_plan_public(p) for p in db.list_billing_plans()],
        "customers": db.list_billing_customers(),
        "invoices": db.list_billing_invoices(80),
        "scopes": list(api_keys.ALL_SCOPES),
        "periods": list(billing.PERIODS),
        "yookassa": {"configured": yookassa.configured()},
    }


@router.post("/billing/plans")
def save_plan(body: PlanBody) -> dict:
    plan = db.upsert_billing_plan(_plan_payload(body), time.time())
    return _plan_public(plan)


@router.post("/billing/customers")
def create_customer(body: CustomerBody) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Укажите имя клиента")
    return db.create_billing_customer(
        name[:160],
        (body.email or "").strip() or None,
        (body.notes or "").strip() or None,
        time.time(),
    )


@router.post("/billing/customers/{customer_id}/subscribe")
def subscribe_customer(customer_id: int, body: SubscribeBody) -> dict:
    if not db.get_billing_customer(customer_id):
        raise HTTPException(status_code=404, detail="Клиент не найден")
    try:
        sub = db.start_subscription(
            customer_id=customer_id,
            plan_id=body.plan_id,
            now=time.time(),
            auto_renew=body.auto_renew,
            mark_paid=body.mark_paid,
            use_trial=body.use_trial,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return sub


@router.post("/billing/subscriptions/{sub_id}/cancel")
def cancel_sub(sub_id: int) -> dict:
    if not db.cancel_subscription(sub_id, time.time()):
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    return {"ok": True, "id": sub_id, "status": "canceled"}


@router.post("/billing/invoices/{invoice_id}/pay")
def pay_invoice(invoice_id: int) -> dict:
    result = db.pay_invoice(invoice_id, time.time())
    if not result:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return result


@router.post("/billing/invoices/{invoice_id}/yookassa")
def create_yookassa_payment(invoice_id: int, request: Request) -> dict:
    if not yookassa.configured():
        raise HTTPException(
            status_code=400,
            detail="ЮKassa не настроена. Задайте YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY.",
        )
    invoice = db.get_billing_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    try:
        return yookassa.create_payment_for_invoice(
            invoice, return_base=yookassa.request_base(request)
        )
    except yookassa.YooKassaError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/billing/invoices/{invoice_id}/void")
def void_invoice(invoice_id: int) -> dict:
    invoice = db.get_billing_invoice(invoice_id)
    if invoice and invoice.get("yookassa_payment_id") and invoice.get("status") == "issued":
        try:
            yookassa.cancel_payment(str(invoice["yookassa_payment_id"]))
        except yookassa.YooKassaError as e:
            log.warning("Не удалось отменить платёж ЮKassa %s: %s", invoice["yookassa_payment_id"], e)
    result = db.void_invoice(invoice_id)
    if not result:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    if result.get("error") == "already_paid":
        raise HTTPException(status_code=400, detail="Оплаченный счёт нельзя аннулировать")
    return result


@router.patch("/billing/customers/{customer_id}")
def update_customer(customer_id: int, body: CustomerBody) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Укажите имя клиента")
    row = db.update_billing_customer(
        customer_id,
        name[:160],
        (body.email or "").strip() or None,
        (body.notes or "").strip() or None,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    return row


@router.delete("/billing/customers/{customer_id}")
def delete_customer(customer_id: int) -> dict:
    if not db.delete_billing_customer(customer_id, time.time()):
        raise HTTPException(status_code=404, detail="Клиент не найден")
    return {"ok": True, "id": customer_id}


@router.delete("/billing/plans/{plan_id}")
def delete_plan(plan_id: int) -> dict:
    reason = db.delete_billing_plan(plan_id)
    if reason == "not_found":
        raise HTTPException(status_code=404, detail="Тариф не найден")
    if reason == "in_use":
        raise HTTPException(
            status_code=400,
            detail="Тариф назначен активным подпискам — сначала смените или отмените их",
        )
    return {"ok": True, "id": plan_id}


@router.get("/metrics")
def admin_metrics(days: int = 14) -> dict:
    """Сводка API и каталога для дашборда админки."""
    usage = db.usage_metrics(days)
    catalog = admin_stats()
    overview = db.billing_overview()
    meta = db.get_catalog_meta()
    return {
        **usage,
        "catalog": catalog,
        "billing": overview,
        "preview_by_status": meta.get("preview_by_status") or {},
        "pending_previews": meta.get("pending_previews") or 0,
    }


@router.get("/models")
def admin_models(
    q: str | None = None,
    category: str | None = None,
    limit: int = 40,
    offset: int = 0,
) -> dict:
    """Постраничный список моделей для управления каталогом."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    cats = [category] if category and category.strip() else None
    rows, total = db.search_assets(
        limit=limit,
        offset=offset,
        sort="newest",
        name_contains=(q or "").strip() or None,
        categories=cats,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "path": r["path"],
                "name": r["name"],
                "ext": r.get("ext"),
                "size": int(r.get("size") or 0),
                "category": r.get("category"),
                "preview_status": r.get("preview_status"),
                "updated_at": r.get("updated_at"),
            }
            for r in rows
        ],
    }


