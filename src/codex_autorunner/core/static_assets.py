from __future__ import annotations

from contextlib import ExitStack
from importlib import resources
from pathlib import Path
from typing import Optional

# Keep the required asset list close to the core boundary so core modules do not
# import from codex_autorunner.web.*
_REQUIRED_STATIC_ASSETS = (
    "index.html",
    "styles.css",
    "bootstrap.js",
    "loader.js",
    "app.js",
    "vendor/xterm.js",
    "vendor/xterm-addon-fit.js",
    "vendor/xterm.css",
)


def missing_static_assets(static_dir: Path) -> list[str]:
    missing: list[str] = []
    for rel_path in _REQUIRED_STATIC_ASSETS:
        try:
            if not (static_dir / rel_path).exists():
                missing.append(rel_path)
        except OSError:
            missing.append(rel_path)
    return missing


def resolve_static_dir() -> tuple[Path, Optional[ExitStack]]:
    """Locate packaged static assets without importing codex_autorunner.web."""

    static_root = resources.files("codex_autorunner").joinpath("static")
    if isinstance(static_root, Path):
        if static_root.exists():
            return static_root, None
        fallback = Path(__file__).resolve().parent.parent / "static"
        return fallback, None

    stack = ExitStack()
    try:
        static_path = stack.enter_context(resources.as_file(static_root))
    except Exception:
        stack.close()
        fallback = Path(__file__).resolve().parent.parent / "static"
        return fallback, None
    if static_path.exists():
        return static_path, stack

    stack.close()
    fallback = Path(__file__).resolve().parent.parent / "static"
    return fallback, None
