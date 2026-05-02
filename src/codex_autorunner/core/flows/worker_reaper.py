from __future__ import annotations

import dataclasses
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ...flow_worker_reaper_constants import (
    DEFAULT_FLOW_WORKER_MAX_AGE_SECONDS,
    DEFAULT_FLOW_WORKER_TERMINATE_GRACE_SECONDS,
    DEFAULT_TERMINAL_RUN_GRACE_SECONDS,
)
from ..diagnostics.process_snapshot import collect_processes
from ..state_roots import resolve_repo_flows_db_path, resolve_repo_state_root
from ..text_utils import _pid_is_running
from .models import FlowRunRecord, parse_flow_timestamp
from .store import FlowStore
from .worker_process import write_worker_exit_info


@dataclasses.dataclass(frozen=True)
class FlowWorkerDiagnostic:
    run_id: str
    pid: int
    alive: bool
    classification: str
    status: Optional[str]
    metadata_path: Path
    metadata_age_seconds: Optional[float]
    rss_kb: Optional[int] = None
    command: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pid": self.pid,
            "alive": self.alive,
            "classification": self.classification,
            "status": self.status,
            "metadata_path": str(self.metadata_path),
            "metadata_age_seconds": self.metadata_age_seconds,
            "rss_kb": self.rss_kb,
            "command": self.command,
            "reason": self.reason,
        }


@dataclasses.dataclass(frozen=True)
class FlowWorkerReapSummary:
    scanned_count: int
    active_count: int
    stale_count: int
    zombie_count: int
    pruned_count: int
    memory_waste_kb: int
    workers: list[FlowWorkerDiagnostic]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_count": self.scanned_count,
            "active_count": self.active_count,
            "stale_count": self.stale_count,
            "zombie_count": self.zombie_count,
            "pruned_count": self.pruned_count,
            "memory_waste_kb": self.memory_waste_kb,
            "workers": [worker.to_dict() for worker in self.workers],
            "errors": list(self.errors),
        }


def _scan_flow_worker_diagnostics(
    repo_root: Path,
    *,
    stale_age_seconds: float,
) -> tuple[list[FlowWorkerDiagnostic], list[str], dict[str, FlowRunRecord]]:
    repo_root = repo_root.resolve()
    state_root = resolve_repo_state_root(repo_root)
    flows_root = state_root / "flows"
    if not flows_root.exists():
        return [], [], {}

    runs_by_id, load_errors = _load_flow_runs(repo_root)
    process_by_pid = {
        proc.pid: proc
        for proc in collect_processes().car_service_processes
        if _looks_like_flow_worker_command(proc.command)
    }

    diagnostics: list[FlowWorkerDiagnostic] = []
    for metadata_path in sorted(flows_root.glob("*/worker.json")):
        run_id = metadata_path.parent.name
        metadata = _read_json(metadata_path)
        raw_pid = metadata.get("pid")
        if raw_pid is None:
            continue
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue

        alive = _pid_is_running(pid)
        record = runs_by_id.get(run_id)
        proc = process_by_pid.get(pid)
        metadata_age = _metadata_age_seconds(metadata, metadata_path)
        classification, reason = _classify_worker(
            record, alive, metadata_age, stale_age_seconds=stale_age_seconds
        )
        diagnostics.append(
            FlowWorkerDiagnostic(
                run_id=run_id,
                pid=pid,
                alive=alive,
                classification=classification,
                status=record.status.value if record is not None else None,
                metadata_path=metadata_path,
                metadata_age_seconds=metadata_age,
                rss_kb=proc.rss_kb if proc is not None else None,
                command=proc.command if proc is not None else None,
                reason=reason,
            )
        )
    return diagnostics, load_errors, runs_by_id


def inspect_flow_workers(
    repo_root: Path,
    *,
    stale_age_seconds: float = DEFAULT_FLOW_WORKER_MAX_AGE_SECONDS,
) -> tuple[list[FlowWorkerDiagnostic], list[str]]:
    workers, load_errors, _ = _scan_flow_worker_diagnostics(
        repo_root, stale_age_seconds=stale_age_seconds
    )
    return workers, load_errors


