"""Hub run-related CLI commands extracted from the monolithic CLI surface."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from ....core.config import HubConfig, load_repo_config
from ....core.flows import FlowStore
from ....core.flows.models import FlowRunRecord, FlowRunStatus
from ....manifest import load_manifest
from ....tickets.outbox import resolve_outbox_paths
from .utils import parse_bool_text, parse_duration


def _resolve_run_paths(record: FlowRunRecord, repo_root: Path):
    workspace_root = Path(record.input_data.get("workspace_root") or repo_root)
    runs_dir = Path(record.input_data.get("runs_dir") or ".codex-autorunner/runs")
    return resolve_outbox_paths(
        workspace_root=workspace_root, runs_dir=runs_dir, run_id=record.id
    )


def _flow_timestamp(record: FlowRunRecord) -> Optional[datetime]:
    candidate = record.finished_at or record.started_at or record.created_at
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cleanup_stale_flow_runs(
    *,
    repo_root: Path,
    exclude_run_id: str,
    older_than: Optional[str] = None,
    delete_run: str = "true",
    parse_bool_text_func: Callable[[str, str], bool] = parse_bool_text,
    parse_duration_func: Callable[[str], Any] = parse_duration,
    load_repo_config_func: Callable[[Path], object] = load_repo_config,
) -> int:
    """Clean up stale terminal runs for a repo, excluding the specified run."""
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return 0

    cutoff = None
    if older_than:
        cutoff = datetime.now(timezone.utc) - parse_duration_func(older_than)
    parsed_delete_run = parse_bool_text_func(delete_run, flag="--delete-run")

    config = load_repo_config_func(repo_root)
    store = FlowStore(db_path, durable=config.durable_writes)
    try:
        store.initialize()
        records = store.list_flow_runs(flow_type="ticket_flow")
    except Exception:
        store.close()
        return 0

    stale_statuses = {
        FlowRunStatus.COMPLETED,
        FlowRunStatus.FAILED,
        FlowRunStatus.STOPPED,
    }
    cleanup_count = 0

    try:
        for record in records:
            if str(record.id) == exclude_run_id:
                continue
            if record.status not in stale_statuses:
                continue
            if cutoff is not None:
                ts = _flow_timestamp(record)
                if ts is None or ts > cutoff:
                    continue
            try:
                _archive_flow_run_artifacts(
                    repo_root=repo_root,
                    store=store,
                    record=record,
                    force=False,
                    delete_run=parsed_delete_run,
                    dry_run=False,
                )
                cleanup_count += 1
            except Exception:
                continue
    finally:
        store.close()

    return cleanup_count


def _archive_flow_run_artifacts(
    *,
    repo_root: Path,
    store: FlowStore,
    record: FlowRunRecord,
    force: bool,
    delete_run: bool,
    dry_run: bool,
) -> dict[str, Any]:
    status = record.status
    terminal = status.is_terminal()
    if not terminal and not (
        force and status in {FlowRunStatus.PAUSED, FlowRunStatus.STOPPING}
    ):
        raise ValueError(
            "Can only archive completed/stopped/failed runs (use --force for paused/stopping)."
        )

    artifacts_root = repo_root / ".codex-autorunner" / "flows"
    run_paths = _resolve_run_paths(record, repo_root)
    run_dir = run_paths.run_dir
    archive_root = artifacts_root / record.id
    archived_runs_dir = archive_root / "archived_runs"
    target_runs_dir = archived_runs_dir
    if target_runs_dir.exists():
        suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target_runs_dir = archive_root / f"archived_runs_{suffix}"

    summary = {
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

    if dry_run:
        return summary

    import shutil

    if run_dir.exists() and run_dir.is_dir():
        target_runs_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(run_dir), str(target_runs_dir))
        summary["archived_runs"] = True

    if delete_run:
        summary["deleted_run"] = bool(store.delete_flow_run(record.id))

    return summary


def register_hub_runs_commands(
    hub_runs_app: typer.Typer,
    *,
    require_hub_config: Callable[[Optional[Path]], HubConfig],
    load_repo_config: Callable[[Path], object],
    parse_bool_text_func: Callable[[str, str], bool] = parse_bool_text,
    parse_duration_func: Callable[[str], Any] = parse_duration,
) -> None:
    @hub_runs_app.command("cleanup")
    def hub_runs_cleanup(
        stale: bool = typer.Option(
            False, "--stale", help="Cleanup stale terminal runs"
        ),
        older_than: Optional[str] = typer.Option(
            None, "--older-than", help="Age threshold like 30m, 12h, 7d"
        ),
        dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
        delete_run: str = typer.Option(
            "true",
            "--delete-run",
            help="Delete flow run record after archive (true|false)",
        ),
        force: bool = typer.Option(
            False, "--force", help="Allow archiving paused/stopping runs"
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", "--hub", help="Hub root path"
        ),
        output_json: bool = typer.Option(
            True, "--json/--no-json", help="Emit JSON output (default: true)"
        ),
        pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output"),
    ):
        """Archive stale run artifacts across hub repos and optionally delete run records."""
        if not stale:
            typer.echo("Pass --stale to confirm batch cleanup intent.", err=True)
            raise typer.Exit(code=1)

        config = require_hub_config(path)
        manifest = load_manifest(config.manifest_path, config.root)
        cutoff = None
        if older_than:
            cutoff = datetime.now(timezone.utc) - parse_duration_func(older_than)
        parsed_delete_run = parse_bool_text_func(delete_run, flag="--delete-run")

        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        stale_statuses = {
            FlowRunStatus.COMPLETED,
            FlowRunStatus.FAILED,
            FlowRunStatus.STOPPED,
        }

        for entry in manifest.repos:
            repo_root = (config.root / entry.path).resolve()
            db_path = repo_root / ".codex-autorunner" / "flows.db"
            if not db_path.exists():
                continue
            try:
                repo_config = load_repo_config(repo_root)
                store = FlowStore(db_path, durable=repo_config.durable_writes)
                store.initialize()
                records = store.list_flow_runs(flow_type="ticket_flow")
            except Exception as exc:
                errors.append(
                    {
                        "repo_id": entry.id,
                        "run_id": None,
                        "error": f"store_open_failed: {exc}",
                    }
                )
                continue

            try:
                for record in records:
                    if record.status in {FlowRunStatus.RUNNING, FlowRunStatus.PENDING}:
                        continue
                    if record.status not in stale_statuses and not (
                        force
                        and record.status
                        in {FlowRunStatus.PAUSED, FlowRunStatus.STOPPING}
                    ):
                        continue
                    if cutoff is not None:
                        ts = _flow_timestamp(record)
                        if ts is None or ts > cutoff:
                            continue
                    try:
                        summary = _archive_flow_run_artifacts(
                            repo_root=repo_root,
                            store=store,
                            record=record,
                            force=force,
                            delete_run=parsed_delete_run,
                            dry_run=dry_run,
                        )
                        summary["repo_id"] = entry.id
                        results.append(summary)
                    except Exception as exc:
                        errors.append(
                            {
                                "repo_id": entry.id,
                                "run_id": record.id,
                                "status": record.status.value,
                                "error": str(exc),
                            }
                        )
            finally:
                store.close()

        payload = {
            "dry_run": dry_run,
            "stale": stale,
            "older_than": older_than,
            "delete_run": parsed_delete_run,
            "force": force,
            "results": results,
            "errors": errors,
        }

        if output_json:
            typer.echo(json.dumps(payload, indent=2 if pretty else None))
            if errors:
                raise typer.Exit(code=1)
            return

        typer.echo(
            f"Hub runs cleanup candidates={len(results)} errors={len(errors)} dry_run={dry_run}"
        )
        if errors:
            typer.echo("hub runs cleanup encountered errors.", err=True)
            raise typer.Exit(code=1)
