"""Flow controller provisioning, caching, and corrupt-store recovery.

This module owns the lifecycle of :class:`FlowController` instances: building
them, caching them per ``(repo_root, flow_type)``, initializing them, and
recovering from a corrupted flow database.

It lives in ``codex_autorunner.flows`` -- the flow composition root -- rather
than in a surface. Surfaces (web routes, CLI commands) render and drive state;
they are not state owners, and deciding that a SQLite file is corrupt, rotating
it aside, and re-initializing the store is not a rendering concern. Previously
this logic lived in ``surfaces/web/routes/flow_routes/runtime_service.py``,
which meant an HTTP route module decided when a user's durable flow database
should be replaced.

Failures surface as :class:`FlowControllerUnavailable`, a transport-agnostic
error. Callers translate it into whatever their protocol needs (the web layer
maps it to HTTP 503). Nothing here imports a web framework.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, MutableMapping, Optional, cast

from ..core.flows import FlowController, FlowDefinition, FlowStore

_logger = logging.getLogger(__name__)

_FLOW_DB_CORRUPT_SUFFIX = ".corrupt"
_FLOW_DB_NOTICE_SUFFIX = ".corrupt.json"

# Errors that mean "this store did not come up", as opposed to a programming
# error. Kept explicit so a genuine bug is not swallowed by recovery.
_STORE_INIT_ERRORS = (sqlite3.Error, OSError, RuntimeError)


class FlowControllerUnavailable(RuntimeError):
    """A flow controller could not be provisioned for a repo.

    Raised after recovery has been attempted and failed. Surfaces translate
    this into their own vocabulary; it deliberately carries no HTTP status.
    """

    def __init__(self, repo_root: Path, flow_type: str, cause: Exception) -> None:
        super().__init__(
            f"Flow controller unavailable for {repo_root} (flow_type={flow_type}): {cause}"
        )
        self.repo_root = repo_root
        self.flow_type = flow_type
        self.cause = cause


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def is_probably_corrupt_flow_db_error(exc: Exception, db_path: Path) -> bool:
    """Best-effort classification of a SQLite failure as store corruption.

    Deliberately conservative: a false positive here rotates a user's flow
    database aside, so anything not clearly corruption is treated as a normal
    error and propagated.
    """
    if not isinstance(exc, sqlite3.Error):
        return False
    msg = str(exc).lower()
    if "file is not a database" in msg or "database disk image is malformed" in msg:
        return True
    if "disk i/o error" in msg:
        try:
            header = db_path.read_bytes()[:16]
        except OSError:
            return False
        return header not in (b"", b"SQLite format 3\x00")
    return False


def rotate_corrupt_flow_db(db_path: Path, detail: str) -> Optional[Path]:
    """Move a corrupt flow database aside and leave a machine-readable notice.

    Returns the backup path when the original was preserved, else ``None``.
    """
    from ..core.utils import atomic_write

    stamp = utc_stamp()
    backup_path = db_path.with_name(f"{db_path.name}{_FLOW_DB_CORRUPT_SUFFIX}.{stamp}")
    notice_path = db_path.with_name(f"{db_path.name}{_FLOW_DB_NOTICE_SUFFIX}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup_value: str = ""
    if db_path.exists():
        try:
            db_path.replace(backup_path)
            backup_value = str(backup_path)
        except OSError:
            backup_value = ""

    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(f"{db_path.name}{suffix}")
        try:
            sidecar.unlink(missing_ok=True)
        except OSError:
            pass

    notice = {
        "status": "corrupt",
        "message": "Flow store reset due to corrupted flows.db.",
        "detail": detail,
        "detected_at": stamp,
        "backup_path": backup_value,
    }
    try:
        atomic_write(notice_path, json.dumps(notice, indent=2) + "\n")
    except OSError:
        _logger.warning("Failed to write flow DB corruption notice at %s", notice_path)
    return backup_path if backup_value else None


def recover_flow_store(
    db_path: Path,
    exc: Exception,
    *,
    on_evict: Callable[[], None] | None = None,
) -> bool:
    """Rotate a corrupt flow store aside and re-initialize it.

    Returns True when ``exc`` was corruption and a fresh store came up. Returns
    False when ``exc`` was not corruption (caller should propagate it) or when
    recovery itself failed.

    ``on_evict`` runs after the database is rotated aside, so callers can drop
    any controller still holding the old file.

    This is the single implementation of flow-store recovery. Both the provider
    and the web surface's ``recover_flow_store_if_possible`` route through it,
    so the decision to replace a user's durable database is made in exactly one
    place.
    """
    if not is_probably_corrupt_flow_db_error(exc, db_path):
        return False

    backup_path = rotate_corrupt_flow_db(db_path, str(exc))
    if on_evict is not None:
        on_evict()

    store = FlowStore(db_path)
    try:
        store.initialize()
        _logger.warning(
            "Recovered corrupted flow DB at %s (backup=%s, reason=%s)",
            db_path,
            str(backup_path) if backup_path else "unavailable",
            exc,
        )
        return True
    except _STORE_INIT_ERRORS as recover_exc:
        _logger.warning(
            "Flow DB recovery failed at %s after error %s: %s",
            db_path,
            exc,
            recover_exc,
        )
        return False
    finally:
        try:
            store.close()
        except (sqlite3.Error, OSError):
            _logger.debug("Failed to close flow store during recovery", exc_info=True)


class FlowControllerProvider:
    """Caches and provisions :class:`FlowController` instances per repo.

    Thread-safe. The cache is keyed by ``(resolved repo_root, flow_type)``.

    ``definition_factory`` builds (and is expected to cache) the
    :class:`FlowDefinition` for a repo/flow_type; ``paths_factory`` resolves the
    ``(db_path, artifacts_root)`` pair. Both are injected so this module stays
    free of repo-layout policy.
    """

    def __init__(
        self,
        *,
        definition_factory: Callable[[Path, str], FlowDefinition],
        paths_factory: Callable[[Path], tuple[Path, Path]],
        cache: MutableMapping[tuple[Path, str], object] | None = None,
        lock: threading.Lock | None = None,
    ) -> None:
        self._definition_factory = definition_factory
        self._paths_factory = paths_factory
        # The cache container may be owned by the caller. Several call sites
        # (route definition builders, explicit eviction) address the same
        # mapping, so provisioning logic moves here without relocating the
        # storage those call sites already share.
        self._cache: MutableMapping[tuple[Path, str], object] = (
            cache if cache is not None else {}
        )
        self._lock = lock if lock is not None else threading.Lock()

    def get(self, repo_root: Path, flow_type: str) -> FlowController:
        """Return an initialized controller, recovering a corrupt store once.

        Raises :class:`FlowControllerUnavailable` if the controller cannot be
        initialized even after recovery.
        """
        repo_root = repo_root.resolve()
        key = (repo_root, flow_type)

        with self._lock:
            # The mapping is typed `object` because it may be owned by a caller
            # that stores it untyped; everything this provider puts in is a
            # FlowController, so read it back as one rather than isinstance-
            # filtering (which would silently discard and rebuild).
            cached = cast(Optional[FlowController], self._cache.get(key))
        if cached is not None:
            try:
                cached.initialize()
                return cached
            except _STORE_INIT_ERRORS as exc:
                if not self._recover_store(repo_root, flow_type, exc):
                    self.evict(repo_root, flow_type)
                    _logger.warning(
                        "Failed to initialize cached flow controller: %s", exc
                    )
                    raise FlowControllerUnavailable(repo_root, flow_type, exc) from exc

        controller = self._build(repo_root, flow_type)
        try:
            controller.initialize()
        except _STORE_INIT_ERRORS as exc:
            if not self._recover_store(repo_root, flow_type, exc):
                _logger.warning("Failed to initialize flow controller: %s", exc)
                raise FlowControllerUnavailable(repo_root, flow_type, exc) from exc
            # Recovery rotated the corrupt database aside; the retry builds
            # against the freshly initialized store.
            controller = self._build(repo_root, flow_type)
            try:
                controller.initialize()
            except _STORE_INIT_ERRORS as retry_exc:
                _logger.warning(
                    "Failed to initialize flow controller after recovery: %s",
                    retry_exc,
                )
                raise FlowControllerUnavailable(
                    repo_root, flow_type, retry_exc
                ) from retry_exc

        with self._lock:
            self._cache[key] = controller
        return controller

    def evict(self, repo_root: Path, flow_type: str) -> None:
        """Drop a cached controller, shutting it down best-effort."""
        key = (repo_root.resolve(), flow_type)
        with self._lock:
            controller = cast(Optional[FlowController], self._cache.pop(key, None))
        if controller is None:
            return
        try:
            controller.shutdown()
        except Exception:  # intentional: shutdown must not mask the original failure
            _logger.debug("Failed to shutdown cached flow controller", exc_info=True)

    def clear(self) -> None:
        """Evict every cached controller (process shutdown, tests)."""
        with self._lock:
            keys = list(self._cache)
        for repo_root, flow_type in list(keys):
            self.evict(repo_root, flow_type)

    def _build(self, repo_root: Path, flow_type: str) -> FlowController:
        db_path, artifacts_root = self._paths_factory(repo_root)
        definition = self._definition_factory(repo_root, flow_type)
        return FlowController(
            definition=definition,
            db_path=db_path,
            artifacts_root=artifacts_root,
        )

    def _recover_store(self, repo_root: Path, flow_type: str, exc: Exception) -> bool:
        """Rotate a corrupt store aside and drop the stale cached controller."""
        db_path, _ = self._paths_factory(repo_root)
        return recover_flow_store(
            db_path,
            exc,
            on_evict=lambda: self.evict(repo_root, flow_type),
        )


__all__ = [
    "FlowControllerProvider",
    "recover_flow_store",
    "FlowControllerUnavailable",
    "is_probably_corrupt_flow_db_error",
    "rotate_corrupt_flow_db",
    "utc_stamp",
]
