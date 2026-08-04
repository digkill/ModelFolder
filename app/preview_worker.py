"""Child process: python -m app.preview_worker <model_abs_path> <png_out_path>"""

import gc
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: preview_worker MODEL_PATH OUT_PNG", file=sys.stderr)
        return 2

    # До тяжёлых импортов и до Chromium — лимит памяти на процесс (Unix).
    from app.memory_limit import apply_preview_process_memory_limit

    apply_preview_process_memory_limit()

    from app.paths import PREVIEW_ENGINE
    from app.preview_render import try_render_preview

    model = Path(sys.argv[1])
    out = Path(sys.argv[2])
    engine = PREVIEW_ENGINE

    ok = False
    err: str | None = None

    if engine == "pyrender":
        ok, err = try_render_preview(model, out)
    elif engine == "browser":
        from app.browser_preview import try_browser_preview

        ok, err = try_browser_preview(model, out)
    else:
        # auto: сначала скриншот из Chromium (WebGL), при неудаче — pyrender
        from app.browser_preview import try_browser_preview

        ok, err = try_browser_preview(model, out)
        if not ok:
            gc.collect()
            berr = err
            ok, err = try_render_preview(model, out)
            if not ok and berr:
                print(f"browser: {berr}", file=sys.stderr)

    if err and not ok:
        print(err, file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
