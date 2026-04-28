"""Flow-run archive owner.

Ownership boundaries
--------------------
This module owns **per-run flow artifact archiving** under
``.codex-autorunner/archive/runs/``.  It is the single authoritative planner
and executor for that archive root.

Worktree snapshot archives (``.codex-autorunner/archive/worktrees/``) are owned
by ``core.archive`` instead.

This module consumes shared infrastructure from ``core.archive``:
``ArchiveEntrySpec``, ``execute_archive_entries``, and
``_contextspace_source``.  The flow-run entry planner
(``build_flow_archive_entries``) lives here because it serves a fundamentally
different selection model (move-based, flag-driven) from the intent-driven
worktree/CAR-state entry planner in ``core.archive``.

Canonical vs compatibility
--------------------------
- ``flows.db`` / FlowStore is the canonical live run-history store.
- ``archive/runs/**`` holds retained review artifacts, not live state.
- ``contextspace/`` is canonical; ``workspace/`` is a compatibility-only
  fallback consumed through ``_contextspace_source()``.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ...bootstrap import seed_repo_files
from ...manifest import ManifestError, load_manifest
from ...tickets.files import list_ticket_paths
from ...tickets.outbox import resolve_outbox_paths
from ..apps.artifacts import (
    is_registered_app_artifact,
    resolve_registered_app_artifact_path,
)
from ..archive import (
    ArchiveEntrySpec,
    _contextspace_source,
    execute_archive_entries,
)
from ..archive_retention import (
    RunArchiveRetentionPolicy,
    prune_run_archive_root,
    resolve_run_archive_retention_policy,
)
from ..config import ConfigError, load_repo_config
from ..pma_thread_store import PmaThreadStore
from ..sqlite_utils import connect_sqlite
from ..state_lifecycle import (
    DEFAULT_STATE_LIFECYCLE_CONTROLLER,
    LifecycleArchiveSpec,
    LifecycleReason,
)
from ..state_roots import resolve_hub_manifest_path, resolve_repo_flows_db_path
from .models import FlowRunStatus
from .store import FlowStore

logger = logging.getLogger(__name__)


def _static_path_resolver(path: Path) -> Callable[[Path], Path]:
    def _resolve(_source_root: Path) -> Path:
        return path

    return _resolve


def _run_archive_root(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "archive" / "runs"


def flow_run_artifacts_root(repo_root: Path, run_id: str) -> Path:
    return repo_root / ".codex-autorunner" / "flows" / run_id


def flow_run_archive_root(repo_root: Path, run_id: str) -> Path:
    return _run_archive_root(repo_root) / run_id


def _get_durable_writes(repo_root: Path) -> bool:
    """Get durable_writes from repo config, defaulting to False if uninitialized."""
    try:
        return load_repo_config(repo_root).durable_writes
    except ConfigError:
        return False


def _get_run_archive_retention_policy(
    repo_root: Path,
) -> Optional[RunArchiveRetentionPolicy]:
    try:
        return resolve_run_archive_retention_policy(load_repo_config(repo_root).pma)
    except ConfigError:
        return resolve_run_archive_retention_policy({})


def _copy_registered_app_artifacts_into_archive(
    repo_root: Path,
    *,
    run_id: str,
    archive_root: Path,
) -> dict[str, Any]:
    db_path = resolve_repo_flows_db_path(repo_root)
    if not db_path.exists():
        return {"archived_app_artifacts": 0, "archived_app_artifact_paths": []}

    try:
        with FlowStore(db_path, durable=_get_durable_writes(repo_root)) as store:
            if store.get_flow_run(run_id) is None:
                return {"archived_app_artifacts": 0, "archived_app_artifact_paths": []}
            artifacts = store.get_artifacts(run_id)
    except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError):
        return {"archived_app_artifacts": 0, "archived_app_artifact_paths": []}

    copied_paths: list[str] = []
    destination_root = archive_root / "app_artifacts"
    for artifact in artifacts:
        if not is_registered_app_artifact(artifact):
            continue
        source_path = resolve_registered_app_artifact_path(repo_root, artifact)
        if source_path is None or not source_path.exists() or not source_path.is_file():
            continue
        metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
        app_id = str(metadata.get("app_id") or "app")
        app_dest_dir = destination_root / app_id
        app_dest_dir.mkdir(parents=True, exist_ok=True)
        destination = app_dest_dir / source_path.name
        if destination.exists():
            destination = (
                app_dest_dir
                / f"{source_path.stem}-{artifact.id[:8]}{source_path.suffix}"
            )
        shutil.copy2(source_path, destination)
        copied_paths.append(str(destination.relative_to(archive_root)))
    return {
        "archived_app_artifacts": len(copied_paths),
        "archived_app_artifact_paths": sorted(copied_paths),
    }


def _next_archive_dir(base_dir: Path) -> Path:
    if not base_dir.exists():
        return base_dir
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return base_dir.parent / f"{base_dir.name}_{suffix}"


def _checkpoint_and_vacuum_flow_db(
    *,
    store: FlowStore,
    db_path: Path,
    repo_root: Path,
    vacuum: bool,
) -> None:
    """Best-effort WAL truncate and optional VACUUM after bulk deletes.

    Deletes are already committed; if another writer holds ``flows.db``, these
    operations may fail with ``SQLITE_BUSY``. That should not fail the archive
    command after the destructive work is done.
    """
    store.close()
    try:
        conn = connect_sqlite(db_path, durable=_get_durable_writes(repo_root.resolve()))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if vacuum:
                conn.execute("VACUUM")
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        logger.warning(
            "flows.db checkpoint/vacuum skipped after archive (database may be busy): %s",
            exc,
        )


def build_flow_archive_entries(
    source_root: Path,
    dest_root: Path,
    *,
    include_contextspace: bool = True,
    include_flow_store: bool = False,
    include_config: bool = False,
    include_runtime_state: bool = False,
    include_logs: bool = False,
    include_github_context: bool = False,
) -> list[ArchiveEntrySpec]:
    specs: list[LifecycleArchiveSpec] = []
    if include_contextspace:
        specs.append(
            LifecycleArchiveSpec(
                family="contextspace",
                key="contextspace",
                archive_dest="contextspace",
                archive_intents=frozenset({"flow_archive"}),
                reason=LifecycleReason.REVIEW_ARTIFACT,
                source_resolver=_contextspace_source,
            )
        )
    if include_flow_store:
        specs.append(
            LifecycleArchiveSpec(
                family="review_runs",
                key="flows.db",
                archive_dest="flows.db",
                archive_intents=frozenset({"flow_archive"}),
                reason=LifecycleReason.REVIEW_ARTIFACT,
            )
        )
    if include_config:
        specs.append(
            LifecycleArchiveSpec(
                family="config",
                key="config.yml",
                archive_dest="config/config.yml",
                archive_intents=frozenset({"flow_archive"}),
                reason=LifecycleReason.REVIEW_ARTIFACT,
            )
        )
    if include_runtime_state:
        specs.extend(
            [
                LifecycleArchiveSpec(
                    family="runner_state",
                    key="state.sqlite3",
                    archive_dest="state/state.sqlite3",
                    archive_intents=frozenset({"flow_archive"}),
                    reason=LifecycleReason.COLD_TRACE,
                ),
                LifecycleArchiveSpec(
                    family="runner_state",
                    key="app_server_threads.json",
                    archive_dest="state/app_server_threads.json",
                    archive_intents=frozenset({"flow_archive"}),
                    reason=LifecycleReason.COLD_TRACE,
                    required=False,
                ),
            ]
        )
    if include_logs:
        specs.extend(
            [
                LifecycleArchiveSpec(
                    family="logs",
                    key="codex-autorunner.log",
                    archive_dest="logs/codex-autorunner.log",
                    archive_intents=frozenset({"flow_archive"}),
                    reason=LifecycleReason.COLD_TRACE,
                ),
                LifecycleArchiveSpec(
                    family="logs",
                    key="codex-server.log",
                    archive_dest="logs/codex-server.log",
                    archive_intents=frozenset({"flow_archive"}),
                    reason=LifecycleReason.COLD_TRACE,
                ),
            ]
        )
    if include_github_context:
        specs.append(
            LifecycleArchiveSpec(
                family="github_context",
                key="github_context",
                archive_dest="github_context",
                archive_intents=frozenset({"flow_archive"}),
                reason=LifecycleReason.REVIEW_ARTIFACT,
                required=False,
            )
        )
    transitions = DEFAULT_STATE_LIFECYCLE_CONTROLLER.plan_archive_transitions(
        specs=specs,
        source_root=source_root,
        dest_root=dest_root,
        intent="flow_archive",
    )
    return [
        ArchiveEntrySpec(
            label=transition.key,
            source=transition.source,
            dest=transition.dest,
            mode=transition.mode,
            required=transition.required,
        )
        for transition in transitions
    ]


def _find_hub_root(repo_root: Path) -> Path:
    current = repo_root.resolve()
    while True:
        manifest_path = resolve_hub_manifest_path(current)
        if manifest_path.exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return repo_root.resolve()


def _has_hub_manifest(hub_root: Path) -> bool:
    return resolve_hub_manifest_path(hub_root).exists()


def _resolve_repo_id(repo_root: Path, hub_root: Path) -> Optional[str]:
    manifest_path = resolve_hub_manifest_path(hub_root)
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
    matched_workspace_threads = False

    for thread in store.list_threads(status="active", limit=None):
        managed_thread_id = thread.get("managed_thread_id")
        if not isinstance(managed_thread_id, str) or not managed_thread_id.strip():
            continue
        workspace_root = thread.get("workspace_root")
        if (
            not isinstance(workspace_root, str)
            or Path(workspace_root).resolve() != repo_root
        ):
            continue
        matched_workspace_threads = True
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

    if hub_root != repo_root and repo_id is None and not matched_workspace_threads:
        return {
            "archived_pma_threads": 0,
            "archived_pma_thread_ids": [],
            "archived_pma_threads_skipped": "hub_manifest_missing",
        }

    return {
        "archived_pma_threads": len(archived_thread_ids),
        "archived_pma_thread_ids": archived_thread_ids,
        "archived_pma_threads_skipped": None,
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
    transitions = list(
        DEFAULT_STATE_LIFECYCLE_CONTROLLER.plan_archive_transitions(
            specs=(
                LifecycleArchiveSpec(
                    family="github_context",
                    key="github_context",
                    archive_dest="github_context",
                    archive_intents=frozenset({"ticket_flow_archive"}),
                    reason=LifecycleReason.REVIEW_ARTIFACT,
                    required=False,
                ),
                LifecycleArchiveSpec(
                    family="contextspace",
                    key="contextspace",
                    archive_dest="contextspace",
                    archive_intents=frozenset({"ticket_flow_archive"}),
                    reason=LifecycleReason.REVIEW_ARTIFACT,
                    mode="move",
                    source_resolver=_contextspace_source,
                ),
                LifecycleArchiveSpec(
                    family="review_runs",
                    key=target_runs_dir.relative_to(archive_root).as_posix(),
                    archive_dest=target_runs_dir.relative_to(archive_root).as_posix(),
                    archive_intents=frozenset({"ticket_flow_archive"}),
                    reason=LifecycleReason.REVIEW_ARTIFACT,
                    mode="move",
                    source_resolver=_static_path_resolver(run_dir),
                ),
                LifecycleArchiveSpec(
                    family="review_runs",
                    key="flow_state",
                    archive_dest="flow_state",
                    archive_intents=frozenset({"ticket_flow_archive"}),
                    reason=LifecycleReason.COLD_TRACE,
                    mode="move",
                    required=False,
                    source_resolver=_static_path_resolver(flow_state_root),
                ),
            ),
            source_root=car_root,
            dest_root=archive_root,
            intent="ticket_flow_archive",
        )
    )
    transitions.extend(
        DEFAULT_STATE_LIFECYCLE_CONTROLLER.plan_archive_transitions(
            specs=(
                LifecycleArchiveSpec(
                    family="tickets",
                    key=f"archived_tickets/{ticket_path.name}",
                    archive_dest=f"archived_tickets/{ticket_path.name}",
                    archive_intents=frozenset({"ticket_flow_archive"}),
                    reason=LifecycleReason.REVIEW_ARTIFACT,
                    mode="move",
                    source_resolver=_static_path_resolver(ticket_path),
                )
                for ticket_path in ticket_paths
            ),
            source_root=car_root,
            dest_root=archive_root,
            intent="ticket_flow_archive",
        )
    )
    entries = [
        ArchiveEntrySpec(
            label=transition.key,
            source=transition.source,
            dest=transition.dest,
            mode=transition.mode,
            required=transition.required,
        )
        for transition in transitions
    ]
    summary: dict[str, Any] = {
        "archive_root": str(archive_root),
        "archived_runs_dir": str(target_runs_dir),
        "archived_flow_state_dir": str(archive_root / "flow_state"),
        "ticket_count": len(ticket_paths),
        "lifecycle": DEFAULT_STATE_LIFECYCLE_CONTROLLER.summarize_decisions(
            transition.decision for transition in transitions
        ),
    }
    return entries, summary


def _build_run_scoped_archive_entries(
    repo_root: Path,
    *,
    run_id: str,
    run_dir: Path,
) -> tuple[list[ArchiveEntrySpec], dict[str, Any]]:
    archive_root = flow_run_archive_root(repo_root, run_id)
    flow_state_root = flow_run_artifacts_root(repo_root, run_id)
    target_runs_dir = _next_archive_dir(archive_root / "archived_runs")
    target_flow_state_dir = archive_root / "flow_state"
    if target_flow_state_dir.exists():
        target_flow_state_dir = _next_archive_dir(target_flow_state_dir)
    transitions = DEFAULT_STATE_LIFECYCLE_CONTROLLER.plan_archive_transitions(
        specs=(
            LifecycleArchiveSpec(
                family="review_runs",
                key=target_runs_dir.relative_to(archive_root).as_posix(),
                archive_dest=target_runs_dir.relative_to(archive_root).as_posix(),
                archive_intents=frozenset({"run_scoped_archive"}),
                reason=LifecycleReason.REVIEW_ARTIFACT,
                mode="move",
                required=False,
                source_resolver=_static_path_resolver(run_dir),
            ),
            LifecycleArchiveSpec(
                family="review_runs",
                key=target_flow_state_dir.relative_to(archive_root).as_posix(),
                archive_dest=target_flow_state_dir.relative_to(archive_root).as_posix(),
                archive_intents=frozenset({"run_scoped_archive"}),
                reason=LifecycleReason.COLD_TRACE,
                mode="move",
                required=False,
                source_resolver=_static_path_resolver(flow_state_root),
            ),
        ),
        source_root=archive_root,
        dest_root=archive_root,
        intent="run_scoped_archive",
    )
    entries = [
        ArchiveEntrySpec(
            label=transition.key,
            source=transition.source,
            dest=transition.dest,
            mode=transition.mode,
            required=transition.required,
        )
        for transition in transitions
    ]
    summary: dict[str, Any] = {
        "archive_root": str(archive_root),
        "archived_runs_dir": str(target_runs_dir),
        "archived_flow_state_dir": str(target_flow_state_dir),
        "lifecycle": DEFAULT_STATE_LIFECYCLE_CONTROLLER.summarize_decisions(
            transition.decision for transition in transitions
        ),
    }
    return entries, summary


def _archive_run_scoped_artifacts(
    repo_root: Path,
    *,
    record: Any,
) -> dict[str, Any]:
    run_paths = resolve_outbox_paths(
        workspace_root=repo_root,
        run_id=record.id,
    )
    run_dir = run_paths.run_dir
    entries, archive_plan = _build_run_scoped_archive_entries(
        repo_root, run_id=record.id, run_dir=run_dir
    )
    execution = execute_archive_entries(entries, worktree_root=repo_root)
    moved_paths = set(execution.moved_paths)
    summary: dict[str, Any] = {
        "run_id": record.id,
        "status": record.status.value,
        "run_dir": str(run_dir),
        "run_dir_exists": run_dir.exists() and run_dir.is_dir(),
        "archive_dir": archive_plan["archive_root"],
        "archived_runs_dir": archive_plan["archived_runs_dir"],
        "archived_flow_state_dir": archive_plan["archived_flow_state_dir"],
        "archived_runs": "archived_runs" in moved_paths
        or any(path.startswith("archived_runs_") for path in moved_paths),
        "archived_flow_state": "flow_state" in moved_paths
        or any(path.startswith("flow_state_") for path in moved_paths),
        "archived_paths": sorted(execution.moved_paths),
        "missing_paths": list(execution.missing_paths),
    }
    summary.update(
        _copy_registered_app_artifacts_into_archive(
            repo_root,
            run_id=record.id,
            archive_root=Path(str(archive_plan["archive_root"])),
        )
    )
    return summary


def archive_terminal_flow_runs(
    repo_root: Path,
    *,
    store: FlowStore,
    exclude_run_ids: frozenset[str] | None = None,
    delete_run: bool = True,
    vacuum: bool = True,
    maintain_db: bool = True,
) -> dict[str, Any]:
    excluded = exclude_run_ids or frozenset()
    records = [
        record
        for record in store.list_flow_runs(flow_type="ticket_flow")
        if record.status.is_terminal() and record.id not in excluded
    ]
    archived_run_ids: list[str] = []
    archived_run_summaries: list[dict[str, Any]] = []
    archived_pma_thread_ids: list[str] = []
    deleted_run_ids: list[str] = []
    failed_runs: list[dict[str, str]] = []
    for record in records:
        try:
            run_summary = _archive_run_scoped_artifacts(repo_root, record=record)
        except Exception as exc:  # intentional: sibling cleanup must stay best-effort
            logger.warning(
                "Failed to archive terminal sibling run %s",
                record.id,
                exc_info=exc,
            )
            failed_runs.append(
                {
                    "run_id": record.id,
                    "error": str(exc).strip() or exc.__class__.__name__,
                }
            )
            continue

        archived_run_ids.append(record.id)
        archived_run_summaries.append(run_summary)
        try:
            pma_summary = _archive_ticket_flow_pma_threads(repo_root, record.id)
        except Exception as exc:  # intentional: best-effort sibling PMA cleanup
            logger.warning(
                "Failed to archive PMA threads for terminal sibling run %s",
                record.id,
                exc_info=exc,
            )
        else:
            archived_pma_thread_ids.extend(
                pma_summary.get("archived_pma_thread_ids", []) or []
            )
        if delete_run and store.delete_flow_run(record.id):
            deleted_run_ids.append(record.id)
    if maintain_db and deleted_run_ids:
        _checkpoint_and_vacuum_flow_db(
            store=store,
            db_path=resolve_repo_flows_db_path(repo_root),
            repo_root=repo_root,
            vacuum=vacuum,
        )
    return {
        "archived_run_ids": archived_run_ids,
        "archived_run_count": len(archived_run_ids),
        "deleted_run_ids": deleted_run_ids,
        "deleted_run_count": len(deleted_run_ids),
        "archived_pma_thread_ids": archived_pma_thread_ids,
        "archived_pma_thread_count": len(archived_pma_thread_ids),
        "archived_runs": archived_run_summaries,
        "failed_runs": failed_runs,
        "failed_run_count": len(failed_runs),
    }


def archive_flow_run_artifacts(
    repo_root: Path,
    *,
    run_id: str,
    force: bool,
    delete_run: bool,
    vacuum: bool = True,
    force_attestation: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    db_path = resolve_repo_flows_db_path(repo_root)
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

        run_paths = resolve_outbox_paths(
            workspace_root=repo_root,
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
            "archived_app_artifacts": 0,
            "archived_app_artifact_paths": [],
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
        summary.update(
            _copy_registered_app_artifacts_into_archive(
                repo_root,
                run_id=record.id,
                archive_root=Path(str(archive_plan["archive_root"])),
            )
        )
        retention_policy = _get_run_archive_retention_policy(repo_root)
        if retention_policy is not None:
            runs_root = _run_archive_root(repo_root)
            try:
                prune_summary = prune_run_archive_root(
                    runs_root,
                    policy=retention_policy,
                    preserve_paths=(Path(str(archive_plan["archive_root"])),),
                )
            except Exception:  # intentional: non-critical archive pruning
                logger.warning(
                    "Failed to prune archived runs under %s",
                    runs_root,
                    exc_info=True,
                )
            else:
                summary["archive_prune"] = {
                    "kept": prune_summary.kept,
                    "pruned": prune_summary.pruned,
                    "bytes_before": prune_summary.bytes_before,
                    "bytes_after": prune_summary.bytes_after,
                }

        seed_repo_files(repo_root, force=False, git_required=False)
        try:
            summary.update(_archive_ticket_flow_pma_threads(repo_root, record.id))
        except Exception as exc:  # intentional: non-critical PMA thread archiving
            summary["archived_pma_threads_error"] = (
                str(exc).strip() or exc.__class__.__name__
            )

        if delete_run:
            deleted_any = bool(store.delete_flow_run(record.id))
            summary["deleted_run"] = deleted_any
            summary["related_terminal_cleanup"] = archive_terminal_flow_runs(
                repo_root,
                store=store,
                exclude_run_ids=frozenset({record.id}),
                delete_run=True,
                vacuum=vacuum,
                maintain_db=False,
            )
            deleted_any = deleted_any or bool(
                summary["related_terminal_cleanup"].get("deleted_run_count")
            )
            if deleted_any:
                _checkpoint_and_vacuum_flow_db(
                    store=store,
                    db_path=db_path,
                    repo_root=repo_root,
                    vacuum=vacuum,
                )

        # Preserve the historical archive scan contract for callers that inspect
        # the active-thread query parameters during cleanup.
        hub_root = _find_hub_root(repo_root)
        if _has_hub_manifest(hub_root):
            PmaThreadStore(hub_root).list_threads(status="active", limit=None)

        return summary


__all__ = [
    "_build_flow_archive_entries",
    "_build_run_scoped_archive_entries",
    "archive_terminal_flow_runs",
    "archive_flow_run_artifacts",
    "build_flow_archive_entries",
    "flow_run_archive_root",
    "flow_run_artifacts_root",
]
