from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logging_utils import log_event, safe_log
from .utils import atomic_write

HUB_PID_FILENAME = "hub.pid"
HUB_ENDPOINT_FILENAME = "hub_endpoint.json"
HUB_CLEAN_SHUTDOWN_FILENAME = ".hub-clean-shutdown"


def _hub_runtime_dir(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner"


def hub_pid_path(hub_root: Path) -> Path:
    return _hub_runtime_dir(hub_root) / HUB_PID_FILENAME


def hub_endpoint_path(hub_root: Path) -> Path:
    return _hub_runtime_dir(hub_root) / HUB_ENDPOINT_FILENAME


def hub_clean_shutdown_path(hub_root: Path) -> Path:
    return _hub_runtime_dir(hub_root) / HUB_CLEAN_SHUTDOWN_FILENAME


def _read_hub_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (OSError, TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _iso_from_mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_hub_endpoint(hub_root: Path) -> dict[str, Any] | None:
    endpoint_path = hub_endpoint_path(hub_root)
    pid = _read_hub_pid(hub_pid_path(hub_root))
    if pid is None or not _pid_is_running(pid):
        return None
    try:
        payload = json.loads(endpoint_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    url = payload.get("url")
    host = payload.get("host")
    port = payload.get("port")
    base_path = payload.get("base_path")
    if not isinstance(url, str) or not url.strip():
        return None
    if not isinstance(host, str) or not host.strip():
        return None
    if not isinstance(port, int) or port <= 0:
        return None
    if not isinstance(base_path, str):
        return None
    return {
        "url": url,
        "host": host,
        "port": port,
        "base_path": base_path,
    }


def record_hub_startup(
    hub_root: Path,
    logger: logging.Logger,
    *,
    pid: int | None = None,
    durable: bool = False,
    host: str | None = None,
    port: int | None = None,
    base_path: str | None = None,
) -> None:
    runtime_dir = _hub_runtime_dir(hub_root)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    pid_path = hub_pid_path(hub_root)
    endpoint_path = hub_endpoint_path(hub_root)
    clean_shutdown_path = hub_clean_shutdown_path(hub_root)
    effective_pid = os.getpid() if pid is None else int(pid)
    started_at = datetime.now(timezone.utc).isoformat()

    if not clean_shutdown_path.exists():
        previous_pid = _read_hub_pid(pid_path)
        if previous_pid is not None:
            fields: dict[str, Any] = {
                "pid": effective_pid,
                "previous_pid": previous_pid,
            }
            previous_started_at = _iso_from_mtime(pid_path)
            if previous_started_at is not None:
                fields["previous_started_at"] = previous_started_at
            log_event(logger, logging.WARNING, "hub_started_unclean", **fields)

    try:
        clean_shutdown_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        safe_log(
            logger,
            logging.WARNING,
            "Failed to clear hub clean-shutdown sentinel",
            exc=exc,
        )

    try:
        atomic_write(pid_path, f"{effective_pid}\n", durable=durable)
    except OSError as exc:
        safe_log(logger, logging.WARNING, "Failed to persist hub pid", exc=exc)

    if host is not None and port is not None and base_path is not None:
        endpoint = {
            "url": f"http://{host}:{int(port)}{base_path}",
            "host": host,
            "port": int(port),
            "base_path": base_path,
        }
        try:
            atomic_write(
                endpoint_path,
                json.dumps(endpoint) + "\n",
                durable=durable,
            )
        except OSError as exc:
            safe_log(logger, logging.WARNING, "Failed to persist hub endpoint", exc=exc)

    log_event(
        logger,
        logging.INFO,
        "hub_started",
        pid=effective_pid,
        started_at=started_at,
    )


def record_hub_clean_shutdown(
    hub_root: Path,
    logger: logging.Logger,
    *,
    pid: int | None = None,
    durable: bool = False,
) -> None:
    endpoint_path = hub_endpoint_path(hub_root)
    clean_shutdown_path = hub_clean_shutdown_path(hub_root)
    shutdown_at = datetime.now(timezone.utc).isoformat()
    effective_pid = os.getpid() if pid is None else int(pid)
    try:
        endpoint_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        safe_log(logger, logging.WARNING, "Failed to remove hub endpoint", exc=exc)
    try:
        atomic_write(clean_shutdown_path, f"{shutdown_at}\n", durable=durable)
    except OSError as exc:
        safe_log(
            logger,
            logging.WARNING,
            "Failed to persist hub clean-shutdown sentinel",
            exc=exc,
        )
        return

    log_event(
        logger,
        logging.INFO,
        "hub_shutdown_clean",
        pid=effective_pid,
        shutdown_at=shutdown_at,
    )


def _is_ignorable_exception(
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
) -> bool:
    if exc_type is not None and issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        return True
    return isinstance(exc, asyncio.CancelledError)


def _log_uncaught_exception(
    logger: logging.Logger,
    *,
    source: str,
    message: str,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    traceback: Any,
    **fields: Any,
) -> None:
    if _is_ignorable_exception(exc_type, exc):
        return
    logged_exc = exc if isinstance(exc, Exception) else None
    log_event(
        logger,
        logging.ERROR,
        "hub_uncaught_exception",
        source=source,
        message=message,
        exc=logged_exc,
        **fields,
    )
    if exc_type is not None and exc is not None and traceback is not None:
        logger.error(message, exc_info=(exc_type, exc, traceback))
        return
    safe_log(logger, logging.ERROR, message)


@dataclass
class HubExceptionHooks:
    loop: asyncio.AbstractEventLoop
    previous_sys_excepthook: Any
    previous_threading_excepthook: Any
    previous_asyncio_exception_handler: Any

    def restore(self) -> None:
        sys.excepthook = self.previous_sys_excepthook
        threading.excepthook = self.previous_threading_excepthook
        self.loop.set_exception_handler(self.previous_asyncio_exception_handler)


def install_hub_exception_hooks(
    *,
    logger: logging.Logger,
    loop: asyncio.AbstractEventLoop,
) -> HubExceptionHooks:
    previous_sys_excepthook = sys.excepthook
    previous_threading_excepthook = threading.excepthook
    previous_asyncio_exception_handler = loop.get_exception_handler()

    def _sys_excepthook(
        exc_type: type[BaseException],
        exc: BaseException,
        traceback: Any,
    ) -> None:
        _log_uncaught_exception(
            logger,
            source="sys",
            message="Unhandled hub exception",
            exc_type=exc_type,
            exc=exc,
            traceback=traceback,
        )
        previous_sys_excepthook(exc_type, exc, traceback)

    def _threading_excepthook(args: threading.ExceptHookArgs) -> None:
        thread_name = getattr(args.thread, "name", None)
        _log_uncaught_exception(
            logger,
            source="thread",
            message="Unhandled hub thread exception",
            exc_type=args.exc_type,
            exc=args.exc_value,
            traceback=args.exc_traceback,
            thread_name=thread_name,
        )
        previous_threading_excepthook(args)

    def _asyncio_exception_handler(
        current_loop: asyncio.AbstractEventLoop,
        context: dict[str, Any],
    ) -> None:
        exc = context.get("exception")
        exc_type = type(exc) if isinstance(exc, BaseException) else None
        fields: dict[str, Any] = {}
        for key in ("task", "future", "handle"):
            value = context.get(key)
            if value is not None:
                fields[key] = repr(value)
        _log_uncaught_exception(
            logger,
            source="asyncio",
            message=str(context.get("message") or "Unhandled hub asyncio exception"),
            exc_type=exc_type,
            exc=exc if isinstance(exc, BaseException) else None,
            traceback=getattr(exc, "__traceback__", None),
            **fields,
        )
        if previous_asyncio_exception_handler is not None:
            previous_asyncio_exception_handler(current_loop, context)
            return
        current_loop.default_exception_handler(context)

    sys.excepthook = _sys_excepthook
    threading.excepthook = _threading_excepthook
    loop.set_exception_handler(_asyncio_exception_handler)
    return HubExceptionHooks(
        loop=loop,
        previous_sys_excepthook=previous_sys_excepthook,
        previous_threading_excepthook=previous_threading_excepthook,
        previous_asyncio_exception_handler=previous_asyncio_exception_handler,
    )
