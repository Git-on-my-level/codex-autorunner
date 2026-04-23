from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, cast

from ..tickets.files import safe_relpath
from ..tickets.models import Dispatch
from .capability_hints import build_hub_capability_hints, build_repo_capability_hints
from .chat_bindings import active_chat_binding_counts_by_source
from .config import (
    CONFIG_FILENAME,
    REPO_OVERRIDE_FILENAME,
    ROOT_CONFIG_FILENAME,
    ROOT_OVERRIDE_FILENAME,
)
from .filebox import BOXES, empty_listing
from .freshness import (
    build_freshness_payload,
    iso_now,
    resolve_stale_threshold_seconds,
    summarize_section_freshness,
)
from .hub_inbox_resolution import (
    find_message_resolution,
    load_hub_inbox_dismissals,
    message_resolution_state,
    message_resolvable_actions,
)
from .hub_projection_store import (
    HUB_LISTING_PROJECTION_NAMESPACE,
    HUB_SNAPSHOT_PROJECTION_NAMESPACE,
    REPO_CAPABILITY_HINT_PROJECTION_NAMESPACE,
    path_stat_fingerprint,
)
from .logging_utils import safe_log
from .orchestration.sqlite import resolve_orchestration_sqlite_path
from .pma_context import (
    PMA_MAX_TEXT,
    _gather_inbox,
    _snapshot_pma_automation,
    _snapshot_pma_files,
    _snapshot_pma_threads,
    build_pma_action_queue,
    enrich_pma_file_inbox_entry,
)
from .pma_thread_store import default_pma_threads_db_path
from .ticket_flow_operator import (
    latest_ticket_flow_dispatch as _latest_ticket_flow_dispatch,
)

_HUB_SNAPSHOT_CACHE_TTL_SECONDS = 2.0
_REPO_CAPABILITY_HINT_CACHE_TTL_SECONDS = 30.0
_HUB_SNAPSHOT_PROJECTION_MAX_AGE_SECONDS = 10.0
_REPO_CAPABILITY_HINT_PROJECTION_MAX_AGE_SECONDS = 60.0
_REPO_LISTING_RESPONSE_CACHE_TTL_SECONDS = 20.0
_HUB_LISTING_PROJECTION_MAX_AGE_SECONDS = 60.0

REPO_LISTING_SECTIONS = frozenset({"repos", "agent_workspaces", "freshness"})


class RepoProjectionProvider(Protocol):
    def repo_state_fingerprint(
        self,
        snapshot: Any,
        *,
        stale_threshold_seconds: Optional[int],
    ) -> tuple[Any, ...]: ...

    def enrich_repo(
        self,
        snapshot: Any,
        chat_binding_counts: Optional[dict[str, int]] = None,
        chat_binding_counts_by_source: Optional[dict[str, dict[str, int]]] = None,
        unbound_thread_counts: Optional[dict[str, int]] = None,
    ) -> dict[str, Any]: ...

    def unbound_repo_thread_counts_snapshot(self) -> dict[str, int]: ...


@dataclass(frozen=True)
class _HubSnapshotCacheEntry:
    fingerprint: tuple[Any, ...]
    expires_at: float
    snapshot: dict[str, Any]


@dataclass(frozen=True)
class _RepoCapabilityHintCacheEntry:
    fingerprint: tuple[Any, ...]
    expires_at: float
    items: list[dict[str, Any]]


@dataclass(frozen=True)
class _RepoListingCacheEntry:
    fingerprint: tuple[Any, ...]
    expires_at: float
    payload: dict[str, Any]


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


_hub_snapshot_cache_lock = threading.Lock()
_hub_snapshot_cache: dict[tuple[int, str], _HubSnapshotCacheEntry] = {}

_repo_capability_hint_cache_lock = threading.Lock()
_repo_capability_hint_cache: dict[
    tuple[str, str, str, str], _RepoCapabilityHintCacheEntry
] = {}

_service_registry_lock = threading.Lock()
_service_registry: dict[int, "HubReadModelService"] = {}


def _monotonic() -> float:
    return time.monotonic()


def _serialize_dispatch_payload(dispatch: Dispatch) -> dict[str, Any]:
    return {
        "mode": dispatch.mode,
        "title": dispatch.title,
        "body": dispatch.body,
        "extra": dict(dispatch.extra),
        "is_handoff": dispatch.is_handoff,
    }


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
    payload: dict[str, Any] = {
        "seq": seq,
        "dir": safe_relpath(dispatch_dir, repo_root),
        "dispatch": (
            _serialize_dispatch_payload(dispatch) if dispatch is not None else None
        ),
        "errors": list(errors),
        "files": list(files),
    }
    if turn_summary_seq is not None:
        payload["turn_summary_seq"] = turn_summary_seq
    if turn_summary is not None:
        payload["turn_summary"] = _serialize_dispatch_payload(turn_summary)
    return payload


def latest_dispatch(
    repo_root: Path, run_id: str, input_data: dict
) -> Optional[dict[str, Any]]:
    return _latest_ticket_flow_dispatch(
        repo_root,
        run_id,
        input_data,
        include_turn_summary=True,
    )


def _build_snapshot_settings(context: Any, requested: set[str]) -> _HubSnapshotSettings:
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


