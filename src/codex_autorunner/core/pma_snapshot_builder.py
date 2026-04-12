from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .filebox import BOXES, empty_listing, list_filebox
from .flows.models import flow_run_duration_seconds
from .freshness import (
    build_freshness_payload,
    iso_now,
    resolve_stale_threshold_seconds,
    summarize_section_freshness,
)
from .hub import HubSupervisor
from .pma_action_queue import build_pma_action_queue
from .pma_automation_snapshot import empty_automation_snapshot, snapshot_pma_automation
from .pma_context_shared import (
    PMA_MAX_MESSAGES,
    PMA_MAX_REPOS,
    PMA_MAX_TEMPLATE_FIELD_CHARS,
    PMA_MAX_TEMPLATE_REPOS,
    PMA_MAX_TEXT,
    _truncate,
)
from .pma_file_inbox import (
    PMA_FILE_NEXT_ACTION_PROCESS,
    _extract_entry_freshness,
    enrich_pma_file_inbox_entry,
)
from .pma_thread_snapshot import snapshot_pma_threads
from .pma_ticket_flow_state import get_latest_ticket_flow_run_state_with_record
from .state_roots import resolve_hub_templates_root
from .ticket_flow_projection import build_canonical_state_v1
from .ticket_flow_summary import build_ticket_flow_summary

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PmaSnapshotLimits:
    max_repos: int = PMA_MAX_REPOS
    max_messages: int = PMA_MAX_MESSAGES
    max_text_chars: int = PMA_MAX_TEXT

    @classmethod
    def from_supervisor(
        cls, supervisor: Optional[HubSupervisor]
    ) -> "PmaSnapshotLimits":
        pma_config = supervisor.hub_config.pma if supervisor else None
        return cls(
            max_repos=(
                pma_config.max_repos
                if pma_config and pma_config.max_repos > 0
                else PMA_MAX_REPOS
            ),
            max_messages=(
                pma_config.max_messages
                if pma_config and pma_config.max_messages > 0
                else PMA_MAX_MESSAGES
            ),
            max_text_chars=(
                pma_config.max_text_chars
                if pma_config and pma_config.max_text_chars > 0
                else PMA_MAX_TEXT
            ),
        )


