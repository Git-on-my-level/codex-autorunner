from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core.pr_binding_runtime import (
    thread_contexts,
    thread_head_branch_hint,
)
from ...core.pr_bindings import PrBinding
from ...core.scm_polling_watches import ScmPollingWatch
from ...core.text_utils import _mapping, _normalize_positive_int, _normalize_text

_ACTIVE_PR_STATES = frozenset({"open", "draft"})
_VALID_PR_STATES = frozenset({"open", "draft", "closed", "merged"})
_HOT_THREAD_WINDOW_MINUTES = 60
_RECENT_THREAD_WINDOW_MINUTES = 24 * 60
_PR_HINT_METADATA_KEYS = ("pr_number", "pr_url", "pull_request_url", "pr_ref")
_GITHUB_PR_URL_HINT_RE = re.compile(
    r"(?:https?://github\.com/|(?<![\w.-])github\.com/)"
    r"[^/\s]{1,200}/[^/\s]{1,200}/pull/\d+",
    re.IGNORECASE,
)


def _normalize_lower_text(value: Any) -> Optional[str]:
    text = _normalize_text(value)
    return text.lower() if text is not None else None


def _text_has_github_pull_request_url(value: str) -> bool:
    return _GITHUB_PR_URL_HINT_RE.search(value) is not None


def _pr_hint_present_in_context(context: Mapping[str, Any]) -> bool:
    for key in _PR_HINT_METADATA_KEYS:
        raw = context.get(key)
        if key == "pr_number":
            if isinstance(raw, bool):
                continue
            if isinstance(raw, int) and raw > 0:
                return True
            if _normalize_text(raw) is not None:
                return True
            continue
        if _normalize_text(raw) is not None:
            return True
    return False


def thread_has_pr_open_hint(thread: Mapping[str, Any]) -> bool:
    metadata = _mapping(thread.get("metadata"))
    for context in thread_contexts(metadata):
        if _pr_hint_present_in_context(context):
            return True
    for key in ("status_reason_code", "status_reason", "last_message_preview"):
        value = _normalize_lower_text(thread.get(key))
        if value is None:
            continue
        if "pull request" in value or "gh pr create" in value:
            return True
        if _text_has_github_pull_request_url(value):
            return True
    return False


def thread_activity_timestamp(
    thread: Mapping[str, Any],
    *,
    parse_optional_iso: Any,
) -> Optional[datetime]:
    return max(
        (
            timestamp
            for timestamp in (
                parse_optional_iso(thread.get("status_updated_at")),
                parse_optional_iso(thread.get("updated_at")),
                parse_optional_iso(thread.get("created_at")),
            )
            if timestamp is not None
        ),
        default=None,
    )


def is_recent_terminal_thread_candidate(
    thread: Mapping[str, Any],
    *,
    cutoff: datetime,
    parse_optional_iso: Any,
) -> bool:
    if not bool(thread.get("status_terminal")):
        return False
    activity_at = thread_activity_timestamp(
        thread,
        parse_optional_iso=parse_optional_iso,
    )
    if activity_at is None or activity_at < cutoff:
        return False
    if thread_has_pr_open_hint(thread):
        return True
    status_reason = (
        _normalize_lower_text(thread.get("status_reason_code"))
        or _normalize_lower_text(thread.get("status_reason"))
        or ""
    )
    if status_reason in {"managed_turn_completed", "completed"}:
        return thread_head_branch_hint(thread) is not None
    return False


def _activity_sort_key(
    timestamp: Optional[datetime],
    *,
    now: datetime,
) -> tuple[int, float]:
    if timestamp is None:
        return (1, 0.0)
    recent_cutoff = now - timedelta(minutes=_RECENT_THREAD_WINDOW_MINUTES)
    if timestamp >= recent_cutoff:
        return (0, -timestamp.timestamp())
    return (2, -timestamp.timestamp())


def _rotated(items: list[Path], *, offset: int) -> list[Path]:
    if not items:
        return []
    normalized_offset = offset % len(items)
    if normalized_offset == 0:
        return list(items)
    return list(items[normalized_offset:]) + list(items[:normalized_offset])


