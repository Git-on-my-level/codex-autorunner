from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ....core.capability_hints import (
    build_hub_capability_hints,
    build_repo_capability_hints,
)
from ....core.filebox import empty_listing
from ....core.flows.workspace_root import resolve_ticket_flow_workspace_root
from ....core.freshness import (
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
    annotate_pma_files_detail,
    build_pma_action_queue,
)
from ....core.pma_thread_store import default_pma_threads_db_path
from ....tickets.files import safe_relpath
from ....tickets.models import Dispatch
from ....tickets.outbox import parse_dispatch, resolve_outbox_paths
from ..app_state import HubAppContext
from ..schemas import (
    HubDispatchPayload,
    HubLatestDispatchResponse,
    HubMessageSnapshotResponse,
)

_HUB_SNAPSHOT_CACHE_TTL_SECONDS = 2.0
_hub_snapshot_cache_lock = threading.Lock()


@dataclass(frozen=True)
class _HubSnapshotCacheEntry:
    fingerprint: tuple[Any, ...]
    expires_at: float
    snapshot: dict[str, Any]


_hub_snapshot_cache: dict[tuple[int, str], _HubSnapshotCacheEntry] = {}


@dataclass(frozen=True)
class _HubSnapshotSettings:
    include_inbox_queue_metadata: bool
    include_full_action_queue_context: bool
    stale_threshold_seconds: int
    max_text_chars: int
    generated_at: str


@dataclass(frozen=True)
class _HubRepoMessageContext:
    snapshots: list[Any]
    repo_roots: dict[str, Path]
    hub_dismissals: dict[str, dict[str, Any]]
    repo_dismissals_by_id: dict[str, dict[str, dict[str, Any]]]


def _serialize_dispatch_payload(dispatch: Dispatch) -> HubDispatchPayload:
    return HubDispatchPayload(
        mode=dispatch.mode,
        title=dispatch.title,
        body=dispatch.body,
        extra=dict(dispatch.extra),
        is_handoff=dispatch.is_handoff,
    )


def _serialize_latest_dispatch_response(
    *,
    seq: int,
    repo_root: Path,
    dispatch_dir: Path,
    dispatch: Optional[Dispatch],
    errors: list[str],
    files: list[str],
    turn_summary_seq: Optional[int] = None,
    turn_summary: Optional[Dispatch] = None,
) -> dict[str, Any]:
    response = HubLatestDispatchResponse(
        seq=seq,
        dir=safe_relpath(dispatch_dir, repo_root),
        dispatch=(
            _serialize_dispatch_payload(dispatch) if dispatch is not None else None
        ),
        errors=list(errors),
        files=list(files),
        turn_summary_seq=turn_summary_seq,
        turn_summary=(
            _serialize_dispatch_payload(turn_summary)
            if turn_summary is not None
            else None
        ),
    )
    payload = response.model_dump(exclude_none=True)
    if dispatch is None:
        payload["dispatch"] = None
    return payload


def _path_stat_fingerprint(path: Path) -> tuple[bool, Optional[int], Optional[int]]:
    try:
        stat = path.stat()
    except OSError:
        return (False, None, None)
    return (True, int(stat.st_mtime_ns), int(stat.st_size))


def _hub_snapshot_fingerprint(
    context: HubAppContext,
    *,
    limit: int,
    scope_key: Optional[str],
    requested: set[str],
) -> tuple[Any, ...]:
    config_root = getattr(getattr(context, "config", None), "root", None)
    root_path = config_root if isinstance(config_root, Path) else None
    supervisor_state = getattr(getattr(context, "supervisor", None), "state", None)
    fingerprint: list[Any] = [
        tuple(sorted(requested)),
        int(limit),
        scope_key or "",
        getattr(supervisor_state, "last_scan_at", None),
    ]
    if root_path is not None:
        fingerprint.extend(
            [
                str(root_path),
                _path_stat_fingerprint(root_path / ".codex-autorunner"),
                _path_stat_fingerprint(root_path / ".codex-autorunner" / "filebox"),
                _path_stat_fingerprint(default_pma_threads_db_path(root_path)),
            ]
        )
    return tuple(fingerprint)


def invalidate_hub_message_snapshot_cache(
    context: Optional[HubAppContext] = None,
) -> None:
    with _hub_snapshot_cache_lock:
        if context is None:
            _hub_snapshot_cache.clear()
            return
        context_id = id(context)
        stale_keys = [key for key in _hub_snapshot_cache if key[0] == context_id]
        for key in stale_keys:
            _hub_snapshot_cache.pop(key, None)


