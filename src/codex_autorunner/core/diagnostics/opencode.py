from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from ..config import RepoConfig, load_repo_config
from ..locks import process_command_matches
from ..managed_processes import ProcessRecord, list_process_records
from ..state_roots import resolve_global_state_root
from .types import DoctorCheck


def summarize_opencode_lifecycle(
    repo_root: Path,
    *,
    repo_config: Optional[RepoConfig] = None,
    backend_orchestrator: Optional[Any] = None,
) -> dict[str, Any]:
    if repo_config is None:
        repo_config = load_repo_config(repo_root)

    agent_cfg = repo_config.agents.get("opencode")
    external_base_url = agent_cfg.base_url if agent_cfg else None
    server_scope = getattr(repo_config.opencode, "server_scope", "workspace")
    registry_repo_root = (
        resolve_global_state_root().resolve()
        if server_scope == "global"
        else repo_root.resolve()
    )

    try:
        records = list_process_records(registry_repo_root, kind="opencode")
    except (RuntimeError, OSError, ValueError, TypeError):
        records = []

    deduped_records: list[ProcessRecord] = []
    records_by_pid: dict[int, ProcessRecord] = {}
    for record in records:
        if record.pid is None:
            deduped_records.append(record)
            continue
        existing = records_by_pid.get(record.pid)
        if existing is None or (
            existing.workspace_id is None and record.workspace_id is not None
        ):
            records_by_pid[record.pid] = record

    seen_pid_records = {id(record) for record in records_by_pid.values()}
    deduped_records.extend(
        record
        for record in records
        if record.pid is not None and id(record) in seen_pid_records
    )

    managed_servers: list[dict[str, Any]] = []
    for record in deduped_records:
        status = "active" if _opencode_record_is_running(record) else "stale"
        metadata = record.metadata if isinstance(record.metadata, dict) else {}
        managed_servers.append(
            {
                "workspace_id": record.workspace_id,
                "pid": record.pid,
                "pgid": record.pgid,
                "base_url": record.base_url,
                "owner_pid": record.owner_pid,
                "owner_alive": _pid_exists(record.owner_pid),
                "status": status,
                "workspace_root": metadata.get("workspace_root"),
                "server_scope": metadata.get("server_scope") or server_scope,
                "process_origin": metadata.get("process_origin") or "unknown",
                "last_attach_mode": metadata.get("last_attach_mode") or "unknown",
            }
        )

    live_handles: list[dict[str, Any]] = []
    supervisor = _existing_opencode_supervisor(backend_orchestrator)
    if supervisor is not None:
        snapshot = getattr(supervisor, "observability_snapshot", None)
        if callable(snapshot):
            try:
                payload = snapshot()
            except (RuntimeError, OSError, ValueError, TypeError):
                payload = {}
            if isinstance(payload, dict):
                raw_handles = payload.get("handles")
                if isinstance(raw_handles, list):
                    live_handles = [
                        handle for handle in raw_handles if isinstance(handle, dict)
                    ]

    counts = {
        "active": sum(1 for record in managed_servers if record["status"] == "active"),
        "stale": sum(1 for record in managed_servers if record["status"] == "stale"),
        "spawned_local": sum(
            1
            for record in managed_servers
            if record["process_origin"] == "spawned_local"
        ),
        "registry_reuse": sum(
            1
            for record in managed_servers
            if record["last_attach_mode"] == "registry_reuse"
        ),
    }

    return {
        "server_scope": server_scope,
        "external_base_url": external_base_url,
        "registry_root": str(registry_repo_root),
        "counts": counts,
        "managed_servers": managed_servers,
        "live_handles": live_handles,
    }