def binding_from_polling_row(row: sqlite3.Row) -> PrBinding:
    binding_id = _normalize_text(row["binding_id"])
    provider = _normalize_text(row["provider"])
    repo_slug = _normalize_text(row["repo_slug"])
    pr_number = _normalize_positive_int(row["pr_number"])
    pr_state = _normalize_lower_text(row["pr_state"])
    created_at = _normalize_text(row["created_at"])
    updated_at = _normalize_text(row["updated_at"])
    if binding_id is None:
        raise ValueError("binding_id is required")
    if provider is None:
        raise ValueError("provider is required")
    if repo_slug is None:
        raise ValueError("repo_slug is required")
    if pr_number is None:
        raise ValueError("pr_number must be > 0")
    if pr_state not in _VALID_PR_STATES:
        raise ValueError("pr_state must be a valid PR state")
    if created_at is None or updated_at is None:
        raise ValueError("binding timestamps are required")
    return PrBinding(
        binding_id=binding_id,
        provider=provider,
        repo_slug=repo_slug,
        pr_number=pr_number,
        pr_state=pr_state,
        created_at=created_at,
        updated_at=updated_at,
        repo_id=_normalize_text(row["repo_id"]),
        head_branch=_normalize_text(row["head_branch"]),
        base_branch=_normalize_text(row["base_branch"]),
        thread_target_id=_normalize_text(row["thread_target_id"]),
        closed_at=_normalize_text(row["closed_at"]),
    )


def compute_activity_tier(
    *,
    binding: PrBinding,
    workspace_root: Path,
    watch: Optional[ScmPollingWatch],
    thread_activity_by_thread: Mapping[str, datetime],
    workspace_activity: Mapping[str, datetime],
    no_activity_tier: str,
    now: datetime,
) -> str:
    activity_at: Optional[datetime] = None
    if binding.thread_target_id is not None:
        activity_at = thread_activity_by_thread.get(binding.thread_target_id)
    if activity_at is None and watch is not None and watch.thread_target_id is not None:
        activity_at = thread_activity_by_thread.get(watch.thread_target_id)
    if activity_at is None:
        activity_at = workspace_activity.get(str(workspace_root.resolve()))
    if activity_at is None:
        return no_activity_tier
    if activity_at >= now - timedelta(minutes=_HOT_THREAD_WINDOW_MINUTES):
        return "hot"
    if activity_at >= now - timedelta(minutes=_RECENT_THREAD_WINDOW_MINUTES):
        return "warm"
    return "cold"


def compute_poll_interval_for_tier(
    *,
    activity_tier: str,
    base_interval_seconds: int,
) -> int:
    _WARM_INTERVAL_SECONDS_FLOOR = 15 * 60
    _COLD_INTERVAL_SECONDS_FLOOR = 60 * 60
    if activity_tier == "hot":
        return base_interval_seconds
    if activity_tier == "cold":
        return max(
            base_interval_seconds * 40,
            _COLD_INTERVAL_SECONDS_FLOOR,
        )
    return max(
        base_interval_seconds * 10,
        _WARM_INTERVAL_SECONDS_FLOOR,
    )


def resolve_workspace_root_for_binding(
    *,
    binding: PrBinding,
    existing_watch: Optional[ScmPollingWatch],
    candidate_roots: list[Path],
    workspaces_by_repo_id: Mapping[str, list[Path]],
    workspaces_by_thread_id: Mapping[str, Path],
    repo_slug_cache: dict[str, Optional[str]],
    github_service_factory: Any,
    raw_config: Any,
) -> Optional[Path]:
    if existing_watch is not None:
        existing_watch_root = Path(existing_watch.workspace_root).resolve()
        if existing_watch_root.exists() and existing_watch_root.is_dir():
            return existing_watch_root

    if binding.thread_target_id is not None:
        thread_root = workspaces_by_thread_id.get(binding.thread_target_id)
        if thread_root is not None:
            return thread_root

    if binding.repo_id is not None:
        repo_roots = workspaces_by_repo_id.get(binding.repo_id) or []
        if repo_roots:
            return repo_roots[0]

    for candidate_root in candidate_roots:
        candidate_key = str(candidate_root)
        if candidate_key not in repo_slug_cache:
            try:
                github = github_service_factory(
                    candidate_root,
                    raw_config if isinstance(raw_config, dict) else None,
                )
                repo_slug_cache[candidate_key] = _normalize_text(
                    github.repo_info().name_with_owner
                )
            except Exception:
                repo_slug_cache[candidate_key] = None
        if repo_slug_cache.get(candidate_key) == binding.repo_slug:
            return candidate_root
    return None