def latest_dispatch(repo_root: Path, run_id: str, input_data: dict) -> Optional[dict]:
    try:
        workspace_root = resolve_ticket_flow_workspace_root(input_data, repo_root)
        outbox_paths = resolve_outbox_paths(
            workspace_root=workspace_root, run_id=run_id
        )
        history_dir = outbox_paths.dispatch_history_dir
        if not history_dir.exists() or not history_dir.is_dir():
            return None

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
                    return _serialize_latest_dispatch_response(
                        seq=seq,
                        repo_root=repo_root,
                        dispatch_dir=seq_dir,
                        dispatch=None,
                        errors=errors,
                        files=[],
                    )
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
                return _serialize_latest_dispatch_response(
                    seq=error_candidate["seq"],
                    repo_root=repo_root,
                    dispatch_dir=error_candidate["dir"],
                    dispatch=None,
                    errors=error_candidate["errors"],
                    files=[],
                )
            return None

        selected_dir = selected["dir"]
        dispatch = selected["dispatch"]
        return _serialize_latest_dispatch_response(
            seq=selected["seq"],
            repo_root=repo_root,
            dispatch_dir=selected_dir,
            dispatch=dispatch,
            errors=[],
            files=_list_files(selected_dir),
            turn_summary_seq=(
                turn_summary_candidate["seq"]
                if turn_summary_candidate is not None
                else None
            ),
            turn_summary=(
                turn_summary_candidate["dispatch"]
                if turn_summary_candidate is not None
                else None
            ),
        )
    except Exception:  # intentional: best-effort dispatch resolution
        return None


def _build_snapshot_settings(
    context: HubAppContext, requested: set[str]
) -> _HubSnapshotSettings:
    include_inbox_queue_metadata = "inbox" in requested
    include_full_action_queue_context = "action_queue" in requested
    pma_config = getattr(getattr(context, "config", None), "pma", None)
    stale_threshold_seconds = resolve_stale_threshold_seconds(
        getattr(pma_config, "freshness_stale_threshold_seconds", None)
    )
    max_text_chars = (
        pma_config.max_text_chars
        if pma_config and getattr(pma_config, "max_text_chars", 0) > 0
        else PMA_MAX_TEXT
    )
    return _HubSnapshotSettings(
        include_inbox_queue_metadata=include_inbox_queue_metadata,
        include_full_action_queue_context=include_full_action_queue_context,
        stale_threshold_seconds=stale_threshold_seconds,
        max_text_chars=max_text_chars,
        generated_at=iso_now(),
    )


def _collect_pma_files_detail(
    hub_root: Path,
    *,
    generated_at: str,
    stale_threshold_seconds: int,
) -> dict[str, list[dict[str, Any]]]:
    _, raw_listing = _snapshot_pma_files(hub_root)
    return annotate_pma_files_detail(
        raw_listing,
        generated_at=generated_at,
        stale_threshold_seconds=stale_threshold_seconds,
    )


def _collect_pma_threads(
    hub_root: Path,
    *,
    generated_at: str,
    stale_threshold_seconds: int,
) -> list[dict[str, Any]]:
    return _snapshot_pma_threads(
        hub_root,
        generated_at=generated_at,
        stale_threshold_seconds=stale_threshold_seconds,
    )


def _load_repo_message_context(
    context: HubAppContext, requested: set[str]
) -> _HubRepoMessageContext:
    snapshots: list[Any] = []
    repo_roots: dict[str, Path] = {}
    hub_dismissals: dict[str, dict[str, Any]] = {}
    if requested & {"inbox", "action_queue"}:
        try:
            snapshots = context.supervisor.list_repos()
        except (OSError, ValueError, RuntimeError):
            snapshots = []
        repo_roots = {
            snap.id: snap.path
            for snap in snapshots
            if isinstance(getattr(snap, "path", None), Path)
        }
        config_root = getattr(getattr(context, "config", None), "root", None)
        if isinstance(config_root, Path):
            hub_dismissals = load_hub_inbox_dismissals(config_root)
    return _HubRepoMessageContext(
        snapshots=snapshots,
        repo_roots=repo_roots,
        hub_dismissals=hub_dismissals,
        repo_dismissals_by_id={},
    )


def _repo_dismissals(
    repo_context: _HubRepoMessageContext,
    repo_id: str,
    repo_root: Optional[Path],
) -> dict[str, dict[str, Any]]:
    dismissals = repo_context.repo_dismissals_by_id.get(repo_id)
    if dismissals is not None:
        return dismissals
    if not repo_id or not isinstance(repo_root, Path):
        return {}
    dismissals = load_hub_inbox_dismissals(repo_root)
    repo_context.repo_dismissals_by_id[repo_id] = dismissals
    return dismissals


