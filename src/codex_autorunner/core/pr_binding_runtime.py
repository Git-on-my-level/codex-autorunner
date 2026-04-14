from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..manifest import ManifestError, load_manifest
from .git_utils import git_branch
from .pma_thread_store import PmaThreadStore
from .pr_bindings import PrBinding, PrBindingStore
from .text_utils import _mapping, _normalize_text

_BRANCH_METADATA_KEYS = ("head_branch", "branch", "git_branch")
_THREAD_TARGET_ID_KEYS = ("thread_target_id", "managed_thread_id")
_CONTEXT_MAPPING_KEYS = ("manual_context", "scm", "scm_context", "context")
_RECENT_TERMINAL_THREAD_LOOKBACK = timedelta(hours=24)
_ACTIVE_PR_STATES = ("open", "draft")


def _parse_timestamp(value: Any) -> Optional[datetime]:
    text = _normalize_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _thread_changed_at(thread: Mapping[str, Any]) -> Optional[datetime]:
    for key in ("status_updated_at", "updated_at", "created_at"):
        parsed = _parse_timestamp(thread.get(key))
        if parsed is not None:
            return parsed
    return None


def find_hub_binding_context(repo_root: Path) -> tuple[Optional[Path], Optional[str]]:
    current = repo_root.resolve()
    while True:
        manifest_path = current / ".codex-autorunner" / "manifest.yml"
        if manifest_path.exists():
            try:
                manifest = load_manifest(manifest_path, current)
            except ManifestError:
                return current, None
            entry = manifest.get_by_path(current, repo_root.resolve())
            repo_id = entry.id.strip() if entry and isinstance(entry.id, str) else None
            return current, repo_id
        parent = current.parent
        if parent == current:
            return None, None
        current = parent


def thread_contexts(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    contexts: list[Mapping[str, Any]] = [payload]
    for key in _CONTEXT_MAPPING_KEYS:
        nested = _mapping(payload.get(key))
        if nested:
            contexts.append(nested)
    return tuple(contexts)


def thread_matches_branch(thread: Mapping[str, Any], *, head_branch: str) -> bool:
    metadata = _mapping(thread.get("metadata"))
    for context in thread_contexts(metadata):
        for key in _BRANCH_METADATA_KEYS:
            candidate = _normalize_text(context.get(key))
            if candidate == head_branch:
                return True
    workspace_root = _normalize_text(thread.get("workspace_root"))
    if workspace_root is None:
        return False
    return _normalize_text(git_branch(Path(workspace_root))) == head_branch


def explicit_thread_target_id(payload: Mapping[str, Any]) -> Optional[str]:
    for context in thread_contexts(payload):
        for key in _THREAD_TARGET_ID_KEYS:
            candidate = _normalize_text(context.get(key))
            if candidate is not None:
                return candidate
    return None


def binding_summary(binding: PrBinding) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "repo_slug": binding.repo_slug,
        "pr_number": binding.pr_number,
        "pr_state": binding.pr_state,
    }
    if binding.head_branch is not None:
        summary["head_branch"] = binding.head_branch
    if binding.base_branch is not None:
        summary["base_branch"] = binding.base_branch
    return summary


def resolve_thread_target_id(
    *,
    hub_root: Path,
    repo_id: Optional[str],
    head_branch: Optional[str],
    existing_binding: Optional[PrBinding] = None,
    thread_target_id: Optional[str] = None,
) -> Optional[str]:
    store = PmaThreadStore(hub_root)
    explicit = _normalize_text(thread_target_id)
    if explicit is not None and store.get_thread(explicit) is not None:
        return explicit
    if existing_binding is not None and existing_binding.thread_target_id is not None:
        return existing_binding.thread_target_id
    if repo_id is None or head_branch is None:
        return None

    for thread in store.list_threads(status="active", repo_id=repo_id, limit=100):
        candidate_thread_id = _normalize_text(thread.get("managed_thread_id"))
        if candidate_thread_id is None:
            continue
        if thread_matches_branch(thread, head_branch=head_branch):
            return candidate_thread_id

    cutoff = datetime.now(timezone.utc) - _RECENT_TERMINAL_THREAD_LOOKBACK
    for thread in store.list_threads(repo_id=repo_id, limit=100):
        candidate_thread_id = _normalize_text(thread.get("managed_thread_id"))
        if candidate_thread_id is None:
            continue
        if not bool(thread.get("status_terminal")):
            continue
        changed_at = _thread_changed_at(thread)
        if changed_at is None or changed_at < cutoff:
            continue
        if thread_matches_branch(thread, head_branch=head_branch):
            return candidate_thread_id
    return None


