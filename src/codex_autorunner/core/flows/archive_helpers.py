from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...tickets.outbox import resolve_outbox_paths
from ..config import load_repo_config
from .models import FlowRunStatus
from .store import FlowStore


def archive_flow_run_artifacts(
    repo_root: Path,
    *,
    run_id: str,
    force: bool,
    delete_run: bool,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        raise ValueError("Flow database not found.")

    config = load_repo_config(repo_root)
    with FlowStore(db_path, durable=config.durable_writes) as store:
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

        artifacts_root = repo_root / ".codex-autorunner" / "flows"
        archive_root = artifacts_root / record.id
        target_runs_dir = archive_root / "archived_runs"
        if target_runs_dir.exists():
            suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target_runs_dir = archive_root / f"archived_runs_{suffix}"

        summary: dict[str, Any] = {
            "repo_root": str(repo_root),
            "run_id": record.id,
            "status": record.status.value,
            "run_dir": str(run_dir),
            "run_dir_exists": run_dir.exists() and run_dir.is_dir(),
            "archive_dir": str(target_runs_dir),
            "delete_run": delete_run,
            "deleted_run": False,
            "archived_runs": False,
        }

        if run_dir.exists() and run_dir.is_dir():
            import shutil

            target_runs_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(run_dir), str(target_runs_dir))
            summary["archived_runs"] = True

        if delete_run:
            summary["deleted_run"] = bool(store.delete_flow_run(record.id))

        return summary


__all__ = ["archive_flow_run_artifacts"]
