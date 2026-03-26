from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ....core.capability_hints import (
    build_hub_capability_hints,
    build_repo_capability_hints,
)
from ....core.filebox import BOXES, empty_listing
from ....core.flows.workspace_root import resolve_ticket_flow_workspace_root
from ....core.freshness import (
    build_freshness_payload,
    iso_now,
    resolve_stale_threshold_seconds,
)
from ....core.hub_inbox_resolution import (
    find_message_resolution,
    load_hub_inbox_dismissals,
    message_resolution_state,
    message_resolvable_actions,
)
from ....core.pma_context import (
    PMA_MAX_TEXT,
    _gather_inbox,
    _snapshot_pma_automation,
    _snapshot_pma_files,
    _snapshot_pma_threads,
    build_pma_action_queue,
)
from ....tickets.files import safe_relpath
from ....tickets.models import Dispatch
from ....tickets.outbox import parse_dispatch, resolve_outbox_paths
from ..app_state import HubAppContext


def latest_dispatch(repo_root: Path, run_id: str, input_data: dict) -> Optional[dict]:
    try:
        workspace_root = resolve_ticket_flow_workspace_root(input_data, repo_root)
        outbox_paths = resolve_outbox_paths(
            workspace_root=workspace_root, run_id=run_id
        )
        history_dir = outbox_paths.dispatch_history_dir
        if not history_dir.exists() or not history_dir.is_dir():
            return None

        def _dispatch_dict(dispatch: Dispatch) -> dict:
            return {
                "mode": dispatch.mode,
                "title": dispatch.title,
                "body": dispatch.body,
                "extra": dispatch.extra,
                "is_handoff": dispatch.is_handoff,
            }

        def _list_files(dispatch_dir: Path) -> list[str]:
            files: list[str] = []
            for child in sorted(dispatch_dir.iterdir(), key=lambda p: p.name):
                if child.name.startswith("."):
                    continue
                if child.name == "DISPATCH.md":
                    continue
                if child.is_file():
                    files.append(child.name)
            return files

        seq_dirs: list[Path] = []
        for child in history_dir.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if len(name) == 4 and name.isdigit():
                seq_dirs.append(child)
        if not seq_dirs:
            return None

        seq_dirs = sorted(seq_dirs, key=lambda p: p.name, reverse=True)
        latest_seq = int(seq_dirs[0].name) if seq_dirs else None
        handoff_candidate: Optional[dict] = None
        non_summary_candidate: Optional[dict] = None
        turn_summary_candidate: Optional[dict] = None
        error_candidate: Optional[dict] = None

        for seq_dir in seq_dirs:
            seq = int(seq_dir.name)
            dispatch_path = seq_dir / "DISPATCH.md"
            dispatch, errors = parse_dispatch(dispatch_path)
            if errors or dispatch is None:
                if latest_seq is not None and seq == latest_seq:
                    return {
                        "seq": seq,
                        "dir": safe_relpath(seq_dir, repo_root),
                        "dispatch": None,
                        "errors": errors,
                        "files": [],
                    }
                if error_candidate is None:
                    error_candidate = {
                        "seq": seq,
                        "dir": seq_dir,
                        "errors": errors,
                    }
                continue
            candidate = {"seq": seq, "dir": seq_dir, "dispatch": dispatch}
            if dispatch.is_handoff and handoff_candidate is None:
                handoff_candidate = candidate
            if dispatch.mode != "turn_summary" and non_summary_candidate is None:
                non_summary_candidate = candidate
            if dispatch.mode == "turn_summary" and turn_summary_candidate is None:
                turn_summary_candidate = candidate
            if handoff_candidate and non_summary_candidate and turn_summary_candidate:
                break

        selected = handoff_candidate or non_summary_candidate or turn_summary_candidate
        if not selected:
            if error_candidate:
                return {
                    "seq": error_candidate["seq"],
                    "dir": safe_relpath(error_candidate["dir"], repo_root),
                    "dispatch": None,
                    "errors": error_candidate["errors"],
                    "files": [],
                }
            return None

        selected_dir = selected["dir"]
        dispatch = selected["dispatch"]
        result = {
            "seq": selected["seq"],
            "dir": safe_relpath(selected_dir, repo_root),
            "dispatch": _dispatch_dict(dispatch),
            "errors": [],
            "files": _list_files(selected_dir),
        }
        if turn_summary_candidate is not None:
            result["turn_summary_seq"] = turn_summary_candidate["seq"]
            result["turn_summary"] = _dispatch_dict(turn_summary_candidate["dispatch"])
        return result
    except Exception:
        return None