def compute_thread_activity(
    threads: list[dict[str, Any]],
    *,
    parse_optional_iso: Any,
) -> tuple[dict[str, datetime], dict[str, datetime]]:
    by_thread: dict[str, datetime] = {}
    by_workspace: dict[str, datetime] = {}
    for thread in threads:
        activity_at = max(
            (
                timestamp
                for timestamp in (
                    parse_optional_iso(thread.get("status_updated_at")),
                    parse_optional_iso(thread.get("updated_at")),
                )
                if timestamp is not None
            ),
            default=None,
        )
        if activity_at is None:
            continue
        thread_target_id = _normalize_text(thread.get("managed_thread_id"))
        if thread_target_id is not None:
            prior_thread_activity = by_thread.get(thread_target_id)
            if prior_thread_activity is None or activity_at > prior_thread_activity:
                by_thread[thread_target_id] = activity_at
        workspace_root = _normalize_text(thread.get("workspace_root"))
        if workspace_root is not None:
            workspace_key = str(Path(workspace_root).resolve())
            prior_workspace_activity = by_workspace.get(workspace_key)
            if (
                prior_workspace_activity is None
                or activity_at > prior_workspace_activity
            ):
                by_workspace[workspace_key] = activity_at
    return by_thread, by_workspace


def prioritized_discovery_roots(
    *,
    candidate_roots: list[Path],
    workspace_activity: Mapping[str, datetime],
    discovery_interval_seconds: int,
    discovery_limit: int,
    now: datetime,
) -> list[Path]:
    if len(candidate_roots) <= 1:
        return list(candidate_roots)
    grouped: dict[int, list[Path]] = {0: [], 1: [], 2: []}
    for root in candidate_roots:
        activity_key = _activity_sort_key(
            workspace_activity.get(str(root.resolve())),
            now=now,
        )
        grouped.setdefault(activity_key[0], []).append(root)
    cycle_index = int(now.timestamp()) // max(1, discovery_interval_seconds)
    rotation_offset = cycle_index * max(1, discovery_limit)
    ordered: list[Path] = []
    for group_key in sorted(grouped):
        bucket = grouped[group_key]
        ordered.extend(_rotated(bucket, offset=rotation_offset))
    return ordered


