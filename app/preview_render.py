"""Offscreen thumbnail via trimesh + pyrender (formats trimesh can load)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import numpy as np

from app.paths import (
    PREVIEW_PIXEL_SIZE,
    PREVIEW_SIMPLIFY_MAX_FACES,
    PREVIEW_SIMPLIFY_TARGET_FACES,
)

log = logging.getLogger(__name__)


def preview_basename_for(path: str, mtime: float) -> str:
    h = hashlib.sha256(f"{path}\0{mtime}".encode()).hexdigest()
    return f"{h}.png"


def _look_at(eye: np.ndarray, target: np.ndarray, up: np.ndarray) -> np.ndarray:
    eye = np.asarray(eye, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    up = np.asarray(up, dtype=np.float64)
    z = eye - target
    n = np.linalg.norm(z)
    if n < 1e-12:
        z = np.array([0.0, 0.0, 1.0])
    else:
        z /= n
    x = np.cross(up, z)
    n = np.linalg.norm(x)
    if n < 1e-12:
        x = np.array([1.0, 0.0, 0.0])
    else:
        x /= n
    y = np.cross(z, x)
    pose = np.eye(4, dtype=np.float64)
    pose[:3, 0] = x
    pose[:3, 1] = y
    pose[:3, 2] = z
    pose[:3, 3] = eye
    return pose


def _scene_to_mesh(scene) -> object | None:
    import trimesh

    if not isinstance(scene, trimesh.Scene) or len(scene.geometry) == 0:
        return None
    try:
        m = scene.dump(concatenate=True)
        if m is not None and len(getattr(m, "vertices", [])) > 0:
            return m
    except Exception:
        pass
    best = None
    best_n = 0
    for g in scene.geometry.values():
        if isinstance(g, trimesh.Trimesh):
            n = len(g.vertices)
            if n > best_n:
                best, best_n = g, n
    return best


def _coerce_mesh(loaded) -> object | None:
    import trimesh

    if isinstance(loaded, trimesh.Trimesh):
        return loaded if len(loaded.vertices) > 0 else None
    if isinstance(loaded, trimesh.Scene):
        return _scene_to_mesh(loaded)
    return None


def load_mesh_for_preview(model_path: Path):
    import trimesh

    last_err: Exception | None = None
    for force in ("scene", "mesh"):
        try:
            loaded = trimesh.load(str(model_path), force=force)
            mesh = _coerce_mesh(loaded)
            if mesh is not None:
                return mesh
        except Exception as e:
            last_err = e
            continue
    try:
        loaded = trimesh.load(str(model_path))
        mesh = _coerce_mesh(loaded)
        if mesh is not None:
            return mesh
    except Exception as e:
        last_err = e
    raise RuntimeError(f"Could not load geometry: {last_err}")


def maybe_simplify_for_preview(mesh) -> object:
    import trimesh

    if not isinstance(mesh, trimesh.Trimesh):
        return mesh
    faces = getattr(mesh, "faces", None)
    if faces is None or len(faces) == 0:
        return mesh
    fc = len(faces)
    if fc <= PREVIEW_SIMPLIFY_MAX_FACES:
        return mesh
    target = min(PREVIEW_SIMPLIFY_TARGET_FACES, PREVIEW_SIMPLIFY_MAX_FACES)
    target = max(target, 4000)
    if target >= fc:
        return mesh
    try:
        simplified = mesh.simplify_quadric_decimation(target)
        if simplified is not None and len(getattr(simplified, "vertices", [])) > 0:
            log.info(
                "Preview simplify %s faces -> %s",
                fc,
                len(simplified.faces),
            )
            return simplified
    except Exception as e:
        log.debug("Quadric simplify skipped: %s", e)
    return mesh


def render_preview(model_path: Path, out_path: Path, size: int | None = None) -> None:
    import pyrender

    if size is None:
        size = PREVIEW_PIXEL_SIZE

    mesh = load_mesh_for_preview(model_path)
    mesh = maybe_simplify_for_preview(mesh)
    if mesh is None or len(mesh.vertices) == 0:
        raise RuntimeError("Empty geometry")

    bounds = mesh.bounds
    center = (bounds[0] + bounds[1]) * 0.5
    diag = np.linalg.norm(bounds[1] - bounds[0])
    if diag < 1e-8:
        diag = 1.0

    yfov = np.pi / 4.0
    dist = (diag * 0.55) / max(np.tan(yfov * 0.5), 0.01)
    eye = center + np.array([dist * 0.85, -dist * 0.55, dist * 0.9], dtype=np.float64)
    cam_pose = _look_at(eye, center, np.array([0.0, 1.0, 0.0]))

    pr_mesh = pyrender.Mesh.from_trimesh(mesh, smooth=False)
    scene = pyrender.Scene(
        ambient_light=np.array([0.42, 0.42, 0.45, 1.0]),
        bg_color=np.array([0.06, 0.07, 0.09, 1.0]),
    )
    scene.add(pr_mesh)

    light_pose = np.eye(4, dtype=np.float64)
    light_pose[:3, 3] = center + np.array([diag * 0.8, diag * 1.1, diag * 0.9])
    dlight = pyrender.DirectionalLight(color=np.ones(3), intensity=5.0)
    scene.add(dlight, pose=light_pose)

    cam = pyrender.PerspectiveCamera(yfov=yfov, aspectRatio=1.0)
    scene.add(cam, pose=cam_pose)

    r = pyrender.OffscreenRenderer(size, size)
    try:
        color, _ = r.render(scene)
    finally:
        r.delete()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image

        Image.fromarray(color).save(out_path, format="PNG")
    except Exception:
        import imageio.v2 as imageio

        imageio.imwrite(out_path, color)


def try_render_preview(model_path: Path, out_path: Path) -> tuple[bool, str | None]:
    try:
        render_preview(model_path, out_path)
        return True, None
    except Exception as e:
        log.warning("Preview failed for %s: %s", model_path, e)
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        return False, str(e)[:500]