def gather_hub_messages(
    context: HubAppContext, *, limit: int = 100, scope_key: Optional[str] = None
) -> list[dict]:
    pma_config = getattr(getattr(context, "config", None), "pma", None)
    stale_threshold_seconds = resolve_stale_threshold_seconds(
        getattr(pma_config, "freshness_stale_threshold_seconds", None)
    )
    max_text_chars = (
        pma_config.max_text_chars
        if pma_config and getattr(pma_config, "max_text_chars", 0) > 0
        else PMA_MAX_TEXT
    )
    generated_at = iso_now()
    inbox = _gather_inbox(
        context.supervisor,
        max_text_chars=max_text_chars,
        stale_threshold_seconds=stale_threshold_seconds,
    )

    hub_root = getattr(getattr(context, "config", None), "root", None)
    pma_files_detail: dict[str, list[dict[str, Any]]] = empty_listing()
    pma_threads: list[dict[str, Any]] = []
    automation = _snapshot_pma_automation(context.supervisor)
    if isinstance(hub_root, Path):
        _, pma_files_detail = _snapshot_pma_files(hub_root)
        pma_threads = _snapshot_pma_threads(hub_root)
        for thread in pma_threads:
            thread["freshness"] = build_freshness_payload(
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                candidates=[("thread_updated_at", thread.get("updated_at"))],
            )
        for box in BOXES:
            for entry in pma_files_detail.get(box) or []:
                entry["freshness"] = build_freshness_payload(
                    generated_at=generated_at,
                    stale_threshold_seconds=stale_threshold_seconds,
                    candidates=[("file_modified_at", entry.get("modified_at"))],
                )

    action_queue = build_pma_action_queue(
        inbox=inbox,
        pma_threads=pma_threads,
        pma_files_detail=pma_files_detail,
        automation=automation,
        generated_at=generated_at,
        stale_threshold_seconds=stale_threshold_seconds,
    )

    messages: list[dict] = []
    try:
        snapshots = context.supervisor.list_repos()
    except Exception:
        return []
    repo_roots = {snap.id: snap.path for snap in snapshots}
    hub_dismissals = load_hub_inbox_dismissals(context.config.root)
    repo_dismissals_by_id: dict[str, dict[str, dict[str, Any]]] = {}
    try:
        hub_hint_items = build_hub_capability_hints(hub_config=context.config)
    except Exception:
        hub_hint_items = []
    for item in hub_hint_items:
        item_type = str(item.get("item_type") or "")
        run_id = str(item.get("run_id") or "").strip()
        if not item_type or not run_id:
            continue
        resolution = find_message_resolution(
            hub_dismissals,
            run_id=run_id,
            item_type=item_type,
            seq=None,
            hint_id=str(item.get("hint_id") or "").strip() or None,
            scope_key=scope_key,
        )
        if resolution is None:
            for snap in snapshots:
                repo_id = str(getattr(snap, "id", "") or "").strip()
                repo_root = getattr(snap, "path", None)
                if not repo_id or not isinstance(repo_root, Path):
                    continue
                dismissals = repo_dismissals_by_id.get(repo_id)
                if dismissals is None:
                    dismissals = load_hub_inbox_dismissals(repo_root)
                    repo_dismissals_by_id[repo_id] = dismissals
                resolution = find_message_resolution(
                    dismissals,
                    run_id=run_id,
                    item_type=item_type,
                    seq=None,
                    hint_id=str(item.get("hint_id") or "").strip() or None,
                    scope_key=scope_key,
                )
                if resolution is not None:
                    break
        if resolution is not None:
            continue
        copied = dict(item)
        copied["resolution_state"] = message_resolution_state(item_type)
        copied["resolvable_actions"] = message_resolvable_actions(item_type)
        messages.append(copied)

    for snap in snapshots:
        repo_id = str(getattr(snap, "id", "") or "").strip()
        repo_root = getattr(snap, "path", None)
        if not repo_id or not isinstance(repo_root, Path):
            continue
        try:
            hint_items = build_repo_capability_hints(
                hub_config=context.config,
                repo_id=repo_id,
                repo_root=repo_root,
                repo_display_name=str(
                    getattr(snap, "display_name", None) or getattr(snap, "id", "")
                ).strip()
                or repo_id,
            )
        except Exception:
            hint_items = []
        dismissals = repo_dismissals_by_id.get(repo_id)
        if dismissals is None:
            dismissals = load_hub_inbox_dismissals(repo_root)
            repo_dismissals_by_id[repo_id] = dismissals
        for item in hint_items:
            item_type = str(item.get("item_type") or "")
            run_id = str(item.get("run_id") or "").strip()
            if not item_type or not run_id:
                continue
            if find_message_resolution(
                dismissals,
                run_id=run_id,
                item_type=item_type,
                seq=None,
                hint_id=str(item.get("hint_id") or "").strip() or None,
                scope_key=scope_key,
            ):
                continue
            copied = dict(item)
            copied["resolution_state"] = message_resolution_state(item_type)
            copied["resolvable_actions"] = message_resolvable_actions(item_type)
            messages.append(copied)

    for item in action_queue:
        if str(item.get("queue_source") or "") != "ticket_flow_inbox":
            continue
        repo_id = str(item.get("repo_id") or "").strip()
        run_id = str(item.get("run_id") or "").strip()
        if not repo_id or not run_id:
            continue
        repo_root_raw = item.get("repo_path")
        repo_root = Path(repo_root_raw) if isinstance(repo_root_raw, str) else None
        if repo_root is None or not repo_root.exists():
            repo_root = repo_roots.get(repo_id)
        if repo_root is None:
            continue
        dismissals = repo_dismissals_by_id.get(repo_id)
        if dismissals is None:
            dismissals = load_hub_inbox_dismissals(repo_root)
            repo_dismissals_by_id[repo_id] = dismissals
        item_type = str(item.get("item_type") or "run_dispatch")
        seq_raw = item.get("seq")
        item_seq = seq_raw if isinstance(seq_raw, int) and seq_raw > 0 else None
        if find_message_resolution(
            dismissals,
            run_id=run_id,
            item_type=item_type,
            seq=item_seq,
            scope_key=scope_key,
        ):
            continue
        copied = dict(item)
        copied["resolution_state"] = message_resolution_state(item_type)
        copied["resolvable_actions"] = message_resolvable_actions(item_type)
        messages.append(copied)

    messages.sort(key=lambda m: int(m.get("queue_rank") or 0))
    if limit and limit > 0:
        return messages[: int(limit)]
    return messages