def _serialize_pma_file_entry(
    box: str,
    entry: dict[str, Any],
    *,
    generated_at: str,
    stale_threshold_seconds: int,
) -> dict[str, Any]:
    payload = dict(entry)
    payload["freshness"] = build_freshness_payload(
        generated_at=generated_at,
        stale_threshold_seconds=stale_threshold_seconds,
        candidates=[("file_modified_at", entry.get("modified_at"))],
    )
    if box == "inbox":
        return enrich_pma_file_inbox_entry(payload)
    return payload


def _collect_pma_files_detail(
    hub_root: Path,
    *,
    generated_at: str,
    stale_threshold_seconds: int,
) -> dict[str, list[dict[str, Any]]]:
    _, raw_listing = _snapshot_pma_files(hub_root)
    return {
        box: [
            _serialize_pma_file_entry(
                box,
                entry,
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
            )
            for entry in raw_listing.get(box) or []
        ]
        for box in BOXES
    }


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
    context: Any, requested: set[str]
) -> tuple[_HubRepoMessageContext, list[dict[str, str]]]:
    unreadable_diagnostics: list[dict[str, str]] = []
    snapshots: list[Any] = []
    repo_roots: dict[str, Path] = {}
    hub_dismissals: dict[str, dict[str, Any]] = {}
    if requested & {"inbox", "action_queue"}:
        try:
            snapshots = context.supervisor.list_repos()
        except (OSError, ValueError, RuntimeError) as exc:
            snapshots = []
            unreadable_diagnostics.append(
                {
                    "section": "inbox",
                    "reason": str(exc) or type(exc).__name__,
                    "source": "supervisor.list_repos",
                }
            )
        repo_roots = {
            snap.id: snap.path
            for snap in snapshots
            if isinstance(getattr(snap, "path", None), Path)
        }
        config_root = getattr(getattr(context, "config", None), "root", None)
        if isinstance(config_root, Path):
            hub_dismissals = load_hub_inbox_dismissals(config_root)
    return (
        _HubRepoMessageContext(
            snapshots=snapshots,
            repo_roots=repo_roots,
            hub_dismissals=hub_dismissals,
            repo_dismissals_by_id={},
        ),
        unreadable_diagnostics,
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


def _hub_snapshot_fingerprint(
    context: Any,
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
        orchestration_db_path = resolve_orchestration_sqlite_path(root_path)
        fingerprint.extend(
            [
                str(root_path),
                path_stat_fingerprint(root_path / ".codex-autorunner" / "filebox"),
                path_stat_fingerprint(default_pma_threads_db_path(root_path)),
                path_stat_fingerprint(orchestration_db_path),
                path_stat_fingerprint(Path(f"{orchestration_db_path}-wal")),
            ]
        )
    return tuple(fingerprint)


def _repo_capability_hint_fingerprint(
    *,
    hub_root: Optional[Path],
    repo_id: str,
    repo_root: Path,
    repo_display_name: str,
) -> tuple[Any, ...]:
    hub_config_root = hub_root if isinstance(hub_root, Path) else None
    return (
        repo_id,
        repo_display_name,
        str(repo_root),
        str(hub_config_root) if hub_config_root is not None else "",
        path_stat_fingerprint(repo_root / REPO_OVERRIDE_FILENAME),
        path_stat_fingerprint(repo_root / ".env"),
        path_stat_fingerprint(repo_root / ".codex-autorunner" / ".env"),
        path_stat_fingerprint(repo_root / CONFIG_FILENAME),
        path_stat_fingerprint(
            hub_config_root / ROOT_CONFIG_FILENAME
            if hub_config_root is not None
            else Path(ROOT_CONFIG_FILENAME)
        ),
        path_stat_fingerprint(
            hub_config_root / ROOT_OVERRIDE_FILENAME
            if hub_config_root is not None
            else Path(ROOT_OVERRIDE_FILENAME)
        ),
        path_stat_fingerprint(
            hub_config_root / CONFIG_FILENAME
            if hub_config_root is not None
            else Path(CONFIG_FILENAME)
        ),
    )


def _cached_repo_capability_hints(
    context: Any,
    *,
    repo_id: str,
    repo_root: Path,
    repo_display_name: str,
    unreadable_diagnostics: Optional[list[dict[str, str]]] = None,
) -> list[dict[str, Any]]:
    hub_root = getattr(getattr(context, "config", None), "root", None)
    cache_key = (
        str(hub_root) if isinstance(hub_root, Path) else "",
        str(repo_root),
        repo_id,
        repo_display_name,
    )
    fingerprint = _repo_capability_hint_fingerprint(
        hub_root=hub_root,
        repo_id=repo_id,
        repo_root=repo_root,
        repo_display_name=repo_display_name,
    )
    now = _monotonic()
    with _repo_capability_hint_cache_lock:
        cached = _repo_capability_hint_cache.get(cache_key)
        if (
            cached is not None
            and cached.expires_at > now
            and cached.fingerprint == fingerprint
        ):
            return [dict(item) for item in cached.items]
    projection_store = getattr(context, "projection_store", None)
    durable_cache_key = f"repo_hints:{repo_id}"
    if projection_store is not None:
        try:
            durable_cached = projection_store.get_cache(
                durable_cache_key,
                fingerprint,
                max_age_seconds=_REPO_CAPABILITY_HINT_PROJECTION_MAX_AGE_SECONDS,
                namespace=REPO_CAPABILITY_HINT_PROJECTION_NAMESPACE,
            )
        except Exception:
            durable_cached = None
        if durable_cached is not None and isinstance(durable_cached, list):
            promoted_items = [dict(item) for item in durable_cached]
            with _repo_capability_hint_cache_lock:
                _repo_capability_hint_cache[cache_key] = _RepoCapabilityHintCacheEntry(
                    fingerprint=fingerprint,
                    expires_at=now + _REPO_CAPABILITY_HINT_CACHE_TTL_SECONDS,
                    items=promoted_items,
                )
            return [dict(item) for item in promoted_items]
    try:
        hint_items = build_repo_capability_hints(
            hub_config=context.config,
            repo_id=repo_id,
            repo_root=repo_root,
            repo_display_name=repo_display_name,
        )
    except (AttributeError, OSError, ValueError, RuntimeError) as exc:
        hint_items = []
        if unreadable_diagnostics is not None:
            unreadable_diagnostics.append(
                {
                    "section": "inbox",
                    "reason": str(exc) or type(exc).__name__,
                    "source": f"build_repo_capability_hints:{repo_id}",
                }
            )
    stored_items = [dict(item) for item in hint_items]
    with _repo_capability_hint_cache_lock:
        _repo_capability_hint_cache[cache_key] = _RepoCapabilityHintCacheEntry(
            fingerprint=fingerprint,
            expires_at=now + _REPO_CAPABILITY_HINT_CACHE_TTL_SECONDS,
            items=stored_items,
        )
    if projection_store is not None:
        try:
            projection_store.set_cache(
                durable_cache_key,
                fingerprint,
                stored_items,
                namespace=REPO_CAPABILITY_HINT_PROJECTION_NAMESPACE,
            )
        except Exception:
            pass
    return [dict(item) for item in stored_items]


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
    context: Any,
    *,
    requested: set[str],
    limit: int,
    scope_key: Optional[str],
    repo_context: _HubRepoMessageContext,
    filtered_action_queue: list[dict[str, Any]],
    unreadable_diagnostics: list[dict[str, str]],
) -> list[dict[str, Any]]:
    if "inbox" not in requested:
        return []
    messages: list[dict[str, Any]] = []
    try:
        hub_hint_items = build_hub_capability_hints(hub_config=context.config)
    except (AttributeError, OSError, ValueError, RuntimeError) as exc:
        hub_hint_items = []
        unreadable_diagnostics.append(
            {
                "section": "inbox",
                "reason": str(exc) or type(exc).__name__,
                "source": "build_hub_capability_hints",
            }
        )
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
        repo_display_name = (
            str(getattr(snap, "display_name", None) or getattr(snap, "id", "")).strip()
            or repo_id
        )
        hint_items = _cached_repo_capability_hints(
            context,
            repo_id=repo_id,
            repo_root=repo_root,
            repo_display_name=repo_display_name,
            unreadable_diagnostics=unreadable_diagnostics,
        )
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
    unreadable_diagnostics: list[dict[str, str]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {"generated_at": generated_at}
    if "inbox" in requested:
        payload["items"] = messages
    if "pma_threads" in requested:
        payload["pma_threads"] = pma_threads
    if "pma_files_detail" in requested:
        payload["pma_files_detail"] = pma_files_detail
    if "automation" in requested:
        payload["automation"] = automation
    if "action_queue" in requested:
        payload["action_queue"] = filtered_action_queue
    if unreadable_diagnostics:
        payload["unreadable_diagnostics"] = unreadable_diagnostics
    return payload


class HubReadModelService:
    def __init__(
        self,
        context: Any,
        *,
        repo_projection_provider: Optional[RepoProjectionProvider] = None,
        prepare_repo_snapshots: Optional[Callable[[list[Any]], Any]] = None,
    ) -> None:
        self._context = context
        self._repo_projection_provider = repo_projection_provider
        self._prepare_repo_snapshots = prepare_repo_snapshots
        self._response_cache: dict[tuple[str, ...], _RepoListingCacheEntry] = {}
        self._response_cache_lock = threading.Lock()
        self._response_refresh_tasks: dict[tuple[str, ...], asyncio.Task[None]] = {}
        self._response_refresh_tasks_lock = threading.Lock()

    def bind_repo_projection_provider(
        self,
        *,
        repo_projection_provider: Optional[RepoProjectionProvider] = None,
        prepare_repo_snapshots: Optional[Callable[[list[Any]], Any]] = None,
    ) -> None:
        if repo_projection_provider is not None:
            self._repo_projection_provider = repo_projection_provider
        if prepare_repo_snapshots is not None:
            self._prepare_repo_snapshots = prepare_repo_snapshots

    def _projection_store(self) -> Any:
        return getattr(self._context, "projection_store", None)

    def _require_repo_projection_provider(self) -> RepoProjectionProvider:
        provider = self._repo_projection_provider
        if provider is None:
            raise RuntimeError("Hub repo projection provider is not configured")
        return provider

    async def _prepare_snapshots(self, snapshots: list[Any]) -> None:
        prepare = self._prepare_repo_snapshots
        if prepare is None:
            return
        result = prepare(snapshots)
        if inspect.isawaitable(result):
            await cast(Awaitable[Any], result)

    def _repo_runtime_fingerprint(
        self, snapshot: Any, *, stale_threshold_seconds: Optional[int]
    ) -> tuple[Any, ...]:
        return self._require_repo_projection_provider().repo_state_fingerprint(
            snapshot,
            stale_threshold_seconds=stale_threshold_seconds,
        )

    def _listing_fingerprint(
        self,
        *,
        requested: set[str],
        stale_threshold_seconds: int,
        repos: list[Any],
        agent_workspaces: list[Any],
    ) -> tuple[Any, ...]:
        supervisor_state = getattr(self._context.supervisor, "state", None)
        pinned_parent_repo_ids = (
            getattr(supervisor_state, "pinned_parent_repo_ids", []) or []
        )
        manifest_path = getattr(self._context.config, "manifest_path", None)
        return (
            tuple(sorted(requested)),
            getattr(supervisor_state, "last_scan_at", None),
            tuple(pinned_parent_repo_ids),
            (
                path_stat_fingerprint(manifest_path)
                if isinstance(manifest_path, Path)
                else None
            ),
            tuple(
                self._repo_runtime_fingerprint(
                    snap, stale_threshold_seconds=stale_threshold_seconds
                )
                for snap in repos
            ),
            tuple(
                (
                    workspace.id,
                    workspace.runtime,
                    str(workspace.path),
                    workspace.display_name,
                    workspace.enabled,
                    workspace.exists_on_disk,
                )
                for workspace in agent_workspaces
            ),
        )

    async def _enrich_repos(
        self,
        snapshots: list[Any],
        chat_binding_counts: dict[str, int],
        chat_binding_counts_by_source: dict[str, dict[str, int]],
        unbound_thread_counts: Optional[dict[str, int]] = None,
    ) -> list[dict[str, Any]]:
        provider = self._require_repo_projection_provider()
        supports_unbound_counts = unbound_thread_counts is not None and callable(
            getattr(provider, "unbound_repo_thread_counts_snapshot", None)
        )
        if supports_unbound_counts:
            tasks = [
                asyncio.to_thread(
                    provider.enrich_repo,
                    snap,
                    chat_binding_counts,
                    chat_binding_counts_by_source,
                    unbound_thread_counts,
                )
                for snap in snapshots
            ]
        else:
            tasks = [
                asyncio.to_thread(
                    provider.enrich_repo,
                    snap,
                    chat_binding_counts,
                    chat_binding_counts_by_source,
                )
                for snap in snapshots
            ]
        return cast(list[dict[str, Any]], await asyncio.gather(*tasks))

    def _active_chat_binding_counts_by_source(self) -> dict[str, dict[str, int]]:
        try:
            return active_chat_binding_counts_by_source(
                hub_root=self._context.config.root,
                raw_config=self._context.config.raw,
            )
        except Exception as exc:
            safe_log(
                self._context.logger,
                logging.WARNING,
                "Hub source chat-bound worktree lookup failed",
                exc=exc,
            )
            return {}

    async def _unbound_thread_counts_snapshot(self) -> Optional[dict[str, int]]:
        snapshot_fn = getattr(
            self._require_repo_projection_provider(),
            "unbound_repo_thread_counts_snapshot",
            None,
        )
        if not callable(snapshot_fn):
            return None
        return cast(dict[str, int], await asyncio.to_thread(snapshot_fn))

    def _store_response_cache(
        self,
        *,
        cache_key: tuple[str, ...],
        fingerprint: tuple[Any, ...],
        payload: dict[str, Any],
    ) -> None:
        with self._response_cache_lock:
            self._response_cache[cache_key] = _RepoListingCacheEntry(
                fingerprint=fingerprint,
                expires_at=_monotonic() + _REPO_LISTING_RESPONSE_CACHE_TTL_SECONDS,
                payload=copy.deepcopy(payload),
            )

    async def _current_topology_snapshots(
        self, *, needs_repos: bool, needs_agent_workspaces: bool
    ) -> tuple[list[Any], list[Any]]:
        supervisor_state = getattr(self._context.supervisor, "state", None)
        snapshots = list(getattr(supervisor_state, "repos", []) or [])
        agent_workspaces = list(getattr(supervisor_state, "agent_workspaces", []) or [])
        if needs_repos and not snapshots:
            snapshots = list(
                await asyncio.to_thread(self._context.supervisor.list_repos)
            )
            supervisor_state = getattr(self._context.supervisor, "state", None)
            agent_workspaces = list(
                getattr(supervisor_state, "agent_workspaces", []) or agent_workspaces
            )
        if needs_agent_workspaces and not agent_workspaces:
            agent_workspaces = list(
                await asyncio.to_thread(self._context.supervisor.list_agent_workspaces)
            )
        return snapshots, agent_workspaces

    def _force_list_repos(self) -> list[Any]:
        try:
            return list(self._context.supervisor.list_repos(use_cache=False))
        except TypeError:
            return list(self._context.supervisor.list_repos())

    def _force_list_agent_workspaces(self) -> list[Any]:
        try:
            return list(self._context.supervisor.list_agent_workspaces(use_cache=False))
        except TypeError:
            return list(self._context.supervisor.list_agent_workspaces())

    async def _load_topology_snapshots(
        self,
        *,
        needs_repos: bool,
        needs_agent_workspaces: bool,
        force_refresh: bool,
    ) -> tuple[list[Any], list[Any]]:
        if force_refresh:
            if needs_repos:
                snapshots = await asyncio.to_thread(self._force_list_repos)
                supervisor_state = getattr(self._context.supervisor, "state", None)
                agent_workspaces = list(
                    getattr(supervisor_state, "agent_workspaces", []) or []
                )
                if needs_agent_workspaces and not agent_workspaces:
                    agent_workspaces = await asyncio.to_thread(
                        self._force_list_agent_workspaces
                    )
                return list(snapshots), list(agent_workspaces)
            if needs_agent_workspaces:
                agent_workspaces = await asyncio.to_thread(
                    self._force_list_agent_workspaces
                )
                return [], list(agent_workspaces)
        return await self._current_topology_snapshots(
            needs_repos=needs_repos,
            needs_agent_workspaces=needs_agent_workspaces,
        )

    def _schedule_response_refresh(
        self,
        *,
        cache_key: tuple[str, ...],
        requested: set[str],
    ) -> None:
        with self._response_refresh_tasks_lock:
            task = self._response_refresh_tasks.get(cache_key)
            if task is not None and not task.done():
                return

            async def _refresh() -> None:
                needs_repos = bool(requested & {"repos", "freshness"})
                needs_agent_workspaces = bool(
                    requested & {"agent_workspaces", "freshness"}
                )
                snapshots, agent_workspaces = await self._load_topology_snapshots(
                    needs_repos=needs_repos,
                    needs_agent_workspaces=needs_agent_workspaces,
                    force_refresh=True,
                )
                stale_threshold_seconds = resolve_stale_threshold_seconds(
                    getattr(
                        self._context.config.pma,
                        "freshness_stale_threshold_seconds",
                        None,
                    )
                )
                refreshed_fingerprint = self._listing_fingerprint(
                    requested=requested,
                    stale_threshold_seconds=stale_threshold_seconds,
                    repos=snapshots,
                    agent_workspaces=agent_workspaces,
                )
                payload = await self._build_listing_payload(
                    sections=requested,
                    snapshots=snapshots,
                    agent_workspaces=agent_workspaces,
                )
                self._store_response_cache(
                    cache_key=cache_key,
                    fingerprint=refreshed_fingerprint,
                    payload=payload,
                )
                projection_store = self._projection_store()
                if projection_store is not None:
                    try:
                        projection_store.set_cache(
                            f"hub_listing:{','.join(cache_key)}",
                            refreshed_fingerprint,
                            payload,
                            namespace=HUB_LISTING_PROJECTION_NAMESPACE,
                        )
                    except Exception:
                        pass

            task = asyncio.create_task(_refresh())
            self._response_refresh_tasks[cache_key] = task

        def _cleanup(done_task: asyncio.Task[None]) -> None:
            with self._response_refresh_tasks_lock:
                current = self._response_refresh_tasks.get(cache_key)
                if current is done_task:
                    self._response_refresh_tasks.pop(cache_key, None)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                safe_log(
                    self._context.logger,
                    logging.WARNING,
                    "Hub list_repos background refresh failed",
                    exc=exc,
                )

        task.add_done_callback(_cleanup)

    async def _build_listing_payload(
        self,
        *,
        sections: Optional[set[str]] = None,
        snapshots: Optional[list[Any]] = None,
        agent_workspaces: Optional[list[Any]] = None,
    ) -> dict[str, Any]:
        safe_log(self._context.logger, logging.INFO, "Hub list_repos")
        requested = set(sections or REPO_LISTING_SECTIONS)
        needs_repos = bool(requested & {"repos", "freshness"})
        needs_agent_workspaces = bool(requested & {"agent_workspaces", "freshness"})
        snapshots = list(snapshots or [])
        repos: list[dict[str, Any]] = []
        agent_workspaces = list(agent_workspaces or [])
        if needs_repos:
            if not snapshots or (needs_agent_workspaces and not agent_workspaces):
                snapshots, agent_workspaces = await self._current_topology_snapshots(
                    needs_repos=True,
                    needs_agent_workspaces=needs_agent_workspaces,
                )
            await self._prepare_snapshots(snapshots)
            chat_binding_counts_by_source = await asyncio.to_thread(
                self._active_chat_binding_counts_by_source
            )
            chat_binding_counts = {
                repo_id: sum(source_counts.values())
                for repo_id, source_counts in chat_binding_counts_by_source.items()
            }
            unbound_thread_counts = await self._unbound_thread_counts_snapshot()
            repos = await self._enrich_repos(
                snapshots,
                chat_binding_counts,
                chat_binding_counts_by_source,
                unbound_thread_counts,
            )
            if needs_agent_workspaces:
                agent_workspaces = [
                    workspace.to_dict(self._context.config.root)
                    for workspace in agent_workspaces
                ]
        elif needs_agent_workspaces:
            if not agent_workspaces:
                _, agent_workspace_snaps = await self._current_topology_snapshots(
                    needs_repos=False,
                    needs_agent_workspaces=True,
                )
                agent_workspaces = [
                    workspace.to_dict(self._context.config.root)
                    for workspace in agent_workspace_snaps
                ]
            else:
                agent_workspaces = [
                    workspace.to_dict(self._context.config.root)
                    for workspace in agent_workspaces
                ]

        generated_at = iso_now()
        stale_threshold_seconds = resolve_stale_threshold_seconds(
            getattr(
                self._context.config.pma,
                "freshness_stale_threshold_seconds",
                None,
            )
        )
        payload: dict[str, Any] = {
            "generated_at": generated_at,
            "last_scan_at": self._context.supervisor.state.last_scan_at,
            "pinned_parent_repo_ids": self._context.supervisor.state.pinned_parent_repo_ids,
        }
        if "freshness" in requested:
            payload["freshness"] = {
                "schema_version": 1,
                "generated_at": generated_at,
                "stale_threshold_seconds": stale_threshold_seconds,
                "sections": {
                    "repos": summarize_section_freshness(
                        repos,
                        generated_at=generated_at,
                        stale_threshold_seconds=stale_threshold_seconds,
                        extractor=lambda item: (
                            (item.get("canonical_state_v1") or {}).get("freshness")
                            if isinstance(item, dict)
                            else None
                        ),
                    ),
                    "agent_workspaces": summarize_section_freshness(
                        agent_workspaces,
                        generated_at=generated_at,
                        stale_threshold_seconds=stale_threshold_seconds,
                        extractor=lambda _item: None,
                    ),
                },
            }
        if "repos" in requested:
            payload["repos"] = repos
        if "agent_workspaces" in requested:
            payload["agent_workspaces"] = agent_workspaces
        return payload

    async def list_repos(
        self, *, sections: Optional[set[str]] = None
    ) -> dict[str, Any]:
        self._require_repo_projection_provider()
        requested = set(sections or REPO_LISTING_SECTIONS)
        cache_key = tuple(sorted(requested))
        durable_cache_key = f"hub_listing:{','.join(cache_key)}"
        needs_repos = bool(requested & {"repos", "freshness"})
        needs_agent_workspaces = bool(requested & {"agent_workspaces", "freshness"})
        snapshots, agent_workspaces = await self._load_topology_snapshots(
            needs_repos=needs_repos,
            needs_agent_workspaces=needs_agent_workspaces,
            force_refresh=False,
        )
        supervisor_state = getattr(self._context.supervisor, "state", None)
        if (
            needs_repos
            and not snapshots
            and getattr(supervisor_state, "last_scan_at", None) is None
        ):
            snapshots = await asyncio.to_thread(self._context.supervisor.scan)
            if needs_agent_workspaces:
                agent_workspaces = await asyncio.to_thread(
                    self._context.supervisor.list_agent_workspaces,
                    use_cache=False,
                )
        stale_threshold_seconds = resolve_stale_threshold_seconds(
            getattr(
                self._context.config.pma,
                "freshness_stale_threshold_seconds",
                None,
            )
        )
        fingerprint = self._listing_fingerprint(
            requested=requested,
            stale_threshold_seconds=stale_threshold_seconds,
            repos=snapshots,
            agent_workspaces=agent_workspaces,
        )
        now = _monotonic()
        with self._response_cache_lock:
            cached = self._response_cache.get(cache_key)
        if cached is not None and cached.fingerprint == fingerprint:
            if cached.expires_at > now:
                return copy.deepcopy(cached.payload)
            self._schedule_response_refresh(cache_key=cache_key, requested=requested)
            return copy.deepcopy(cached.payload)
        snapshots, agent_workspaces = await self._load_topology_snapshots(
            needs_repos=needs_repos,
            needs_agent_workspaces=needs_agent_workspaces,
            force_refresh=True,
        )
        fingerprint = self._listing_fingerprint(
            requested=requested,
            stale_threshold_seconds=stale_threshold_seconds,
            repos=snapshots,
            agent_workspaces=agent_workspaces,
        )
        projection_store = self._projection_store()
        if projection_store is not None:
            try:
                cached = projection_store.get_cache(
                    durable_cache_key,
                    fingerprint,
                    max_age_seconds=_HUB_LISTING_PROJECTION_MAX_AGE_SECONDS,
                    namespace=HUB_LISTING_PROJECTION_NAMESPACE,
                )
            except Exception:
                cached = None
            if cached is not None:
                payload = cast(dict[str, Any], cached)
                self._store_response_cache(
                    cache_key=cache_key,
                    fingerprint=fingerprint,
                    payload=payload,
                )
                return copy.deepcopy(payload)
        payload = await self._build_listing_payload(
            sections=requested,
            snapshots=snapshots,
            agent_workspaces=agent_workspaces,
        )
        self._store_response_cache(
            cache_key=cache_key,
            fingerprint=fingerprint,
            payload=payload,
        )
        if projection_store is not None:
            try:
                projection_store.set_cache(
                    durable_cache_key,
                    fingerprint,
                    payload,
                    namespace=HUB_LISTING_PROJECTION_NAMESPACE,
                )
            except Exception:
                pass
        return payload

    async def scan_repos(self) -> dict[str, Any]:
        self._require_repo_projection_provider()
        safe_log(self._context.logger, logging.INFO, "Hub scan_repos")
        snapshots = await asyncio.to_thread(self._context.supervisor.scan)
        agent_workspace_snapshots = await asyncio.to_thread(
            self._context.supervisor.list_agent_workspaces, use_cache=False
        )
        await self._prepare_snapshots(list(snapshots))
        chat_binding_counts_by_source = await asyncio.to_thread(
            self._active_chat_binding_counts_by_source
        )
        chat_binding_counts = {
            repo_id: sum(source_counts.values())
            for repo_id, source_counts in chat_binding_counts_by_source.items()
        }
        unbound_thread_counts = await self._unbound_thread_counts_snapshot()
        repos = await self._enrich_repos(
            snapshots,
            chat_binding_counts,
            chat_binding_counts_by_source,
            unbound_thread_counts,
        )
        agent_workspaces = [
            workspace.to_dict(self._context.config.root)
            for workspace in agent_workspace_snapshots
        ]
        generated_at = iso_now()
        stale_threshold_seconds = resolve_stale_threshold_seconds(
            getattr(
                self._context.config.pma,
                "freshness_stale_threshold_seconds",
                None,
            )
        )
        payload = {
            "generated_at": generated_at,
            "last_scan_at": self._context.supervisor.state.last_scan_at,
            "pinned_parent_repo_ids": self._context.supervisor.state.pinned_parent_repo_ids,
            "freshness": {
                "schema_version": 1,
                "generated_at": generated_at,
                "stale_threshold_seconds": stale_threshold_seconds,
                "sections": {
                    "repos": summarize_section_freshness(
                        repos,
                        generated_at=generated_at,
                        stale_threshold_seconds=stale_threshold_seconds,
                        extractor=lambda item: (
                            (item.get("canonical_state_v1") or {}).get("freshness")
                            if isinstance(item, dict)
                            else None
                        ),
                    ),
                    "agent_workspaces": summarize_section_freshness(
                        agent_workspaces,
                        generated_at=generated_at,
                        stale_threshold_seconds=stale_threshold_seconds,
                        extractor=lambda _item: None,
                    ),
                },
            },
            "repos": repos,
            "agent_workspaces": agent_workspaces,
        }
        listing_fingerprint = self._listing_fingerprint(
            requested=set(REPO_LISTING_SECTIONS),
            stale_threshold_seconds=stale_threshold_seconds,
            repos=snapshots,
            agent_workspaces=agent_workspace_snapshots,
        )
        self._store_response_cache(
            cache_key=tuple(sorted(REPO_LISTING_SECTIONS)),
            fingerprint=listing_fingerprint,
            payload=payload,
        )
        projection_store = self._projection_store()
        if projection_store is not None:
            try:
                projection_store.set_cache(
                    "hub_listing:agent_workspaces,freshness,repos",
                    listing_fingerprint,
                    payload,
                    namespace=HUB_LISTING_PROJECTION_NAMESPACE,
                )
            except Exception:
                pass
        return payload

    def invalidate_listing_response_cache(self) -> None:
        with self._response_cache_lock:
            self._response_cache.clear()
        with self._response_refresh_tasks_lock:
            tasks = list(self._response_refresh_tasks.values())
            self._response_refresh_tasks.clear()
        for task in tasks:
            task.cancel()
        projection_store = self._projection_store()
        if projection_store is not None:
            try:
                projection_store.delete(namespace=HUB_LISTING_PROJECTION_NAMESPACE)
            except Exception:
                pass

    def invalidate_message_snapshot_cache(
        self,
        *,
        include_repo_capability_hints: bool = False,
    ) -> None:
        with _hub_snapshot_cache_lock:
            context_id = id(self._context)
            stale_keys = [
                cache_key
                for cache_key in _hub_snapshot_cache
                if cache_key[0] == context_id
            ]
            for cache_key in stale_keys:
                _hub_snapshot_cache.pop(cache_key, None)
        if include_repo_capability_hints:
            with _repo_capability_hint_cache_lock:
                root = getattr(getattr(self._context, "config", None), "root", None)
                root_key = str(root) if isinstance(root, Path) else ""
                stale_hint_keys = [
                    hint_cache_key
                    for hint_cache_key in _repo_capability_hint_cache
                    if hint_cache_key[0] == root_key
                ]
                for hint_cache_key in stale_hint_keys:
                    _repo_capability_hint_cache.pop(hint_cache_key, None)
        projection_store = self._projection_store()
        if projection_store is not None:
            try:
                projection_store.delete(namespace=HUB_SNAPSHOT_PROJECTION_NAMESPACE)
            except Exception:
                pass
            if include_repo_capability_hints:
                try:
                    projection_store.delete(
                        namespace=REPO_CAPABILITY_HINT_PROJECTION_NAMESPACE
                    )
                except Exception:
                    pass

    def gather_message_snapshot(
        self,
        *,
        limit: int = 100,
        scope_key: Optional[str] = None,
        sections: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        requested = (
            set(sections)
            if sections is not None
            else {
                "inbox",
                "pma_threads",
                "pma_files_detail",
                "automation",
                "action_queue",
            }
        )
        settings = _build_snapshot_settings(self._context, requested)
        cache_key = (id(self._context), "|".join(sorted(requested)))
        fingerprint = _hub_snapshot_fingerprint(
            self._context,
            limit=limit,
            scope_key=scope_key,
            requested=requested,
        )
        now = _monotonic()
        with _hub_snapshot_cache_lock:
            cached = _hub_snapshot_cache.get(cache_key)
            if (
                cached is not None
                and cached.expires_at > now
                and cached.fingerprint == fingerprint
            ):
                return copy.deepcopy(cached.snapshot)
        durable_cache_key = (
            f"hub_snapshot:{','.join(sorted(requested))}:{limit}:{scope_key or ''}"
        )
        projection_store = self._projection_store()
        if projection_store is not None:
            try:
                durable_cached = projection_store.get_cache(
                    durable_cache_key,
                    fingerprint,
                    max_age_seconds=_HUB_SNAPSHOT_PROJECTION_MAX_AGE_SECONDS,
                    namespace=HUB_SNAPSHOT_PROJECTION_NAMESPACE,
                )
            except Exception:
                durable_cached = None
            if durable_cached is not None:
                with _hub_snapshot_cache_lock:
                    _hub_snapshot_cache[cache_key] = _HubSnapshotCacheEntry(
                        fingerprint=fingerprint,
                        expires_at=now + _HUB_SNAPSHOT_CACHE_TTL_SECONDS,
                        snapshot=copy.deepcopy(cast(dict[str, Any], durable_cached)),
                    )
                return copy.deepcopy(cast(dict[str, Any], durable_cached))

        inbox: list[dict[str, Any]] = []
        if (
            settings.include_inbox_queue_metadata
            or settings.include_full_action_queue_context
        ):
            inbox = _gather_inbox(
                self._context.supervisor,
                max_text_chars=settings.max_text_chars,
                stale_threshold_seconds=settings.stale_threshold_seconds,
            )

        hub_root = getattr(getattr(self._context, "config", None), "root", None)
        pma_files_detail: dict[str, list[dict[str, Any]]] = empty_listing()
        pma_threads: list[dict[str, Any]] = []
        automation = (
            _snapshot_pma_automation(self._context.supervisor)
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
            if (
                requested & {"pma_threads"}
                or settings.include_full_action_queue_context
            ):
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

        repo_context, repo_diagnostics = _load_repo_message_context(
            self._context, requested
        )
        unreadable_diagnostics: list[dict[str, str]] = list(repo_diagnostics)
        filtered_action_queue = _filter_action_queue_items(
            action_queue,
            repo_context=repo_context,
            scope_key=scope_key,
        )
        messages = _collect_inbox_messages(
            self._context,
            requested=requested,
            limit=limit,
            scope_key=scope_key,
            repo_context=repo_context,
            filtered_action_queue=filtered_action_queue,
            unreadable_diagnostics=unreadable_diagnostics,
        )
        snapshot = _serialize_hub_snapshot(
            generated_at=settings.generated_at,
            requested=requested,
            messages=messages,
            pma_threads=pma_threads,
            pma_files_detail=pma_files_detail,
            automation=automation,
            filtered_action_queue=filtered_action_queue,
            unreadable_diagnostics=unreadable_diagnostics,
        )
        with _hub_snapshot_cache_lock:
            _hub_snapshot_cache[cache_key] = _HubSnapshotCacheEntry(
                fingerprint=fingerprint,
                expires_at=now + _HUB_SNAPSHOT_CACHE_TTL_SECONDS,
                snapshot=copy.deepcopy(snapshot),
            )
        if projection_store is not None:
            try:
                projection_store.set_cache(
                    durable_cache_key,
                    fingerprint,
                    snapshot,
                    namespace=HUB_SNAPSHOT_PROJECTION_NAMESPACE,
                )
            except Exception:
                pass
        return snapshot


def get_hub_read_model_service(
    context: Any,
    *,
    repo_projection_provider: Optional[RepoProjectionProvider] = None,
    prepare_repo_snapshots: Optional[Callable[[list[Any]], Any]] = None,
) -> HubReadModelService:
    with _service_registry_lock:
        service = _service_registry.get(id(context))
        if service is None:
            service = HubReadModelService(
                context,
                repo_projection_provider=repo_projection_provider,
                prepare_repo_snapshots=prepare_repo_snapshots,
            )
            _service_registry[id(context)] = service
        else:
            service.bind_repo_projection_provider(
                repo_projection_provider=repo_projection_provider,
                prepare_repo_snapshots=prepare_repo_snapshots,
            )
        return service


def invalidate_hub_message_snapshot_cache(
    context: Optional[Any] = None,
    *,
    include_repo_capability_hints: bool = False,
) -> None:
    if context is None:
        with _hub_snapshot_cache_lock:
            _hub_snapshot_cache.clear()
        if include_repo_capability_hints:
            with _repo_capability_hint_cache_lock:
                _repo_capability_hint_cache.clear()
        return
    service = get_hub_read_model_service(context)
    service.invalidate_message_snapshot_cache(
        include_repo_capability_hints=include_repo_capability_hints
    )


__all__ = [
    "HUB_LISTING_PROJECTION_NAMESPACE",
    "HUB_SNAPSHOT_PROJECTION_NAMESPACE",
    "REPO_CAPABILITY_HINT_PROJECTION_NAMESPACE",
    "REPO_LISTING_SECTIONS",
    "HubReadModelService",
    "RepoProjectionProvider",
    "_HubSnapshotCacheEntry",
    "_RepoCapabilityHintCacheEntry",
    "_collect_pma_files_detail",
    "_collect_pma_threads",
    "_gather_inbox",
    "_hub_snapshot_cache",
    "_repo_capability_hint_cache",
    "_snapshot_pma_automation",
    "build_hub_capability_hints",
    "build_repo_capability_hints",
    "get_hub_read_model_service",
    "invalidate_hub_message_snapshot_cache",
    "latest_dispatch",
    "load_hub_inbox_dismissals",
]