def _load_template_scan_summary(
    hub_root: Optional[Path],
    *,
    max_field_chars: int = PMA_MAX_TEMPLATE_FIELD_CHARS,
) -> Optional[dict[str, Any]]:
    if hub_root is None:
        return None
    try:
        scans_root = resolve_hub_templates_root(hub_root) / "scans"
        if not scans_root.exists():
            return None
        candidates = [
            entry
            for entry in scans_root.iterdir()
            if entry.is_file() and entry.suffix == ".json"
        ]
        if not candidates:
            return None
        newest = max(candidates, key=lambda entry: entry.stat().st_mtime)
        payload = json.loads(newest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return {
            "repo_id": _truncate(str(payload.get("repo_id", "")), max_field_chars),
            "decision": _truncate(str(payload.get("decision", "")), max_field_chars),
            "severity": _truncate(str(payload.get("severity", "")), max_field_chars),
            "scanned_at": _truncate(
                str(payload.get("scanned_at", "")), max_field_chars
            ),
        }
    except (OSError, ValueError, TypeError, KeyError) as exc:
        _logger.warning("Could not load template scan summary: %s", exc)
        return None


def _snapshot_pma_files(
    hub_root: Path,
) -> tuple[dict[str, list[str]], dict[str, list[dict[str, Any]]]]:
    pma_files: dict[str, list[str]] = {box: [] for box in BOXES}
    pma_files_detail: dict[str, list[dict[str, Any]]] = empty_listing()
    try:
        filebox = list_filebox(hub_root)
        for box in BOXES:
            entries = filebox.get(box) or []
            pma_files[box] = sorted([entry.name for entry in entries])
            pma_files_detail[box] = [
                (
                    enrich_pma_file_inbox_entry(
                        {
                            "item_type": "pma_file",
                            "next_action": PMA_FILE_NEXT_ACTION_PROCESS,
                            "box": box,
                            "name": entry.name,
                            "source": entry.source or "filebox",
                            "size": str(entry.size) if entry.size is not None else "",
                            "modified_at": entry.modified_at or "",
                        }
                    )
                    if box == "inbox"
                    else {
                        "item_type": "pma_file",
                        "box": box,
                        "name": entry.name,
                        "source": entry.source or "filebox",
                        "size": str(entry.size) if entry.size is not None else "",
                        "modified_at": entry.modified_at or "",
                    }
                )
                for entry in entries
            ]
    except (OSError, KeyError, TypeError, RuntimeError) as exc:
        _logger.warning("Could not list filebox contents: %s", exc)
    return pma_files, pma_files_detail


def annotate_pma_files_detail(
    pma_files_detail: dict[str, list[dict[str, Any]]],
    *,
    generated_at: str,
    stale_threshold_seconds: int,
) -> dict[str, list[dict[str, Any]]]:
    annotated: dict[str, list[dict[str, Any]]] = {}
    for box in BOXES:
        entries = pma_files_detail.get(box) or []
        annotated_entries: list[dict[str, Any]] = []
        for entry in entries:
            payload = dict(entry)
            payload["freshness"] = build_freshness_payload(
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                candidates=[("file_modified_at", entry.get("modified_at"))],
            )
            if box == "inbox":
                payload = enrich_pma_file_inbox_entry(payload)
            annotated_entries.append(payload)
        annotated[box] = annotated_entries
    return annotated


def _build_templates_snapshot(
    supervisor: HubSupervisor,
    *,
    hub_root: Optional[Path] = None,
    max_repos: int = PMA_MAX_TEMPLATE_REPOS,
    max_field_chars: int = PMA_MAX_TEMPLATE_FIELD_CHARS,
) -> dict[str, Any]:
    hub_config = getattr(supervisor, "hub_config", None)
    templates_cfg = getattr(hub_config, "templates", None)
    if templates_cfg is None:
        return {"enabled": False, "repos": []}
    repos = []
    for repo in templates_cfg.repos[: max(0, max_repos)]:
        repos.append(
            {
                "id": _truncate(repo.id, max_field_chars),
                "default_ref": _truncate(repo.default_ref, max_field_chars),
                "trusted": bool(repo.trusted),
            }
        )
    payload: dict[str, Any] = {
        "enabled": bool(templates_cfg.enabled),
        "repos": repos,
    }
    scan_summary = _load_template_scan_summary(
        hub_root, max_field_chars=max_field_chars
    )
    if scan_summary:
        payload["last_scan"] = scan_summary
    return payload


def _resolve_pma_freshness_threshold_seconds(
    supervisor: Optional[HubSupervisor],
) -> int:
    pma_config = getattr(getattr(supervisor, "hub_config", None), "pma", None)
    return resolve_stale_threshold_seconds(
        getattr(pma_config, "freshness_stale_threshold_seconds", None)
    )


def _build_snapshot_freshness_summary(
    *,
    generated_at: str,
    stale_threshold_seconds: int,
    repos: list[dict[str, Any]],
    agent_workspaces: list[dict[str, Any]],
    inbox: list[dict[str, Any]],
    action_queue: list[dict[str, Any]],
    pma_threads: list[dict[str, Any]],
    pma_files_detail: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "stale_threshold_seconds": stale_threshold_seconds,
        "sections": {
            "repos": summarize_section_freshness(
                repos,
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                extractor=_extract_entry_freshness,
            ),
            "agent_workspaces": summarize_section_freshness(
                agent_workspaces,
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
            ),
            "inbox": summarize_section_freshness(
                inbox,
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                extractor=_extract_entry_freshness,
            ),
            "action_queue": summarize_section_freshness(
                action_queue,
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                extractor=_extract_entry_freshness,
            ),
            "pma_threads": summarize_section_freshness(
                pma_threads,
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
            ),
            "pma_file_inbox": summarize_section_freshness(
                pma_files_detail.get("inbox") or [],
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
            ),
            "pma_file_outbox": summarize_section_freshness(
                pma_files_detail.get("outbox") or [],
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
            ),
        },
    }


def _gather_lifecycle_events(
    supervisor: HubSupervisor, limit: int = 20
) -> list[dict[str, Any]]:
    events = supervisor.lifecycle_store.get_unprocessed(limit=limit)
    result: list[dict[str, Any]] = []
    for event in events[:limit]:
        result.append(
            {
                "event_type": event.event_type.value,
                "repo_id": event.repo_id,
                "run_id": event.run_id,
                "timestamp": event.timestamp,
                "data": event.data,
            }
        )
    return result


def _build_repo_summaries(
    supervisor: HubSupervisor,
    *,
    stale_threshold_seconds: int,
    limits: PmaSnapshotLimits,
) -> list[dict[str, Any]]:
    repo_snapshots = sorted(supervisor.list_repos(), key=lambda snap: snap.id)
    repos: list[dict[str, Any]] = []
    for snap in repo_snapshots[: limits.max_repos]:
        effective_destination = (
            dict(snap.effective_destination)
            if isinstance(snap.effective_destination, dict)
            else {"kind": "local"}
        )
        summary: dict[str, Any] = {
            "id": snap.id,
            "display_name": snap.display_name,
            "status": snap.status.value,
            "last_run_id": snap.last_run_id,
            "last_run_started_at": snap.last_run_started_at,
            "last_run_finished_at": snap.last_run_finished_at,
            "last_run_duration_seconds": None,
            "last_exit_code": snap.last_exit_code,
            "effective_destination": effective_destination,
            "ticket_flow": None,
            "run_state": None,
            "canonical_state_v1": None,
        }
        if snap.initialized and snap.exists_on_disk:
            summary["ticket_flow"] = build_ticket_flow_summary(
                snap.path, include_failure=False
            )
            run_state, run_record = get_latest_ticket_flow_run_state_with_record(
                snap.path, snap.id
            )
            summary["run_state"] = run_state
            if run_record is not None:
                if str(summary.get("last_run_id")) != str(run_record.id):
                    summary["last_exit_code"] = None
                summary["last_run_id"] = run_record.id
                summary["last_run_started_at"] = run_record.started_at
                summary["last_run_finished_at"] = run_record.finished_at
                summary["last_run_duration_seconds"] = flow_run_duration_seconds(
                    run_record
                )
            summary["canonical_state_v1"] = build_canonical_state_v1(
                repo_root=snap.path,
                repo_id=snap.id,
                run_state=summary["run_state"],
                record=run_record,
                preferred_run_id=(
                    str(snap.last_run_id) if snap.last_run_id is not None else None
                ),
                stale_threshold_seconds=stale_threshold_seconds,
            )
        repos.append(summary)
    return repos


def _build_agent_workspace_summaries(
    supervisor: HubSupervisor,
    *,
    hub_root: Optional[Path],
    limits: PmaSnapshotLimits,
) -> list[dict[str, Any]]:
    list_agent_workspaces = getattr(supervisor, "list_agent_workspaces", None)
    if not callable(list_agent_workspaces):
        return []
    agent_workspace_snapshots = sorted(
        list_agent_workspaces(), key=lambda snap: snap.id
    )
    agent_workspaces: list[dict[str, Any]] = []
    for workspace in agent_workspace_snapshots[: limits.max_repos]:
        if hub_root is not None:
            summary = workspace.to_dict(hub_root)
        else:
            summary = {
                "id": workspace.id,
                "runtime": workspace.runtime,
                "path": str(workspace.path),
                "display_name": workspace.display_name,
                "enabled": workspace.enabled,
                "exists_on_disk": workspace.exists_on_disk,
                "effective_destination": workspace.effective_destination,
                "resource_kind": workspace.resource_kind,
            }
        agent_workspaces.append(summary)
    return agent_workspaces


def _collect_hub_local_artifacts(
    *,
    hub_root: Optional[Path],
    generated_at: str,
    stale_threshold_seconds: int,
    supervisor: HubSupervisor,
) -> tuple[
    dict[str, list[str]],
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    pma_files: dict[str, list[str]] = {box: [] for box in BOXES}
    pma_files_detail: dict[str, list[dict[str, Any]]] = empty_listing()
    pma_threads: list[dict[str, Any]] = []
    automation = snapshot_pma_automation(supervisor)
    if hub_root is None:
        return pma_files, pma_files_detail, pma_threads, automation

    pma_files, raw_files_detail = _snapshot_pma_files(hub_root)
    pma_files_detail = annotate_pma_files_detail(
        raw_files_detail,
        generated_at=generated_at,
        stale_threshold_seconds=stale_threshold_seconds,
    )
    pma_threads = snapshot_pma_threads(
        hub_root,
        generated_at=generated_at,
        stale_threshold_seconds=stale_threshold_seconds,
    )
    return pma_files, pma_files_detail, pma_threads, automation


async def build_hub_snapshot_payload(
    supervisor: Optional[HubSupervisor],
    hub_root: Optional[Path] = None,
    *,
    gather_inbox: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    generated_at = iso_now()
    stale_threshold_seconds = _resolve_pma_freshness_threshold_seconds(supervisor)
    if supervisor is None:
        empty_files = empty_listing()
        return {
            "generated_at": generated_at,
            "repos": [],
            "agent_workspaces": [],
            "inbox": [],
            "action_queue": [],
            "templates": {"enabled": False, "repos": []},
            "lifecycle_events": [],
            "pma_files_detail": empty_files,
            "pma_threads": [],
            "automation": empty_automation_snapshot(),
            "freshness": _build_snapshot_freshness_summary(
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                repos=[],
                agent_workspaces=[],
                inbox=[],
                action_queue=[],
                pma_threads=[],
                pma_files_detail=empty_files,
            ),
        }

    limits = PmaSnapshotLimits.from_supervisor(supervisor)
    repos, agent_workspaces, inbox, lifecycle_events = await asyncio.gather(
        asyncio.to_thread(
            _build_repo_summaries,
            supervisor,
            stale_threshold_seconds=stale_threshold_seconds,
            limits=limits,
        ),
        asyncio.to_thread(
            _build_agent_workspace_summaries,
            supervisor,
            hub_root=hub_root,
            limits=limits,
        ),
        asyncio.to_thread(
            gather_inbox,
            supervisor,
            max_text_chars=limits.max_text_chars,
            stale_threshold_seconds=stale_threshold_seconds,
        ),
        asyncio.to_thread(_gather_lifecycle_events, supervisor, limit=20),
    )
    inbox = inbox[: limits.max_messages]

    templates = _build_templates_snapshot(supervisor, hub_root=hub_root)
    pma_files, pma_files_detail, pma_threads, automation = await asyncio.to_thread(
        _collect_hub_local_artifacts,
        hub_root=hub_root,
        generated_at=generated_at,
        stale_threshold_seconds=stale_threshold_seconds,
        supervisor=supervisor,
    )
    action_queue = build_pma_action_queue(
        inbox=inbox,
        pma_threads=pma_threads,
        pma_files_detail=pma_files_detail,
        automation=automation,
        generated_at=generated_at,
        stale_threshold_seconds=stale_threshold_seconds,
    )
    freshness = _build_snapshot_freshness_summary(
        generated_at=generated_at,
        stale_threshold_seconds=stale_threshold_seconds,
        repos=repos,
        agent_workspaces=agent_workspaces,
        inbox=inbox,
        action_queue=action_queue,
        pma_threads=pma_threads,
        pma_files_detail=pma_files_detail,
    )

    return {
        "generated_at": generated_at,
        "repos": repos,
        "agent_workspaces": agent_workspaces,
        "inbox": inbox,
        "action_queue": action_queue,
        "templates": templates,
        "pma_files": pma_files,
        "pma_files_detail": pma_files_detail,
        "pma_threads": pma_threads,
        "automation": automation,
        "lifecycle_events": lifecycle_events,
        "freshness": freshness,
        "limits": {
            "max_repos": limits.max_repos,
            "max_messages": limits.max_messages,
            "max_text_chars": limits.max_text_chars,
        },
    }
