from __future__ import annotations

from pathlib import Path
from typing import Optional

_ASSET_VERSION_TOKEN = "__CAR_ASSET_VERSION__"
_REQUIRED_STATIC_ASSETS = (
    "index.html",
    "styles.css",
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


def asset_version(static_dir: Path) -> str:
    candidates = [
        static_dir / "index.html",
        static_dir / "styles.css",
        static_dir / "app.js",
    ]
    try:
        candidates.extend(static_dir.rglob("*.js"))
    except Exception:
        pass
    mtimes = []
    for path in candidates:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        mtimes.append(stat.st_mtime_ns)
    if not mtimes:
        return "0"
    return str(max(mtimes))


def render_index_html(static_dir: Path, version: Optional[str]) -> str:
    index_path = static_dir / "index.html"
    text = index_path.read_text(encoding="utf-8")
    if version:
        text = text.replace(_ASSET_VERSION_TOKEN, version)
    return text


def index_response_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
