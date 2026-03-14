from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from ...bootstrap import seed_repo_files
from ...manifest import ManifestError, load_manifest
from ..pma_thread_store import PmaThreadStore
from ...tickets.files import list_ticket_paths
from ...tickets.outbox import resolve_outbox_paths
from ..archive import (
    ArchiveEntrySpec,
    build_common_car_archive_entries,
    execute_archive_entries,
)
from ..config import ConfigError, load_repo_config
from .models import FlowRunStatus
from .store import FlowStore


def flow_run_artifacts_root(repo_root: Path, run_id: str) -> Path:
    return repo_root / ".codex-autorunner" / "flows" / run_id


def flow_run_archive_root(repo_root: Path, run_id: str) -> Path:
    return repo_root / ".codex-autorunner" / "archive" / "runs" / run_id


def _get_durable_writes(repo_root: Path) -> bool:
    """Get durable_writes from repo config, defaulting to False if uninitialized."""
    try:
        return load_repo_config(repo_root).durable_writes
    except ConfigError:
        return False


def _next_archive_dir(base_dir: Path) -> Path:
    if not base_dir.exists():
        return base_dir
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return base_dir.parent / f"{base_dir.name}_{suffix}"


def _contextspace_source(car_root: Path) -> Path:
    contextspace = car_root / "contextspace"
    if contextspace.exists() or contextspace.is_symlink():
        return contextspace
    legacy_workspace = car_root / "workspace"
    if legacy_workspace.exists() or legacy_workspace.is_symlink():
        return legacy_workspace
    return contextspace


def _find_hub_root(repo_root: Path) -> Path:
    current = repo_root.resolve()
    for _ in range(5):
        manifest_path = current / ".codex-autorunner" / "manifest.yml"
        if manifest_path.exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return repo_root.resolve()


def _has_hub_manifest(hub_root: Path) -> bool:
    return (hub_root / ".codex-autorunner" / "manifest.yml").exists()


def _resolve_repo_id(repo_root: Path, hub_root: Path) -> Optional[str]:
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    try:
        manifest = load_manifest(manifest_path, hub_root)
    except ManifestError:
        return None
    entry = manifest.get_by_path(hub_root, repo_root)
    if entry is None or not isinstance(entry.id, str) or not entry.id.strip():
        return None
    return entry.id.strip()


def _archive_ticket_flow_pma_threads(repo_root: Path, run_id: str) -> dict[str, Any]:
    hub_root = _find_hub_root(repo_root)
    if not _has_hub_manifest(hub_root):
        return {
            "archived_pma_threads": 0,
            "archived_pma_thread_ids": [],
            "archived_pma_threads_skipped": "hub_manifest_missing",
        }
    store = PmaThreadStore(hub_root)
    repo_id = _resolve_repo_id(repo_root, hub_root)
    archived_thread_ids: list[str] = []

    for thread in store.list_threads(status="active", limit=2000):
        managed_thread_id = thread.get("managed_thread_id")
        if not isinstance(managed_thread_id, str) or not managed_thread_id.strip():
            continue
        workspace_root = thread.get("workspace_root")
        if (
            not isinstance(workspace_root, str)
            or Path(workspace_root).resolve() != repo_root
        ):
            continue
        thread_repo_id = thread.get("repo_id")
        if (
            repo_id
            and isinstance(thread_repo_id, str)
            and thread_repo_id.strip()
            and thread_repo_id.strip() != repo_id
        ):
            continue
        metadata = thread.get("metadata")
        thread_kind = (
            metadata.get("thread_kind") if isinstance(metadata, dict) else None
        )
        thread_run_id = metadata.get("run_id") if isinstance(metadata, dict) else None
        display_name = str(thread.get("name") or "").strip().lower()
        is_ticket_flow_thread = thread_kind == "ticket_flow" or display_name.startswith(
            "ticket-flow:"
        )
        if not is_ticket_flow_thread:
            continue
        if (
            isinstance(thread_run_id, str)
            and thread_run_id.strip()
            and thread_run_id != run_id
        ):
            continue
        store.archive_thread(managed_thread_id.strip())
        archived_thread_ids.append(managed_thread_id.strip())

    return {
        "archived_pma_threads": len(archived_thread_ids),
        "archived_pma_thread_ids": archived_thread_ids,
    }