def _serialize_resolvable_item(item: dict[str, Any], item_type: str) -> dict[str, Any]:
    return {
        **item,
        "resolution_state": message_resolution_state(item_type),
        "resolvable_actions": message_resolvable_actions(item_type),
    }


def _filter_action_queue_items(
    action_queue: list[dict[str, Any]],
    *,
    repo_context: _HubRepoMessageContext,
    scope_key: Optional[str],
) -> list[dict[str, Any]]:
    filtered_items: list[dict[str, Any]] = []
    for item in action_queue:
        if str(item.get("queue_source") or "") != "ticket_flow_inbox":
            filtered_items.append(dict(item))
            continue
        repo_id = str(item.get("repo_id") or "").strip()
        run_id = str(item.get("run_id") or "").strip()
        if not repo_id or not run_id:
            continue
        repo_root_raw = item.get("repo_path")
        repo_root = Path(repo_root_raw) if isinstance(repo_root_raw, str) else None
        if repo_root is None or not repo_root.exists():
            repo_root = repo_context.repo_roots.get(repo_id)
        if repo_root is None:
            continue
        dismissals = _repo_dismissals(repo_context, repo_id, repo_root)
        item_type = str(item.get("item_type") or "run_dispatch")
        seq_raw = item.get("seq")
        item_seq = seq_raw if isinstance(seq_raw, int) and seq_raw > 0 else None
        candidate_item_types = [item_type]
        if item_seq is None and not bool(item.get("dispatch_actionable")):
            candidate_item_types.extend(
                ["run_failed", "run_stopped", "run_state_attention"]
            )
        if any(
            find_message_resolution(
                dismissals,
                run_id=run_id,
                item_type=candidate_type,
                seq=item_seq,
                scope_key=scope_key,
            )
            for candidate_type in dict.fromkeys(candidate_item_types)
        ):
            continue
        if item_seq is None and not bool(item.get("dispatch_actionable")):
            if any(
                str(resolution.get("run_id") or "").strip() == run_id
                and str(resolution.get("item_type") or "").strip()
                in {"run_failed", "run_stopped", "run_state_attention"}
                and str(resolution.get("resolution_state") or "").strip().lower()
                in {"", "dismissed", "resolved"}
                for resolution in dismissals.values()
                if isinstance(resolution, dict)
            ):
                continue
        filtered_items.append(_serialize_resolvable_item(dict(item), item_type))
    return filtered_items


