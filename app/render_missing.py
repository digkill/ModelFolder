"""Рендер превью для моделей, у которых картинки рядом нет.

Без превью модель не попадает в каталог: по ней нечего показать в галерее и
нечего дать Vision для разметки. Такие модели составляют бо́льшую часть диска,
поэтому картинку надо получить из самой геометрии.

Рендерим в браузере (Chromium + model-viewer/three.js), а не через pyrender:
на Windows связка PyOpenGL + Python 3.12 падает на уровне ctypes, а Chromium
работает и заодно честно применяет материалы и PBR-освещение glTF.

Ключевая оптимизация — один браузер на весь прогон. Штатный
`try_browser_preview` поднимает Chromium заново на каждую модель, и это почти
всё время работы: ~8 с на модель против ~1–2 с при переиспользовании.

    python -m app.render_missing --root F:/catalog --limit 500
    python -m app.render_missing --root F:/catalog --shard 0 --shards 4
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.browser_preview import (
    ASSET_ORIGIN,
    _asset_url,
    _html_modelviewer,
    _html_three_fbx,
    _mime,
)
from app.ingest import Progress, _pick_preview
from app.paths import (
    INGEST_IMAGE_EXTENSIONS,
    MODEL_EXTENSIONS,
    PREVIEW_BROWSER_TIMEOUT_MS,
    PREVIEW_PIXEL_SIZE,
)

log = logging.getLogger("render")

# Форматы, которые умеет открыть браузерный просмотрщик.
RENDERABLE = {".glb", ".gltf", ".fbx"}


def find_models_without_preview(root: Path) -> list[Path]:
    """Модели, для которых ingest не нашёл бы превью ни строгим, ни мягким правилом."""
    out: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        models = [f for f in filenames if Path(f).suffix.lower() in RENDERABLE]
        if not models:
            continue
        dir_path = Path(dirpath)
        images = {
            Path(f).stem: dir_path / f
            for f in filenames
            if Path(f).suffix.lower() in INGEST_IMAGE_EXTENSIONS
        }
        model_count = len([f for f in filenames if Path(f).suffix.lower() in MODEL_EXTENSIONS])
        for name in sorted(models):
            stem = Path(name).stem
            if stem in images:
                continue
            if _pick_preview(stem, images, model_count) is not None:
                continue
            out.append(dir_path / name)
    out.sort(key=lambda p: str(p).lower())
    return out


class BrowserRenderer:
    """Держит один Chromium на весь прогон и снимает по кадру на модель."""

    def __init__(self, size: int | None = None, timeout_ms: int | None = None):
        self.size = size or PREVIEW_PIXEL_SIZE
        # Штатные 4 минуты рассчитаны на единичный рендер по запросу. На массовом
        # прогоне это ловушка: десяток «неподъёмных» моделей съедает часы, пока
        # тысячи обычных ждут очереди. Лучше быстро сдаться и идти дальше.
        self.timeout_ms = timeout_ms or PREVIEW_BROWSER_TIMEOUT_MS
        self._pw = None
        self._browser = None
        self._context = None

    def __enter__(self) -> "BrowserRenderer":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._launch()
        return self

    def _launch(self) -> None:
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-zygote",
                "--disable-setuid-sandbox",
                "--disable-extensions",
                "--disable-background-networking",
            ],
        )
        self._context = self._browser.new_context(
            viewport={"width": self.size, "height": self.size},
            device_scale_factor=1,
        )

    def _restart(self) -> None:
        """Chromium иногда умирает на тяжёлой геометрии — поднимаем заново.

        Без этого одна проблемная модель обрывала бы весь прогон на тысячах
        оставшихся: контекст закрыт, и все последующие вызовы падают.
        """
        log.warning("Chromium перезапускается после сбоя")
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
        self._launch()

    def __exit__(self, *_exc) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001 — закрытие не должно ронять прогон
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass

    def render(self, model_path: Path, out_path: Path) -> tuple[bool, str | None]:
        ok, err = self._render_once(model_path, out_path)
        if ok:
            return True, None
        # Признак умершего браузера, а не плохой модели: пробуем ещё раз на свежем.
        if err and ("has been closed" in err or "Target closed" in err):
            self._restart()
            return self._render_once(model_path, out_path)
        return False, err

    @staticmethod
    def _make_router(model_path: Path, asset_url: str, mime: str):
        """Отдаёт браузеру файлы модели, разрешая пути относительно её папки.

        Наивный вариант «на любой запрос вернуть сам файл модели» ломается на
        .gltf: тот тянет внешние .bin и текстуры, получает вместо них копию
        себя, и разбор уходит вразнос — процесс раздувается на гигабайты.
        Поэтому соседние файлы ищем на диске, а чужие запросы обрываем.
        """
        base = model_path.parent.resolve()

        def handler(route):
            url = route.request.url
            if not url.startswith(ASSET_ORIGIN):
                # model-viewer подгружается с CDN — его пускаем, остальное режем.
                route.continue_()
                return
            if url.split("?", 1)[0] == asset_url:
                route.fulfill(path=str(model_path), content_type=mime)
                return
            rel = unquote(urlparse(url).path).lstrip("/")
            candidate = (base / rel).resolve()
            try:
                candidate.relative_to(base)  # не выпускаем за пределы папки модели
            except ValueError:
                route.abort()
                return
            if candidate.is_file():
                route.fulfill(path=str(candidate))
            else:
                route.abort()

        return handler

    def _render_once(self, model_path: Path, out_path: Path) -> tuple[bool, str | None]:
        ext = model_path.suffix.lower()
        if ext not in RENDERABLE:
            return False, f"формат {ext} браузером не открывается"
        if not model_path.is_file():
            return False, "файл модели отсутствует"

        mime = _mime(ext)
        asset_url = _asset_url(ext)
        if ext == ".fbx":
            html, selector = _html_three_fbx(asset_url, self.size, self.size), "#c"
        else:
            html, selector = _html_modelviewer(asset_url, self.size, self.size), "#mv"

        try:
            page = self._context.new_page()
        except Exception as e:  # noqa: BLE001 — браузер мог умереть между моделями
            return False, str(e)[:300]
        try:
            # Модель отдаём подменой ответа, а не файловым URL: так работает и
            # для путей с пробелами и кириллицей, и без локального веб-сервера.
            page.route("**/*", self._make_router(model_path, asset_url, mime))
            page.set_content(
                html,
                wait_until="domcontentloaded",
                timeout=min(60_000, self.timeout_ms),
            )
            page.wait_for_function(
                "() => window.__PREVIEW_READY__ === true "
                "|| typeof window.__PREVIEW_ERR__ === 'string'",
                timeout=self.timeout_ms,
            )
            err = page.evaluate("() => window.__PREVIEW_ERR__")
            if err:
                return False, str(err)[:300]
            out_path.parent.mkdir(parents=True, exist_ok=True)
            page.locator(selector).screenshot(path=str(out_path), type="png")
            return True, None
        except Exception as e:  # noqa: BLE001 — одна битая модель не должна ронять прогон
            if out_path.exists():
                try:
                    out_path.unlink()
                except OSError:
                    pass
            return False, str(e)[:300]
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001 — страница могла закрыться вместе с браузером
                pass


def run(
    root: Path,
    *,
    limit: int | None,
    shard: int,
    shards: int,
    overwrite: bool,
    timeout_sec: int = 45,
    max_mb: int = 64,
) -> dict:
    models = find_models_without_preview(root)
    total_found = len(models)

    # Замер показал жёсткую зависимость от размера: до ~5 МБ модель рисуется за
    # 1.6 с, а на 80–180 МБ headless Chromium стабильно падает, потратив перед
    # этим 30–130 с. Отсекаем такие сразу, иначе десяток гигантов съедает часы.
    too_big = 0
    if max_mb:
        limit_bytes = max_mb * 1024 * 1024
        kept: list[Path] = []
        for m in models:
            try:
                if m.stat().st_size > limit_bytes:
                    too_big += 1
                    continue
            except OSError:
                continue
            kept.append(m)
        models = kept
    if shards > 1:
        models = [m for i, m in enumerate(models) if i % shards == shard]
    if limit is not None:
        models = models[:limit]

    log.info(
        "Без превью: %d; крупнее %d МБ (пропуск): %d; в этом прогоне: %d%s",
        total_found,
        max_mb,
        too_big,
        len(models),
        f" (шард {shard + 1}/{shards})" if shards > 1 else "",
    )

    stats = {
        "total": len(models),
        "rendered": 0,
        "skipped": 0,
        "failed": 0,
        "too_big": too_big,
    }
    errors: list[str] = []
    progress = Progress(len(models), "Рендер превью", every=10)

    if not models:
        return stats

    with BrowserRenderer(timeout_ms=timeout_sec * 1000) as renderer:
        for model in models:
            # Кладём картинку рядом с моделью и с тем же именем: дальше её
            # подхватит обычный ingest, никакого особого режима не нужно.
            out = model.with_suffix(".png")
            if out.exists() and not overwrite:
                stats["skipped"] += 1
                progress.step()
                continue
            ok, err = renderer.render(model, out)
            if ok:
                stats["rendered"] += 1
            else:
                stats["failed"] += 1
                if len(errors) < 30:
                    errors.append(f"{model.name}: {err}")
            progress.step(size=out.stat().st_size if out.exists() else 0)

    stats["errors"] = errors
    return stats


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    p = argparse.ArgumentParser(
        prog="python -m app.render_missing",
        description="Рендер превью для моделей без картинки",
    )
    p.add_argument("--root", type=Path, required=True, help="корень каталога моделей")
    p.add_argument("--limit", type=int, default=None, help="максимум моделей за прогон")
    p.add_argument("--shards", type=int, default=1, help="на сколько частей делить работу")
    p.add_argument("--shard", type=int, default=0, help="номер части, от 0 до shards-1")
    p.add_argument("--overwrite", action="store_true", help="перерисовать уже готовые")
    p.add_argument(
        "--timeout-sec",
        type=int,
        default=45,
        help="сколько ждать загрузку одной модели, прежде чем сдаться",
    )
    p.add_argument(
        "--max-mb",
        type=int,
        default=64,
        help="пропускать модели крупнее указанного размера (0 = без ограничения)",
    )
    args = p.parse_args(argv)

    if not args.root.is_dir():
        log.error("Каталог не найден: %s", args.root)
        return 2
    if not (0 <= args.shard < args.shards):
        log.error("--shard должен быть в диапазоне 0..%d", args.shards - 1)
        return 2

    stats = run(
        args.root.resolve(),
        limit=args.limit,
        shard=args.shard,
        shards=args.shards,
        overwrite=args.overwrite,
        timeout_sec=args.timeout_sec,
        max_mb=args.max_mb,
    )
    import json

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if not stats.get("failed") else 1


if __name__ == "__main__":
    sys.exit(main())