def upsert_pr_binding(
    hub_root: Path,
    *,
    provider: str,
    repo_slug: str,
    repo_id: Optional[str],
    pr_number: int,
    pr_state: str,
    head_branch: Optional[str] = None,
    base_branch: Optional[str] = None,
    existing_binding: Optional[PrBinding] = None,
    thread_target_id: Optional[str] = None,
) -> PrBinding:
    store = PrBindingStore(hub_root)
    durable_binding = store.get_binding_by_pr(
        provider=provider,
        repo_slug=repo_slug,
        pr_number=pr_number,
    )
    resolved_binding = durable_binding or existing_binding
    resolved_repo_id = repo_id
    if resolved_repo_id is None and resolved_binding is not None:
        resolved_repo_id = resolved_binding.repo_id
    if head_branch is None and resolved_binding is not None:
        head_branch = resolved_binding.head_branch
    if base_branch is None and resolved_binding is not None:
        base_branch = resolved_binding.base_branch
    resolved_thread_target_id = resolve_thread_target_id(
        hub_root=hub_root,
        repo_id=resolved_repo_id,
        head_branch=head_branch,
        existing_binding=resolved_binding,
        thread_target_id=thread_target_id,
    )
    return store.upsert_binding(
        provider=provider,
        repo_slug=repo_slug,
        repo_id=resolved_repo_id,
        pr_number=pr_number,
        pr_state=pr_state,
        head_branch=head_branch,
        base_branch=base_branch,
        thread_target_id=resolved_thread_target_id,
    )


def claim_pr_binding_for_thread(
    hub_root: Path,
    *,
    provider: str,
    repo_slug: str,
    pr_number: int,
    pr_state: str,
    thread_target_id: str,
    repo_id: Optional[str] = None,
    head_branch: Optional[str] = None,
    base_branch: Optional[str] = None,
) -> Optional[PrBinding]:
    store = PrBindingStore(hub_root)
    existing_binding = store.get_binding_by_pr(
        provider=provider,
        repo_slug=repo_slug,
        pr_number=pr_number,
    )
    normalized_thread_target_id = _normalize_text(thread_target_id)
    if normalized_thread_target_id is None:
        return existing_binding
    if (
        existing_binding is not None
        and existing_binding.thread_target_id is not None
        and existing_binding.thread_target_id != normalized_thread_target_id
    ):
        return existing_binding
    return upsert_pr_binding(
        hub_root,
        provider=provider,
        repo_slug=repo_slug,
        repo_id=repo_id,
        pr_number=pr_number,
        pr_state=pr_state,
        head_branch=head_branch,
        base_branch=base_branch,
        existing_binding=existing_binding,
        thread_target_id=normalized_thread_target_id,
    )


def backfill_pr_binding_thread_target_ids(
    hub_root: Path,
    *,
    limit: int = 200,
    include_recent_terminal_threads: bool = True,
    terminal_thread_lookback: timedelta = _RECENT_TERMINAL_THREAD_LOOKBACK,
) -> dict[str, int]:
    store = PmaThreadStore(hub_root)
    binding_store = PrBindingStore(hub_root)
    counts = {
        "threads_scanned": 0,
        "bindings_matched": 0,
        "bindings_updated": 0,
    }
    candidate_threads: list[Mapping[str, Any]] = []
    seen_thread_ids: set[str] = set()
    for active_thread in store.list_threads(status="active", limit=limit):
        managed_thread_id = _normalize_text(active_thread.get("managed_thread_id"))
        if managed_thread_id is None or managed_thread_id in seen_thread_ids:
            continue
        seen_thread_ids.add(managed_thread_id)
        candidate_threads.append(active_thread)
    if include_recent_terminal_threads:
        cutoff = datetime.now(timezone.utc) - terminal_thread_lookback
        for terminal_thread in store.list_threads(limit=max(limit * 5, limit)):
            managed_thread_id = _normalize_text(
                terminal_thread.get("managed_thread_id")
            )
            if managed_thread_id is None or managed_thread_id in seen_thread_ids:
                continue
            if not bool(terminal_thread.get("status_terminal")):
                continue
            changed_at = _thread_changed_at(terminal_thread)
            if changed_at is None or changed_at < cutoff:
                continue
            seen_thread_ids.add(managed_thread_id)
            candidate_threads.append(terminal_thread)

    for thread in candidate_threads:
        managed_thread_id = _normalize_text(thread.get("managed_thread_id"))
        repo_id = _normalize_text(thread.get("repo_id"))
        if managed_thread_id is None or repo_id is None:
            continue
        counts["threads_scanned"] += 1
        head_branch = store.refresh_thread_head_branch(managed_thread_id)
        if head_branch is None:
            head_branch = _normalize_text(
                _mapping(thread.get("metadata")).get("head_branch")
            )
        if head_branch is None:
            continue
        for pr_state in _ACTIVE_PR_STATES:
            bindings = binding_store.list_bindings(
                provider="github",
                repo_id=repo_id,
                pr_state=pr_state,
                head_branch=head_branch,
                limit=20,
            )
            for binding in bindings:
                counts["bindings_matched"] += 1
                if binding.thread_target_id is not None:
                    continue
                updated = binding_store.attach_thread_target(
                    provider=binding.provider,
                    repo_slug=binding.repo_slug,
                    pr_number=binding.pr_number,
                    thread_target_id=managed_thread_id,
                )
                if (
                    updated is not None
                    and updated.thread_target_id == managed_thread_id
                    and binding.thread_target_id != managed_thread_id
                ):
                    counts["bindings_updated"] += 1
    return counts


__all__ = [
    "backfill_pr_binding_thread_target_ids",
    "binding_summary",
    "claim_pr_binding_for_thread",
    "explicit_thread_target_id",
    "find_hub_binding_context",
    "resolve_thread_target_id",
    "thread_contexts",
    "thread_matches_branch",
    "upsert_pr_binding",
]
