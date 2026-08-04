"""Превью через headless Chromium: скриншот model-viewer (glb/gltf) или Three.js (fbx)."""

from __future__ import annotations

import gc
import logging
import mimetypes
from pathlib import Path

from app.paths import PREVIEW_BROWSER_TIMEOUT_MS, PREVIEW_PIXEL_SIZE

log = logging.getLogger(__name__)

ASSET_ORIGIN = "https://preview.local"


def _asset_url(ext: str) -> str:
    return f"{ASSET_ORIGIN}/model{ext}"


def _mime(ext: str) -> str:
    m = mimetypes.types_map.get(ext)
    if m:
        return m
    if ext == ".glb":
        return "model/gltf-binary"
    if ext == ".gltf":
        return "model/gltf+json"
    return "application/octet-stream"


def _html_modelviewer(asset_url: str, w: int, h: int) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<style>html,body{{margin:0;padding:0;background:#0a0c0f;overflow:hidden}}
model-viewer{{width:{w}px;height:{h}px;display:block}}</style>
<script type="module" src="https://ajax.googleapis.com/ajax/libs/model-viewer/3.5.0/model-viewer.min.js"></script>
</head><body>
<model-viewer id="mv" src="{asset_url}" camera-controls exposure="1" shadow-intensity="1"
  interaction-prompt="none" environment-image="neutral"></model-viewer>
<script>
const mv = document.getElementById('mv');
mv.addEventListener('load', () => {{ window.__PREVIEW_READY__ = true; }});
mv.addEventListener('error', (e) => {{
  try {{
    const d = e && e.detail;
    if (typeof d === 'string') window.__PREVIEW_ERR__ = d;
    else if (d && typeof d === 'object')
      window.__PREVIEW_ERR__ = d.message || d.reason || JSON.stringify(d);
    else window.__PREVIEW_ERR__ = 'model-viewer error';
  }} catch (_) {{
    window.__PREVIEW_ERR__ = 'model-viewer error';
  }}
}});
</script>
</body></html>"""


def _html_three_fbx(asset_url: str, w: int, h: int) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<style>html,body{{margin:0;padding:0;background:#0a0c0f;overflow:hidden}}</style>
<script type="importmap">{{"imports":{{"three":"https://unpkg.com/three@0.170.0/build/three.module.js","three/addons/":"https://unpkg.com/three@0.170.0/examples/jsm/"}}}}</script>
</head><body>
<canvas id="c" width="{w}" height="{h}"></canvas>
<script type="module">
import * as THREE from 'three';
import {{ FBXLoader }} from 'three/addons/loaders/FBXLoader.js';
const W={w}, H={h};
const canvas = document.getElementById('c');
const renderer = new THREE.WebGLRenderer({{canvas, antialias:true, alpha:false, preserveDrawingBuffer:true}});
renderer.setSize(W, H, false);
renderer.setPixelRatio(1);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0c0f);
const camera = new THREE.PerspectiveCamera(45, W/H, 0.1, 5000);
scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 1));
const dl = new THREE.DirectionalLight(0xffffff, 1.2);
dl.position.set(3, 8, 5);
scene.add(dl);
const loader = new FBXLoader();
loader.load(
  '{asset_url}',
  (obj) => {{
    scene.add(obj);
    const box = new THREE.Box3().setFromObject(obj);
    const c = box.getCenter(new THREE.Vector3());
    const s = box.getSize(new THREE.Vector3());
    const d = Math.max(s.x, s.y, s.z, 1);
    const dist = d * 2.2;
    camera.position.set(c.x + dist * 0.65, c.y + d * 0.35, c.z + dist * 0.65);
    camera.lookAt(c);
    renderer.render(scene, camera);
    window.__PREVIEW_READY__ = true;
  }},
  undefined,
  (err) => {{ window.__PREVIEW_ERR__ = String(err && err.message ? err.message : err); }}
);
</script>
</body></html>"""


def try_browser_preview(model_path: Path, out_path: Path) -> tuple[bool, str | None]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "playwright not installed"

    ext = model_path.suffix.lower()
    if ext not in (".glb", ".gltf", ".fbx"):
        return False, f"browser preview unsupported ext {ext}"

    if not model_path.is_file():
        return False, "model file missing"

    w = h = PREVIEW_PIXEL_SIZE
    asset_url = _asset_url(ext)
    mime = _mime(ext)
    if ext in (".glb", ".gltf"):
        html = _html_modelviewer(asset_url, w, h)
        shot_selector = "#mv"
    else:
        html = _html_three_fbx(asset_url, w, h)
        shot_selector = "#c"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    def route_handler(route):
        u = route.request.url
        if u.startswith(ASSET_ORIGIN):
            route.fulfill(path=str(model_path), content_type=mime)
        else:
            route.continue_()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-zygote",
                    "--disable-setuid-sandbox",
                    # Меньше процессов рендера — ниже суммарный RSS (грубо).
                    "--renderer-process-limit=1",
                    "--disable-extensions",
                    "--disable-background-networking",
                ],
            )
            context = browser.new_context(
                viewport={"width": w, "height": h},
                device_scale_factor=1,
            )
            page = context.new_page()
            page.route("**/*", route_handler)
            page.set_content(html, wait_until="domcontentloaded", timeout=min(120_000, PREVIEW_BROWSER_TIMEOUT_MS))
            page.wait_for_function(
                "() => window.__PREVIEW_READY__ === true || typeof window.__PREVIEW_ERR__ === 'string'",
                timeout=PREVIEW_BROWSER_TIMEOUT_MS,
            )
            err = page.evaluate("() => window.__PREVIEW_ERR__")
            if err:
                return False, str(err)[:500]
            page.locator(shot_selector).screenshot(path=str(out_path), type="png")
            context.close()
            browser.close()
        gc.collect()
        return True, None
    except Exception as e:
        log.warning("Browser preview failed: %s", e)
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        return False, str(e)[:500]