def _append_opencode_lifecycle_checks(
    checks: list[DoctorCheck],
    *,
    repo_root: Path,
    repo_config: RepoConfig,
    backend_orchestrator: Optional[Any],
    check_id: Optional[str],
) -> None:
    summary = summarize_opencode_lifecycle(
        repo_root,
        repo_config=repo_config,
        backend_orchestrator=backend_orchestrator,
    )
    lifecycle_check_id = check_id or "opencode.lifecycle.registry"
    external_check_id = check_id or "opencode.lifecycle.external"
    handles_check_id = check_id or "opencode.lifecycle.handles"

    external_base_url = summary.get("external_base_url")
    if isinstance(external_base_url, str) and external_base_url:
        checks.append(
            DoctorCheck(
                name="OpenCode external mode",
                passed=True,
                message=(
                    f"External OpenCode base_url configured: {external_base_url}. "
                    "No CAR-managed server teardown is expected for this path."
                ),
                severity="info",
                check_id=external_check_id,
            )
        )

    managed_servers = summary.get("managed_servers") or []
    if not managed_servers:
        checks.append(
            DoctorCheck(
                name="OpenCode lifecycle",
                passed=True,
                message="No CAR-managed OpenCode server records found.",
                severity="info",
                check_id=lifecycle_check_id,
            )
        )
    else:
        counts = summary.get("counts") or {}
        stale_records = int(counts.get("stale") or 0)
        sample = ", ".join(
            "{workspace_id}:{pid}:{mode}".format(
                workspace_id=record.get("workspace_id") or "pid-only",
                pid=record.get("pid") or "n/a",
                mode=record.get("last_attach_mode") or record.get("process_origin"),
            )
            for record in managed_servers[:3]
        )
        checks.append(
            DoctorCheck(
                name="OpenCode lifecycle",
                passed=stale_records == 0,
                message=(
                    "Managed OpenCode server records: "
                    f"active={counts.get('active', 0)} stale={stale_records} "
                    f"spawned_local={counts.get('spawned_local', 0)} "
                    f"registry_reuse={counts.get('registry_reuse', 0)} "
                    f"(registry={summary.get('registry_root')}). "
                    f"Sample: {sample}"
                ),
                severity="warning" if stale_records else "info",
                check_id=lifecycle_check_id,
                fix=(
                    None
                    if stale_records == 0
                    else "Inspect `car doctor processes --save` and clear stale OpenCode records/processes."
                ),
            )
        )

    live_handles = summary.get("live_handles") or []
    if live_handles:
        handle_counts: dict[str, int] = {}
        for handle in live_handles:
            mode = str(handle.get("mode") or "unknown")
            handle_counts[mode] = handle_counts.get(mode, 0) + 1
        checks.append(
            DoctorCheck(
                name="OpenCode live handles",
                passed=True,
                message=(
                    "Live OpenCode handles: "
                    + ", ".join(
                        f"{mode}={handle_counts[mode]}"
                        for mode in sorted(handle_counts)
                    )
                ),
                severity="info",
                check_id=handles_check_id,
            )
        )


def _pid_exists(pid: Optional[int]) -> bool:
    if pid is None or pid <= 0:
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


def _opencode_record_is_running(record: ProcessRecord) -> bool:
    pid = record.pid
    if pid is not None and _pid_exists(pid):
        cmd_matches = process_command_matches(pid, record.command)
        if cmd_matches is not False:
            return True
    pgid = record.pgid
    if pgid is None or os.name == "nt" or not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False
    return True


def _existing_opencode_supervisor(
    backend_orchestrator: Optional[Any],
) -> Optional[Any]:
    if backend_orchestrator is None:
        return None

    active_backend = getattr(backend_orchestrator, "_active_backend", None)
    supervisor = getattr(active_backend, "_supervisor", None)
    if supervisor is not None:
        return supervisor

    factory_getter = getattr(backend_orchestrator, "_agent_backend_factory", None)
    if callable(factory_getter):
        try:
            factory = factory_getter()
        except (RuntimeError, OSError, ValueError, TypeError):
            factory = None
        supervisor = getattr(factory, "_opencode_supervisor", None)
        if supervisor is not None:
            return supervisor

    return None


__all__ = ["summarize_opencode_lifecycle"]