def _collect_inbox_messages(
    context: HubAppContext,
    *,
    requested: set[str],
    limit: int,
    scope_key: Optional[str],
    repo_context: _HubRepoMessageContext,
    filtered_action_queue: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if "inbox" not in requested:
        return []

    messages: list[dict[str, Any]] = []
    try:
        hub_hint_items = build_hub_capability_hints(hub_config=context.config)
    except (AttributeError, OSError, ValueError, RuntimeError):
        hub_hint_items = []
    for item in hub_hint_items:
        item_type = str(item.get("item_type") or "")
        run_id = str(item.get("run_id") or "").strip()
        if not item_type or not run_id:
            continue
        resolution = find_message_resolution(
            repo_context.hub_dismissals,
            run_id=run_id,
            item_type=item_type,
            seq=None,
            hint_id=str(item.get("hint_id") or "").strip() or None,
            scope_key=scope_key,
        )
        if resolution is None:
            for snap in repo_context.snapshots:
                repo_id = str(getattr(snap, "id", "") or "").strip()
                repo_root = getattr(snap, "path", None)
                if not repo_id or not isinstance(repo_root, Path):
                    continue
                dismissals = _repo_dismissals(repo_context, repo_id, repo_root)
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
        if resolution is None:
            messages.append(_serialize_resolvable_item(dict(item), item_type))

    for snap in repo_context.snapshots:
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
        except (AttributeError, OSError, ValueError, RuntimeError):
            hint_items = []
        dismissals = _repo_dismissals(repo_context, repo_id, repo_root)
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
            messages.append(_serialize_resolvable_item(dict(item), item_type))

    messages.extend(
        copied
        for copied in filtered_action_queue
        if str(copied.get("queue_source") or "") == "ticket_flow_inbox"
    )
    messages.sort(key=lambda message: int(message.get("queue_rank") or 0))
    if limit and limit > 0:
        return messages[: int(limit)]
    return messages


def _serialize_hub_snapshot(
    *,
    generated_at: str,
    requested: set[str],
    messages: list[dict[str, Any]],
    pma_threads: list[dict[str, Any]],
    pma_files_detail: dict[str, list[dict[str, Any]]],
    automation: dict[str, Any],
    filtered_action_queue: list[dict[str, Any]],
) -> dict[str, Any]:
    snapshot = HubMessageSnapshotResponse(
        generated_at=generated_at,
        items=messages if "inbox" in requested else None,
        pma_threads=pma_threads if "pma_threads" in requested else None,
        pma_files_detail=(
            pma_files_detail if "pma_files_detail" in requested else None
        ),
        automation=automation if "automation" in requested else None,
        action_queue=filtered_action_queue if "action_queue" in requested else None,
    )
    return snapshot.model_dump(exclude_none=True)


def gather_hub_message_snapshot(
    context: HubAppContext,
    *,
    limit: int = 100,
    scope_key: Optional[str] = None,
    sections: Optional[set[str]] = None,
) -> dict[str, Any]:
    requested = (
        set(sections)
        if sections is not None
        else {"inbox", "pma_threads", "pma_files_detail", "automation", "action_queue"}
    )
    settings = _build_snapshot_settings(context, requested)
    cache_key = (id(context), "|".join(sorted(requested)))
    fingerprint = _hub_snapshot_fingerprint(
        context,
        limit=limit,
        scope_key=scope_key,
        requested=requested,
    )
    now = time.monotonic()
    with _hub_snapshot_cache_lock:
        cached = _hub_snapshot_cache.get(cache_key)
        if (
            cached is not None
            and cached.expires_at > now
            and cached.fingerprint == fingerprint
        ):
            return copy.deepcopy(cached.snapshot)
    inbox: list[dict[str, Any]] = []
    if (
        settings.include_inbox_queue_metadata
        or settings.include_full_action_queue_context
    ):
        inbox = _gather_inbox(
            context.supervisor,
            max_text_chars=settings.max_text_chars,
            stale_threshold_seconds=settings.stale_threshold_seconds,
        )

    hub_root = getattr(getattr(context, "config", None), "root", None)
    pma_files_detail: dict[str, list[dict[str, Any]]] = empty_listing()
    pma_threads: list[dict[str, Any]] = []
    automation = (
        _snapshot_pma_automation(context.supervisor)
        if requested & {"automation"} or settings.include_full_action_queue_context
        else {"items": [], "summary": {}}
    )
    if isinstance(hub_root, Path):
        if (
            requested & {"pma_files_detail"}
            or settings.include_full_action_queue_context
        ):
            pma_files_detail = _collect_pma_files_detail(
                hub_root,
                generated_at=settings.generated_at,
                stale_threshold_seconds=settings.stale_threshold_seconds,
            )
        if requested & {"pma_threads"} or settings.include_full_action_queue_context:
            pma_threads = _collect_pma_threads(
                hub_root,
                generated_at=settings.generated_at,
                stale_threshold_seconds=settings.stale_threshold_seconds,
            )

    action_queue: list[dict[str, Any]] = []
    if (
        settings.include_inbox_queue_metadata
        or settings.include_full_action_queue_context
    ):
        action_queue = build_pma_action_queue(
            inbox=inbox,
            pma_threads=pma_threads,
            pma_files_detail=pma_files_detail,
            automation=automation,
            generated_at=settings.generated_at,
            stale_threshold_seconds=settings.stale_threshold_seconds,
        )

    repo_context = _load_repo_message_context(context, requested)
    filtered_action_queue = _filter_action_queue_items(
        action_queue,
        repo_context=repo_context,
        scope_key=scope_key,
    )
    messages = _collect_inbox_messages(
        context,
        requested=requested,
        limit=limit,
        scope_key=scope_key,
        repo_context=repo_context,
        filtered_action_queue=filtered_action_queue,
    )

    snapshot = _serialize_hub_snapshot(
        generated_at=settings.generated_at,
        requested=requested,
        messages=messages,
        pma_threads=pma_threads,
        pma_files_detail=pma_files_detail,
        automation=automation,
        filtered_action_queue=filtered_action_queue,
    )
    store_fingerprint = _hub_snapshot_fingerprint(
        context,
        limit=limit,
        scope_key=scope_key,
        requested=requested,
    )
    with _hub_snapshot_cache_lock:
        _hub_snapshot_cache[cache_key] = _HubSnapshotCacheEntry(
            fingerprint=store_fingerprint,
            expires_at=now + _HUB_SNAPSHOT_CACHE_TTL_SECONDS,
            snapshot=copy.deepcopy(snapshot),
        )
    return snapshot
