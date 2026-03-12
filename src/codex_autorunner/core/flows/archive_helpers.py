from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

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


def _build_flow_archive_entries(
    repo_root: Path,
    *,
    run_id: str,
    run_dir: Path,
) -> tuple[list[ArchiveEntrySpec], dict[str, Any]]:
    archive_root = repo_root / ".codex-autorunner" / "flows" / run_id
    target_runs_dir = _next_archive_dir(archive_root / "archived_runs")
    ticket_paths = list(list_ticket_paths(repo_root / ".codex-autorunner" / "tickets"))
    entries = build_common_car_archive_entries(
        repo_root / ".codex-autorunner", archive_root
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
    summary: dict[str, Any] = {
        "archive_root": str(archive_root),
        "archived_runs_dir": str(target_runs_dir),
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
            "delete_run": delete_run,
            "deleted_run": False,
            "archived_tickets": 0,
            "archived_runs": False,
            "archived_contextspace": False,
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
        summary["archived_contextspace"] = "contextspace" in copied_paths
        summary["archived_paths"] = sorted(
            list(execution.copied_paths) + list(execution.moved_paths)
        )
        summary["missing_paths"] = list(execution.missing_paths)

        if delete_run:
            summary["deleted_run"] = bool(store.delete_flow_run(record.id))

        return summary


__all__ = ["archive_flow_run_artifacts", "_build_flow_archive_entries"]
