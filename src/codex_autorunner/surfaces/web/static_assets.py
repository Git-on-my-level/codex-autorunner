from __future__ import annotations

import base64
import enum
import hashlib
import json
import logging
import os
import shutil
import time
from contextlib import ExitStack
from importlib import resources
from pathlib import Path
from typing import Iterable, Optional
from uuid import uuid4

from ...core.logging_utils import safe_log

_ASSET_VERSION_TOKEN = "__CAR_ASSET_VERSION__"


class StaticAssetProvenance(enum.Enum):
    SOURCE_MATERIALIZE = "source_materialize"
    FINGERPRINT_CACHE_HIT = "fingerprint_cache_hit"
    EXISTING_CACHE_FALLBACK = "existing_cache_fallback"


_REQUIRED_STATIC_ASSETS: tuple[str, ...] = ()


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
    """Locate packaged static assets."""

    # Fallback must reach the `codex_autorunner` package root (`.../surfaces/web` → parents[2]).
    _pkg_root = Path(__file__).resolve().parents[2]

    static_root = resources.files("codex_autorunner").joinpath("static")
    if isinstance(static_root, Path):
        if static_root.exists():
            return static_root, None
        fallback = _pkg_root / "static"
        return fallback, None

    stack = ExitStack()
    try:
        static_path = stack.enter_context(resources.as_file(static_root))
    except (
        Exception
    ):  # intentional: importlib.resources can raise varied errors across Python versions
        stack.close()
        fallback = _pkg_root / "static"
        return fallback, None
    if static_path.exists():
        return static_path, stack

    stack.close()
    fallback = _pkg_root / "static"
    return fallback, None


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


def render_index_html(static_dir: Path, version: Optional[str]) -> str:
    index_path = static_dir / "index.html"
    text = index_path.read_text(encoding="utf-8")
    if version:
        text = text.replace(_ASSET_VERSION_TOKEN, version)
    return text


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
    # CSP: scripts are all local with no inline JS; runtime UI uses inline styles.
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


def _cleanup_temp_dir(path: Path, logger: logging.Logger) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        safe_log(
            logger,
            logging.WARNING,
            "Failed to remove temporary static cache dir %s",
            path,
            exc=exc,
        )


def _acquire_cache_lock(lock_path: Path, logger: logging.Logger) -> bool:
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError as exc:
        safe_log(
            logger,
            logging.WARNING,
            "Failed to create static cache lock %s",
            lock_path,
            exc=exc,
        )
        return False
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return True


def _release_cache_lock(lock_path: Path, logger: logging.Logger) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        safe_log(
            logger,
            logging.WARNING,
            "Failed to remove static cache lock %s",
            lock_path,
            exc=exc,
        )


def _cache_dir_mtime(path: Path) -> float:
    index_path = path / "index.html"
    try:
        return index_path.stat().st_mtime
    except OSError:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0


def _list_cache_entries(cache_root: Path) -> list[Path]:
    if not cache_root.exists():
        return []
    entries: list[Path] = []
    try:
        for entry in cache_root.iterdir():
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    entries.append(entry)
            except OSError:
                continue
    except OSError:
        return []
    return entries


def _select_latest_valid_cache(cache_root: Path) -> Optional[Path]:
    candidates = []
    for entry in _list_cache_entries(cache_root):
        if not missing_static_assets(entry):
            candidates.append(entry)
    if not candidates:
        return None
    candidates.sort(key=_cache_dir_mtime, reverse=True)
    return candidates[0]


def _prune_cache_entries(
    cache_root: Path,
    *,
    keep: set[Path],
    max_cache_entries: int,
    max_cache_age_days: Optional[int],
    logger: logging.Logger,
) -> None:
    if max_cache_entries <= 0 and max_cache_age_days is None:
        return
    entries = _list_cache_entries(cache_root)
    if not entries:
        return
    now = time.time()
    if max_cache_age_days is not None:
        cutoff = now - (max_cache_age_days * 86400)
        for entry in list(entries):
            if entry in keep:
                continue
            if _cache_dir_mtime(entry) < cutoff:
                try:
                    shutil.rmtree(entry)
                    entries.remove(entry)
                except OSError as exc:
                    safe_log(
                        logger,
                        logging.WARNING,
                        "Failed to remove stale static cache dir %s",
                        entry,
                        exc=exc,
                    )
    if max_cache_entries > 0 and len(entries) > max_cache_entries:
        removable = [entry for entry in entries if entry not in keep]
        removable.sort(key=_cache_dir_mtime)
        for entry in removable[: len(entries) - max_cache_entries]:
            try:
                shutil.rmtree(entry)
            except OSError as exc:
                safe_log(
                    logger,
                    logging.WARNING,
                    "Failed to remove old static cache dir %s",
                    entry,
                    exc=exc,
                )