def reap_stale_flow_workers(
    repo_root: Path,
    *,
    max_age_seconds: float = DEFAULT_FLOW_WORKER_MAX_AGE_SECONDS,
    terminal_grace_seconds: float = DEFAULT_TERMINAL_RUN_GRACE_SECONDS,
    terminate_grace_seconds: float = DEFAULT_FLOW_WORKER_TERMINATE_GRACE_SECONDS,
    prune: bool = True,
    logger: Optional[logging.Logger] = None,
) -> FlowWorkerReapSummary:
    workers, load_errors, runs_by_id = _scan_flow_worker_diagnostics(
        repo_root, stale_age_seconds=max_age_seconds
    )
    errors = list(load_errors)
    pruned = 0

    if load_errors:
        stale = [w for w in workers if w.classification == "stale"]
        zombies = [w for w in workers if w.classification == "zombie"]
        active = [w for w in workers if w.classification == "active"]
        memory_waste_kb = sum(int(w.rss_kb or 0) for w in stale + zombies if w.alive)
        return FlowWorkerReapSummary(
            scanned_count=len(workers),
            active_count=len(active),
            stale_count=len(stale),
            zombie_count=len(zombies),
            pruned_count=0,
            memory_waste_kb=memory_waste_kb,
            workers=workers,
            errors=errors,
        )

    for worker in workers:
        if not worker.alive:
            continue
        if not _eligible_for_reap(
            worker,
            runs_by_id,
            max_age_seconds=max_age_seconds,
            terminal_grace_seconds=terminal_grace_seconds,
        ):
            continue
        if not prune:
            continue
        try:
            _terminate_worker(
                repo_root,
                worker,
                terminate_grace_seconds=terminate_grace_seconds,
            )
            pruned += 1
            if logger is not None:
                logger.info(
                    "flow_worker.reaped run_id=%s pid=%s classification=%s reason=%s",
                    worker.run_id,
                    worker.pid,
                    worker.classification,
                    worker.reason,
                )
        except OSError as exc:
            errors.append(f"{worker.run_id}:{worker.pid}: {exc}")

    stale = [w for w in workers if w.classification == "stale"]
    zombies = [w for w in workers if w.classification == "zombie"]
    active = [w for w in workers if w.classification == "active"]
    memory_waste_kb = sum(int(w.rss_kb or 0) for w in stale + zombies if w.alive)
    return FlowWorkerReapSummary(
        scanned_count=len(workers),
        active_count=len(active),
        stale_count=len(stale),
        zombie_count=len(zombies),
        pruned_count=pruned,
        memory_waste_kb=memory_waste_kb,
        workers=workers,
        errors=errors,
    )


def _load_flow_runs(repo_root: Path) -> tuple[dict[str, FlowRunRecord], list[str]]:
    db_path = resolve_repo_flows_db_path(repo_root)
    if not db_path.exists():
        return {}, []
    try:
        with FlowStore.connect_readonly(db_path) as store:
            return (
                {record.id: record for record in store.list_flow_runs()},
                [],
            )
    except (OSError, RuntimeError, ValueError) as exc:
        return {}, [f"flows_db_read_failed:{exc}"]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _metadata_age_seconds(
    metadata: dict[str, Any], metadata_path: Path
) -> Optional[float]:
    spawned_at = metadata.get("spawned_at")
    if isinstance(spawned_at, (int, float)) and not isinstance(spawned_at, bool):
        return max(0.0, time.time() - float(spawned_at))
    try:
        return max(0.0, time.time() - metadata_path.stat().st_mtime)
    except OSError:
        return None


def _classify_worker(
    record: Optional[FlowRunRecord],
    alive: bool,
    metadata_age_seconds: Optional[float],
    *,
    stale_age_seconds: float,
) -> tuple[str, str]:
    if not alive:
        return "dead", "pid_not_running"
    if record is None:
        return "zombie", "no_run_record"
    if record.status.is_terminal():
        return "stale", f"run_{record.status.value}"
    if (
        metadata_age_seconds is not None
        and metadata_age_seconds >= stale_age_seconds
    ):
        return "stale", "metadata_age_exceeded"
    return "active", f"run_{record.status.value}"


def _eligible_for_reap(
    worker: FlowWorkerDiagnostic,
    runs_by_id: dict[str, FlowRunRecord],
    *,
    max_age_seconds: float,
    terminal_grace_seconds: float,
) -> bool:
    if worker.classification == "zombie":
        return True
    record = runs_by_id.get(worker.run_id)
    if record is not None and record.status.is_terminal():
        finished_at = parse_flow_timestamp(record.finished_at)
        if finished_at is not None:
            terminal_age_seconds = (
                datetime.now(timezone.utc) - finished_at.astimezone(timezone.utc)
            ).total_seconds()
            if terminal_age_seconds >= terminal_grace_seconds:
                return True
    if worker.classification != "stale" and (
        worker.metadata_age_seconds is None
        or worker.metadata_age_seconds < max_age_seconds
    ):
        return False
    if worker.metadata_age_seconds is not None:
        return worker.metadata_age_seconds >= max_age_seconds
    return False


def _terminate_worker(
    repo_root: Path,
    worker: FlowWorkerDiagnostic,
    *,
    terminate_grace_seconds: float,
) -> None:
    write_worker_exit_info(
        repo_root,
        worker.run_id,
        returncode=-signal.SIGTERM,
        shutdown_intent=True,
    )
    _send_signal(worker.pid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, terminate_grace_seconds)
    while time.monotonic() < deadline:
        if not _pid_is_running(worker.pid):
            return
        time.sleep(0.1)
    if _pid_is_running(worker.pid):
        write_worker_exit_info(
            repo_root,
            worker.run_id,
            returncode=-signal.SIGKILL,
            shutdown_intent=True,
        )
        _send_signal(worker.pid, signal.SIGKILL)


def _send_signal(pid: int, sig: signal.Signals) -> None:
    if os.name != "nt" and hasattr(os, "getpgid") and hasattr(os, "killpg"):
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None
        if pgid == pid:
            os.killpg(pgid, sig)
            return
    os.kill(pid, sig)


def _looks_like_flow_worker_command(command: str) -> bool:
    command_lc = command.lower()
    return " flow worker " in f" {command_lc} " or (
        "codex_autorunner" in command_lc
        and " flow " in command_lc
        and " worker" in command_lc
    )
