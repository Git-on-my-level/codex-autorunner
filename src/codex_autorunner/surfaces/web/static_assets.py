from __future__ import annotations

import base64
import hashlib
import json
from contextlib import ExitStack
from importlib import resources
from pathlib import Path
from typing import Iterable, Optional


def resolve_web_static_dir() -> tuple[Path, Optional[ExitStack]]:
    """Locate the packaged Web Hub SvelteKit static assets when built."""

    _pkg_root = Path(__file__).resolve().parents[2]

    static_root = resources.files("codex_autorunner").joinpath("web_static")
    if isinstance(static_root, Path):
        fallback = _pkg_root / "web_static"
        if static_root.exists():
            return static_root, None
        return fallback, None

    stack = ExitStack()
    try:
        static_path = stack.enter_context(resources.as_file(static_root))
    except (
        Exception
    ):  # intentional: importlib.resources can raise varied errors across Python versions
        stack.close()
        fallback = _pkg_root / "web_static"
        return fallback, None
    if static_path.exists():
        return static_path, stack

    stack.close()
    fallback = _pkg_root / "web_static"
    return fallback, None


def _iter_asset_files(static_dir: Path) -> Iterable[Path]:
    try:
        for path in static_dir.rglob("*"):
            try:
                if path.is_dir():
                    continue
                yield path
            except OSError:
                continue
    except OSError:
        return


def _hash_file(path: Path, digest: "hashlib._Hash") -> None:
    try:
        if path.is_symlink():
            digest.update(b"SYMLINK:")
            try:
                target = path.readlink()
            except OSError:
                target = Path("dangling")
            digest.update(str(target).encode("utf-8", errors="replace"))
            return
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        digest.update(b"UNREADABLE")


def asset_version(static_dir: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(_iter_asset_files(static_dir), key=lambda p: p.as_posix())
    if not files:
        return "0"
    for path in files:
        try:
            rel_path = path.relative_to(static_dir)
        except ValueError:
            rel_path = path
        digest.update(rel_path.as_posix().encode("utf-8", errors="replace"))
        _hash_file(path, digest)
    return digest.hexdigest()


def render_web_index_html(static_dir: Path, base_path: str = "") -> str:
    index_path = static_dir / "index.html"
    html = index_path.read_text(encoding="utf-8")
    if base_path:
        normalized_base_path = base_path.rstrip("/")
        base_json = json.dumps(normalized_base_path)
        html = html.replace('"/_app/', f'"{normalized_base_path}/_app/')
        html = html.replace("'/_app/", f"'{normalized_base_path}/_app/")
        html = html.replace(
            "__sveltekit_",
            f"globalThis.__CAR_BASE_PATH__ = {base_json};\n\n\t\t\t\t\t__sveltekit_",
            1,
        )
    return html


def _has_script_tag_boundary(html_lower: str, index: int, tag_prefix: str) -> bool:
    boundary_index = index + len(tag_prefix)
    if boundary_index >= len(html_lower):
        return True
    boundary = html_lower[boundary_index]
    return not (boundary.isalnum() or boundary in {"-", "_", ":"})


def _iter_inline_script_contents(html: str) -> Iterable[str]:
    html_lower = html.lower()
    position = 0
    while True:
        start = html_lower.find("<script", position)
        if start < 0:
            return
        if not _has_script_tag_boundary(html_lower, start, "<script"):
            position = start + 1
            continue
        script_start = html.find(">", start)
        if script_start < 0:
            return
        end = html_lower.find("</script", script_start + 1)
        while end >= 0 and not _has_script_tag_boundary(html_lower, end, "</script"):
            end = html_lower.find("</script", end + 1)
        if end < 0:
            return
        script_end = html.find(">", end)
        if script_end < 0:
            return
        yield html[script_start + 1 : end]
        position = script_end + 1


def _inline_script_hashes(html: str) -> list[str]:
    hashes: list[str] = []
    for script in _iter_inline_script_contents(html):
        if not script.strip():
            continue
        digest = hashlib.sha256(script.encode("utf-8")).digest()
        hashes.append(f"'sha256-{base64.b64encode(digest).decode('ascii')}'")
    return hashes


def security_headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'"
        ),
        "Referrer-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def index_response_headers() -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    headers.update(security_headers())
    return headers


def web_index_response_headers(static_dir: Path, base_path: str = "") -> dict[str, str]:
    headers = index_response_headers()
    html = render_web_index_html(static_dir, base_path=base_path)
    script_hashes = _inline_script_hashes(html)
    if not script_hashes:
        return headers
    csp = headers["Content-Security-Policy"]
    script_src = "script-src 'self' " + " ".join(script_hashes)
    headers["Content-Security-Policy"] = csp.replace("script-src 'self'", script_src)
    return headers