def collect_candidate_workspace_roots(
    *,
    hub_root: Path,
    raw_config: Any,
    active_watch_workspace_candidates_fn: Any,
    active_bindings_fn: Any,
    now: datetime,
    parse_optional_iso: Any,
    polling_config: Any,
) -> tuple[list[Path], dict[str, list[Path]], dict[str, Path], dict[Path, str]]:
    from ...core.pma_thread_store import PmaThreadStore
    from ...core.pr_binding_runtime import thread_head_branch_hint

    roots: list[Path] = []
    seen_roots: set[Path] = set()
    workspaces_by_repo_id: dict[str, list[Path]] = {}
    workspaces_by_thread_id: dict[str, Path] = {}
    workspace_branch_hints: dict[Path, str] = {}

    def add_root(
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        thread_target_id: Optional[str] = None,
        branch_hint: Optional[str] = None,
    ) -> None:
        resolved_root = workspace_root.resolve()
        if not resolved_root.exists() or not resolved_root.is_dir():
            return
        if resolved_root not in seen_roots:
            seen_roots.add(resolved_root)
            roots.append(resolved_root)
        normalized_repo_id = _normalize_text(repo_id)
        if normalized_repo_id is not None:
            bucket = workspaces_by_repo_id.setdefault(normalized_repo_id, [])
            if resolved_root not in bucket:
                bucket.append(resolved_root)
        normalized_thread_target_id = _normalize_text(thread_target_id)
        if normalized_thread_target_id is not None:
            workspaces_by_thread_id[normalized_thread_target_id] = resolved_root
        normalized_branch_hint = _normalize_text(branch_hint)
        if (
            normalized_branch_hint is not None
            and resolved_root not in workspace_branch_hints
        ):
            workspace_branch_hints[resolved_root] = normalized_branch_hint

    thread_store = PmaThreadStore(hub_root)
    lookback_cutoff = now - timedelta(
        minutes=max(1, polling_config.discovery_terminal_thread_lookback_minutes)
    )
    try:
        active_threads = thread_store.list_threads(status="active", limit=500)
    except Exception:
        active_threads = []
    try:
        terminal_thread_candidates = thread_store.list_threads(limit=500)
    except Exception:
        terminal_thread_candidates = []

    for thread in active_threads:
        workspace_root = _normalize_text(thread.get("workspace_root"))
        if workspace_root is None:
            continue
        lifecycle_status = _normalize_lower_text(thread.get("lifecycle_status"))
        include_active_thread = lifecycle_status == "active" and not bool(
            thread.get("status_terminal")
        )
        if not include_active_thread:
            continue
        add_root(
            Path(workspace_root),
            repo_id=_normalize_text(thread.get("repo_id")),
            thread_target_id=_normalize_text(thread.get("managed_thread_id")),
            branch_hint=thread_head_branch_hint(thread),
        )

    for thread in terminal_thread_candidates:
        workspace_root = _normalize_text(thread.get("workspace_root"))
        if workspace_root is None:
            continue
        lifecycle_status = _normalize_lower_text(thread.get("lifecycle_status"))
        if lifecycle_status == "active" and not bool(thread.get("status_terminal")):
            continue
        if not is_recent_terminal_thread_candidate(
            thread,
            cutoff=lookback_cutoff,
            parse_optional_iso=parse_optional_iso,
        ):
            continue
        add_root(
            Path(workspace_root),
            repo_id=_normalize_text(thread.get("repo_id")),
            thread_target_id=_normalize_text(thread.get("managed_thread_id")),
            branch_hint=thread_head_branch_hint(thread),
        )

    for (
        workspace_root,
        repo_id,
        thread_target_id,
    ) in active_watch_workspace_candidates_fn(limit=1000):
        if not workspace_root:
            continue
        add_root(
            Path(workspace_root),
            repo_id=repo_id,
            thread_target_id=thread_target_id,
        )

    active_bindings, _ = active_bindings_fn(limit=2000)
    for binding in active_bindings.values():
        if binding.thread_target_id is None:
            continue
        if binding.thread_target_id in workspaces_by_thread_id:
            continue
        bound_thread = thread_store.get_thread(binding.thread_target_id)
        if not isinstance(bound_thread, dict):
            continue
        workspace_root = _normalize_text(bound_thread.get("workspace_root"))
        if workspace_root is None:
            continue
        add_root(
            Path(workspace_root),
            repo_id=_normalize_text(bound_thread.get("repo_id")) or binding.repo_id,
            thread_target_id=binding.thread_target_id,
            branch_hint=thread_head_branch_hint(bound_thread) or binding.head_branch,
        )

    if polling_config.discovery_include_manifest_repos:
        from ...manifest import ManifestError, load_manifest

        manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
        if manifest_path.exists():
            try:
                manifest = load_manifest(manifest_path, hub_root)
            except ManifestError:
                manifest = None
            if manifest is not None:
                for repo in manifest.repos:
                    if not repo.enabled:
                        continue
                    add_root(hub_root / repo.path, repo_id=repo.id)
    return (
        roots,
        workspaces_by_repo_id,
        workspaces_by_thread_id,
        workspace_branch_hints,
    )


__all__ = [
    "binding_from_polling_row",
    "collect_candidate_workspace_roots",
    "compute_activity_tier",
    "compute_poll_interval_for_tier",
    "compute_thread_activity",
    "is_recent_terminal_thread_candidate",
    "prioritized_discovery_roots",
    "resolve_workspace_root_for_binding",
    "thread_activity_timestamp",
    "thread_has_pr_open_hint",
]