def materialize_static_assets(
    cache_root: Path,
    *,
    max_cache_entries: int,
    max_cache_age_days: Optional[int],
    logger: logging.Logger,
) -> tuple[Path, Optional[ExitStack], StaticAssetProvenance]:
    static_dir, static_context = resolve_static_dir()
    if not static_dir.exists():
        if static_context is not None:
            static_context.close()
        target_dir = cache_root / "_no_legacy_static"
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_log(
            logger,
            logging.DEBUG,
            "static_assets: [%s] legacy static dir removed; using empty placeholder",
            StaticAssetProvenance.EXISTING_CACHE_FALLBACK.value,
        )
        return target_dir, None, StaticAssetProvenance.EXISTING_CACHE_FALLBACK
    existing_cache = _select_latest_valid_cache(cache_root)
    missing_source = missing_static_assets(static_dir)
    if missing_source:
        if static_context is not None:
            static_context.close()
        if existing_cache is not None:
            safe_log(
                logger,
                logging.INFO,
                "static_assets: [%s] serving from existing cache (source missing %s)",
                StaticAssetProvenance.EXISTING_CACHE_FALLBACK.value,
                ", ".join(missing_source[:3]),
            )
            _prune_cache_entries(
                cache_root,
                keep={existing_cache},
                max_cache_entries=max_cache_entries,
                max_cache_age_days=max_cache_age_days,
                logger=logger,
            )
            return existing_cache, None, StaticAssetProvenance.EXISTING_CACHE_FALLBACK
        raise RuntimeError("Static UI assets missing; reinstall package")
    fingerprint = asset_version(static_dir)
    target_dir = cache_root / fingerprint
    if target_dir.exists() and not missing_static_assets(target_dir):
        if static_context is not None:
            static_context.close()
        safe_log(
            logger,
            logging.DEBUG,
            "static_assets: [%s] serving from fingerprint cache %s",
            StaticAssetProvenance.FINGERPRINT_CACHE_HIT.value,
            fingerprint[:12],
        )
        _prune_cache_entries(
            cache_root,
            keep={target_dir},
            max_cache_entries=max_cache_entries,
            max_cache_age_days=max_cache_age_days,
            logger=logger,
        )
        return target_dir, None, StaticAssetProvenance.FINGERPRINT_CACHE_HIT
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        safe_log(
            logger,
            logging.WARNING,
            "Failed to create static cache root %s",
            cache_root,
            exc=exc,
        )
        if static_context is not None:
            static_context.close()
        if existing_cache is not None:
            safe_log(
                logger,
                logging.WARNING,
                "static_assets: [%s] serving from existing cache after mkdir failure",
                StaticAssetProvenance.EXISTING_CACHE_FALLBACK.value,
            )
            return existing_cache, None, StaticAssetProvenance.EXISTING_CACHE_FALLBACK
        raise RuntimeError("Static UI assets missing; reinstall package") from exc
    lock_path = cache_root / f".lock-{fingerprint}"
    lock_acquired = _acquire_cache_lock(lock_path, logger)
    try:
        if not lock_acquired:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if target_dir.exists() and not missing_static_assets(target_dir):
                    if static_context is not None:
                        static_context.close()
                    _prune_cache_entries(
                        cache_root,
                        keep={target_dir},
                        max_cache_entries=max_cache_entries,
                        max_cache_age_days=max_cache_age_days,
                        logger=logger,
                    )
                    return target_dir, None, StaticAssetProvenance.FINGERPRINT_CACHE_HIT
                time.sleep(0.2)
        temp_dir = cache_root / f".tmp-{fingerprint}-{uuid4().hex}"
        try:
            shutil.copytree(static_dir, temp_dir, symlinks=True)
            missing = missing_static_assets(temp_dir)
            if missing:
                safe_log(
                    logger,
                    logging.WARNING,
                    "Static UI assets missing in cache copy %s: %s",
                    temp_dir,
                    ", ".join(missing),
                )
                raise RuntimeError("Static UI assets missing; reinstall package")
            if target_dir.exists():
                existing_missing = missing_static_assets(target_dir)
                if not existing_missing:
                    _cleanup_temp_dir(temp_dir, logger)
                    if static_context is not None:
                        static_context.close()
                    _prune_cache_entries(
                        cache_root,
                        keep={target_dir},
                        max_cache_entries=max_cache_entries,
                        max_cache_age_days=max_cache_age_days,
                        logger=logger,
                    )
                    return (
                        target_dir,
                        None,
                        StaticAssetProvenance.FINGERPRINT_CACHE_HIT,
                    )
                try:
                    shutil.rmtree(target_dir)
                except OSError as exc:
                    safe_log(
                        logger,
                        logging.WARNING,
                        "Failed to replace stale static cache dir %s",
                        target_dir,
                        exc=exc,
                    )
                    raise RuntimeError(
                        "Static UI assets missing; reinstall package"
                    ) from exc
            temp_dir.replace(target_dir)
        except (
            Exception
        ) as exc:  # intentional: top-level error handler for asset materialization
            _cleanup_temp_dir(temp_dir, logger)
            if static_context is not None:
                static_context.close()
            if existing_cache is not None:
                safe_log(
                    logger,
                    logging.WARNING,
                    "static_assets: [%s] serving from existing cache after copy failure",
                    StaticAssetProvenance.EXISTING_CACHE_FALLBACK.value,
                )
                return (
                    existing_cache,
                    None,
                    StaticAssetProvenance.EXISTING_CACHE_FALLBACK,
                )
            raise RuntimeError("Static UI assets missing; reinstall package") from exc
        if static_context is not None:
            static_context.close()
        _prune_cache_entries(
            cache_root,
            keep={target_dir},
            max_cache_entries=max_cache_entries,
            max_cache_age_days=max_cache_age_days,
            logger=logger,
        )
        safe_log(
            logger,
            logging.DEBUG,
            "static_assets: [%s] materialized fresh cache %s",
            StaticAssetProvenance.SOURCE_MATERIALIZE.value,
            fingerprint[:12],
        )
        return target_dir, None, StaticAssetProvenance.SOURCE_MATERIALIZE
    finally:
        if lock_acquired:
            _release_cache_lock(lock_path, logger)


def require_static_assets(static_dir: Path, logger: logging.Logger) -> None:
    missing = missing_static_assets(static_dir)
    if not missing:
        return
    safe_log(
        logger,
        logging.ERROR,
        "Static UI assets missing in %s: %s",
        static_dir,
        ", ".join(missing),
    )
    raise RuntimeError("Static UI assets missing; reinstall package")
