from __future__ import annotations

import logging
import os
import signal
import time
from typing import Optional

from .logging_utils import log_event


def _linux_child_pids_for_parent(parent_pid: int) -> list[int]:
    """Direct child PIDs for a parent (Linux).

    Prefer `/proc/.../children` when the kernel exposes it; many containers
    omit that file, so fall back to scanning `/proc/*/status` for ``PPid:``.
    """
    if parent_pid <= 0:
        return []
    path = f"/proc/{parent_pid}/task/{parent_pid}/children"
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        text = ""
    if text:
        result: list[int] = []
        for part in text.split():
            try:
                result.append(int(part))
            except ValueError:
                continue
        return result

    proc_root = "/proc"
    try:
        names = os.listdir(proc_root)
    except OSError:
        return []
    children: list[int] = []
    for name in names:
        if not name.isdigit():
            continue
        try:
            candidate = int(name)
        except ValueError:
            continue
        if candidate == parent_pid:
            continue
        status_path = os.path.join(proc_root, name, "status")
        try:
            with open(status_path, encoding="utf-8") as handle:
                for line in handle:
                    if not line.startswith("PPid:"):
                        continue
                    parts = line.split()
                    if len(parts) < 2:
                        break
                    if int(parts[1]) == parent_pid:
                        children.append(candidate)
                    break
        except (OSError, ValueError):
            continue
    return children


def terminate_pid(
    pid: int,
    *,
    grace_seconds: float,
    kill_seconds: float,
    logger: Optional[logging.Logger] = None,
    event_prefix: str = "process_termination",
) -> bool:
    """
    Terminate a single pid via SIGTERM then SIGKILL on POSIX.
    """
    if pid <= 0:
        return False

    grace_seconds = max(0.0, float(grace_seconds))
    kill_seconds = max(0.0, float(kill_seconds))
    _log_event(
        logger,
        logging.DEBUG,
        event_prefix,
        "terminate_pid.start",
        pid=pid,
        grace_seconds=grace_seconds,
        kill_seconds=kill_seconds,
    )

    if not _send_pid_signal(
        pid, signal.SIGTERM, logger=logger, event_prefix=event_prefix
    ):
        _log_event(
            logger,
            logging.WARNING,
            event_prefix,
            "terminate_pid.failed",
            pid=pid,
            phase="sigterm",
        )
        return False

    if grace_seconds > 0:
        time.sleep(grace_seconds)

    kill_signal = signal.SIGTERM if os.name == "nt" else signal.SIGKILL
    if not _send_pid_signal(pid, kill_signal, logger=logger, event_prefix=event_prefix):
        _log_event(
            logger,
            logging.WARNING,
            event_prefix,
            "terminate_pid.failed",
            pid=pid,
            phase="sigkill" if os.name != "nt" else "sigterm",
        )
        return False

    if kill_seconds > 0:
        time.sleep(kill_seconds)

    _log_event(
        logger,
        logging.DEBUG,
        event_prefix,
        "terminate_pid.complete",
        pid=pid,
    )
    return True


def terminate_process_group(
    pgid: int,
    *,
    grace_seconds: float,
    kill_seconds: float,
    logger: Optional[logging.Logger] = None,
    event_prefix: str = "process_termination",
) -> bool:
    """
    Terminate a process group via killpg with SIGTERM then SIGKILL on POSIX.
    """
    if pgid <= 0:
        return False
    if os.name == "nt" or not hasattr(os, "killpg"):
        _log_event(
            logger,
            logging.DEBUG,
            event_prefix,
            "terminate_process_group.windows_fallback",
            pgid=pgid,
            os_name=os.name,
        )
        return terminate_pid(
            pgid,
            grace_seconds=grace_seconds,
            kill_seconds=kill_seconds,
            logger=logger,
            event_prefix=event_prefix,
        )

    grace_seconds = max(0.0, float(grace_seconds))
    kill_seconds = max(0.0, float(kill_seconds))
    _log_event(
        logger,
        logging.DEBUG,
        event_prefix,
        "terminate_process_group.start",
        pgid=pgid,
        grace_seconds=grace_seconds,
        kill_seconds=kill_seconds,
    )

    if not _send_pgid_signal(
        pgid,
        signal.SIGTERM,
        logger=logger,
        event_prefix=event_prefix,
    ):
        _log_event(
            logger,
            logging.WARNING,
            event_prefix,
            "terminate_process_group.failed",
            pgid=pgid,
            phase="sigterm",
        )
        return False

    if grace_seconds > 0:
        time.sleep(grace_seconds)

    if not _send_pgid_signal(
        pgid,
        signal.SIGKILL,
        logger=logger,
        event_prefix=event_prefix,
    ):
        _log_event(
            logger,
            logging.WARNING,
            event_prefix,
            "terminate_process_group.failed",
            pgid=pgid,
            phase="sigkill",
        )
        return False

    if kill_seconds > 0:
        time.sleep(kill_seconds)

    _log_event(
        logger,
        logging.DEBUG,
        event_prefix,
        "terminate_process_group.complete",
        pgid=pgid,
    )
    return True