def _build_flow_archive_entries(
    repo_root: Path,
    *,
    run_id: str,
    run_dir: Path,
) -> tuple[list[ArchiveEntrySpec], dict[str, Any]]:
    car_root = repo_root / ".codex-autorunner"
    archive_root = flow_run_archive_root(repo_root, run_id)
    flow_state_root = flow_run_artifacts_root(repo_root, run_id)
    target_runs_dir = _next_archive_dir(archive_root / "archived_runs")
    ticket_paths = list(list_ticket_paths(repo_root / ".codex-autorunner" / "tickets"))
    entries = build_common_car_archive_entries(
        car_root,
        archive_root,
        include_contextspace=False,
    )
    entries.append(
        ArchiveEntrySpec(
            label="contextspace",
            source=_contextspace_source(car_root),
            dest=archive_root / "contextspace",
            mode="move",
        )
    )
    entries.extend(
        ArchiveEntrySpec(
            label=f"archived_tickets/{ticket_path.name}",
            source=ticket_path,
            dest=archive_root / "archived_tickets" / ticket_path.name,
            mode="move",
        )
        for ticket_path in ticket_paths
    )
    entries.append(
        ArchiveEntrySpec(
            label=target_runs_dir.relative_to(archive_root).as_posix(),
            source=run_dir,
            dest=target_runs_dir,
            mode="move",
        )
    )
    entries.append(
        ArchiveEntrySpec(
            label="flow_state",
            source=flow_state_root,
            dest=archive_root / "flow_state",
            mode="move",
            required=False,
        )
    )
    summary: dict[str, Any] = {
        "archive_root": str(archive_root),
        "archived_runs_dir": str(target_runs_dir),
        "archived_flow_state_dir": str(archive_root / "flow_state"),
        "ticket_count": len(ticket_paths),
    }
    return entries, summary


def archive_flow_run_artifacts(
    repo_root: Path,
    *,
    run_id: str,
    force: bool,
    delete_run: bool,
    force_attestation: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        raise ValueError("Flow database not found.")

    with FlowStore(db_path, durable=_get_durable_writes(repo_root)) as store:
        record = store.get_flow_run(run_id)
        if record is None:
            raise ValueError(f"Flow run {run_id} not found.")

        status = record.status
        terminal = status.is_terminal()
        if not terminal and not (
            force and status in {FlowRunStatus.PAUSED, FlowRunStatus.STOPPING}
        ):
            raise ValueError(
                "Can only archive completed/stopped/failed runs (use --force for paused/stopping)."
            )

        runs_dir_raw = record.input_data.get("runs_dir")
        runs_dir = (
            Path(runs_dir_raw)
            if isinstance(runs_dir_raw, str) and runs_dir_raw
            else Path(".codex-autorunner/runs")
        )
        run_paths = resolve_outbox_paths(
            workspace_root=repo_root,
            runs_dir=runs_dir,
            run_id=record.id,
        )
        run_dir = run_paths.run_dir
        entries, archive_plan = _build_flow_archive_entries(
            repo_root, run_id=record.id, run_dir=run_dir
        )

        summary: dict[str, Any] = {
            "repo_root": str(repo_root),
            "run_id": record.id,
            "status": record.status.value,
            "run_dir": str(run_dir),
            "run_dir_exists": run_dir.exists() and run_dir.is_dir(),
            "archive_dir": archive_plan["archive_root"],
            "archived_runs_dir": archive_plan["archived_runs_dir"],
            "archived_flow_state_dir": archive_plan["archived_flow_state_dir"],
            "delete_run": delete_run,
            "deleted_run": False,
            "archived_tickets": 0,
            "archived_runs": False,
            "archived_contextspace": False,
            "archived_flow_state": False,
            "archived_pma_threads": 0,
            "archived_pma_thread_ids": [],
            "archived_pma_threads_skipped": None,
            "archived_pma_threads_error": None,
            "archived_paths": [],
        }
        execution = execute_archive_entries(entries, worktree_root=repo_root)
        moved_paths = set(execution.moved_paths)
        copied_paths = set(execution.copied_paths)
        summary["archived_tickets"] = sum(
            1 for path in moved_paths if path.startswith("archived_tickets/")
        )
        summary["archived_runs"] = "archived_runs" in moved_paths or any(
            path.startswith("archived_runs_") for path in moved_paths
        )
        summary["archived_contextspace"] = "contextspace" in (
            copied_paths | moved_paths
        )
        summary["archived_flow_state"] = "flow_state" in moved_paths
        summary["archived_paths"] = sorted(
            list(execution.copied_paths) + list(execution.moved_paths)
        )
        summary["missing_paths"] = list(execution.missing_paths)

        seed_repo_files(repo_root, force=False, git_required=False)
        try:
            summary.update(_archive_ticket_flow_pma_threads(repo_root, record.id))
        except Exception as exc:
            summary["archived_pma_threads_error"] = (
                str(exc).strip() or exc.__class__.__name__
            )

        if delete_run:
            summary["deleted_run"] = bool(store.delete_flow_run(record.id))

        return summary


__all__ = [
    "archive_flow_run_artifacts",
    "flow_run_archive_root",
    "flow_run_artifacts_root",
    "_build_flow_archive_entries",
]