def terminate_record(
    pid: int | None,
    pgid: int | None,
    *,
    grace_seconds: float,
    kill_seconds: float,
    logger: Optional[logging.Logger] = None,
    event_prefix: str = "process_termination",
) -> bool:
    """
    Terminate both a pid and optional process group record.

    POSIX sends SIGTERM to the process group first (if available), then
    SIGTERM/SIGKILL to pid as fallback.
    """
    _log_event(
        logger,
        logging.DEBUG,
        event_prefix,
        "terminate_record.start",
        pid=pid,
        pgid=pgid,
        grace_seconds=grace_seconds,
        kill_seconds=kill_seconds,
    )

    had_target = False
    group_ok = False
    pid_ok = False
    # Snapshot and terminate direct children before the parent: subprocess children
    # may live in a different process group than the parent, so killpg misses them,
    # and reparenting after the parent dies makes PPid-based discovery unreliable.
    child_pids = _linux_child_pids_for_parent(pid) if pid is not None else []
    children_ok = True
    for child_pid in child_pids:
        had_target = True
        if not terminate_pid(
            child_pid,
            grace_seconds=grace_seconds,
            kill_seconds=kill_seconds,
            logger=logger,
            event_prefix=event_prefix,
        ):
            children_ok = False

    if pgid is not None:
        had_target = True
        group_ok = terminate_process_group(
            pgid,
            grace_seconds=grace_seconds,
            kill_seconds=kill_seconds,
            logger=logger,
            event_prefix=event_prefix,
        )

    if pid is not None:
        had_target = True
        pid_ok = terminate_pid(
            pid,
            grace_seconds=grace_seconds,
            kill_seconds=kill_seconds,
            logger=logger,
            event_prefix=event_prefix,
        )

    ok = had_target and (group_ok or pid_ok or children_ok)
    if not had_target:
        _log_event(
            logger,
            logging.WARNING,
            event_prefix,
            "terminate_record.no_target",
            pid=pid,
            pgid=pgid,
        )
        return False

    if not ok:
        _log_event(
            logger,
            logging.WARNING,
            event_prefix,
            "terminate_record.failed",
            pid=pid,
            pgid=pgid,
        )

    return ok


def _send_pid_signal(
    pid: int,
    sig: int,
    *,
    logger: Optional[logging.Logger],
    event_prefix: str,
) -> bool:
    try:
        os.kill(pid, sig)
        _log_event(
            logger,
            logging.DEBUG,
            event_prefix,
            "signal.sent",
            target="pid",
            signal=sig,
            id=pid,
        )
        return True
    except ProcessLookupError:
        _log_event(
            logger,
            logging.DEBUG,
            event_prefix,
            "signal.not_found",
            target="pid",
            signal=sig,
            id=pid,
        )
        return True
    except PermissionError as exc:
        _log_event(
            logger,
            logging.WARNING,
            event_prefix,
            "signal.permission_denied",
            target="pid",
            signal=sig,
            id=pid,
            exc=exc,
        )
        return False
    except OSError as exc:
        _log_event(
            logger,
            logging.DEBUG,
            event_prefix,
            "signal.os_error",
            target="pid",
            signal=sig,
            id=pid,
            exc=exc,
        )
        return True


def _send_pgid_signal(
    pgid: int,
    sig: int,
    *,
    logger: Optional[logging.Logger],
    event_prefix: str,
) -> bool:
    try:
        os.killpg(pgid, sig)
        _log_event(
            logger,
            logging.DEBUG,
            event_prefix,
            "signal.sent",
            target="pgid",
            signal=sig,
            id=pgid,
        )
        return True
    except ProcessLookupError:
        _log_event(
            logger,
            logging.DEBUG,
            event_prefix,
            "signal.not_found",
            target="pgid",
            signal=sig,
            id=pgid,
        )
        return True
    except AttributeError as exc:
        _log_event(
            logger,
            logging.DEBUG,
            event_prefix,
            "signal.unsupported",
            target="pgid",
            signal=sig,
            id=pgid,
            exc=exc,
        )
        return False
    except PermissionError as exc:
        _log_event(
            logger,
            logging.WARNING,
            event_prefix,
            "signal.permission_denied",
            target="pgid",
            signal=sig,
            id=pgid,
            exc=exc,
        )
        return False
    except OSError as exc:
        _log_event(
            logger,
            logging.DEBUG,
            event_prefix,
            "signal.os_error",
            target="pgid",
            signal=sig,
            id=pgid,
            exc=exc,
        )
        return True


def _log_event(
    logger: Optional[logging.Logger],
    level: int,
    event_prefix: str,
    event_name: str,
    **fields,
) -> None:
    if logger is None:
        return
    full_event = _qualified_event(event_prefix, event_name)
    if fields:
        log_event(logger, level, full_event, **fields)
    else:
        log_event(logger, level, full_event)


def _qualified_event(event_prefix: str, event_name: str) -> str:
    prefix = (event_prefix or "process_termination").strip().rstrip(".")
    return f"{prefix}.{event_name}"
