from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core.git_utils import git_branch
from ...core.pr_binding_runtime import claim_pr_binding_for_thread
from ...core.pr_bindings import PrBinding, PrBindingStore
from ...core.text_utils import _mapping, _normalize_text
from .polling import GitHubScmPollingService
from .service import GitHubError, GitHubService

_LOGGER = logging.getLogger(__name__)
_GITHUB_PR_URL_RE = re.compile(
    r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
    re.IGNORECASE,
)
_ACTIVE_PR_STATES = ("open", "draft")
_BRANCH_METADATA_KEYS = ("head_branch", "branch", "git_branch")
_CONTEXT_MAPPING_KEYS = ("manual_context", "scm", "scm_context", "context")


def runtime_output_suggests_pr_open(
    assistant_text: str,
    raw_events: tuple[Any, ...],
) -> bool:
    message = str(assistant_text or "")
    if _GITHUB_PR_URL_RE.search(message):
        return True
    lowered_message = message.lower()
    if "pull request" in lowered_message or re.search(
        r"\bpr\s*#?\d+\b", lowered_message
    ):
        return True
    for raw_event in raw_events:
        try:
            serialized = json.dumps(raw_event, sort_keys=True)
        except TypeError:
            serialized = str(raw_event)
        if _GITHUB_PR_URL_RE.search(serialized):
            return True
        lowered_event = serialized.lower()
        if "gh pr create" in lowered_event or "pull request" in lowered_event:
            return True
    return False


def _thread_head_branch_hint(
    thread_payload: Mapping[str, Any],
) -> Optional[str]:
    metadata = _mapping(thread_payload.get("metadata"))
    contexts: list[Mapping[str, Any]] = [metadata]
    for key in _CONTEXT_MAPPING_KEYS:
        nested = _mapping(metadata.get(key))
        if nested:
            contexts.append(nested)
    for context in contexts:
        for key in _BRANCH_METADATA_KEYS:
            head_branch = _normalize_text(context.get(key))
            if head_branch is not None:
                return head_branch
    return None


def _resolve_head_branch(
    *,
    workspace_root: Path,
    head_branch_hint: Optional[str],
    thread_payload: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    resolved_hint = _normalize_text(head_branch_hint)
    if resolved_hint is not None:
        return resolved_hint
    if isinstance(thread_payload, Mapping):
        resolved_hint = _thread_head_branch_hint(thread_payload)
        if resolved_hint is not None:
            return resolved_hint
    return _normalize_text(git_branch(workspace_root))


def _claim_existing_branch_binding_for_thread(
    *,
    hub_root: Path,
    repo_id: Optional[str],
    head_branch: Optional[str],
    managed_thread_id: str,
) -> Optional[PrBinding]:
    normalized_repo_id = _normalize_text(repo_id)
    normalized_head_branch = _normalize_text(head_branch)
    if normalized_repo_id is None or normalized_head_branch is None:
        return None
    binding_store = PrBindingStore(hub_root)
    claimed_binding: Optional[PrBinding] = None
    for pr_state in _ACTIVE_PR_STATES:
        bindings = binding_store.list_bindings(
            provider="github",
            repo_id=normalized_repo_id,
            pr_state=pr_state,
            head_branch=normalized_head_branch,
            limit=20,
        )
        for binding in bindings:
            if binding.thread_target_id not in {None, managed_thread_id}:
                continue
            updated = binding_store.attach_thread_target(
                provider=binding.provider,
                repo_slug=binding.repo_slug,
                pr_number=binding.pr_number,
                thread_target_id=managed_thread_id,
            )
            if updated is not None and updated.thread_target_id == managed_thread_id:
                claimed_binding = updated
    return claimed_binding


def _claim_discovered_pr_binding_for_thread(
    *,
    hub_root: Path,
    workspace_root: Path,
    managed_thread_id: str,
    repo_id: Optional[str],
    raw_config: Optional[dict[str, Any]],
) -> Optional[PrBinding]:
    try:
        github = GitHubService(
            workspace_root,
            raw_config=raw_config if isinstance(raw_config, dict) else None,
            config_root=hub_root,
            traffic_class="background",
        )
        summary = github.discover_pr_binding_summary(cwd=workspace_root)
    except (GitHubError, OSError, RuntimeError, ValueError):
        _LOGGER.debug(
            "Managed-thread PR discovery failed (managed_thread_id=%s, workspace_root=%s)",
            managed_thread_id,
            workspace_root,
            exc_info=True,
        )
        return None
    if not isinstance(summary, dict):
        return None
    repo_slug = _normalize_text(summary.get("repo_slug"))
    pr_number = summary.get("pr_number")
    pr_state = _normalize_text(summary.get("pr_state"))
    if repo_slug is None or not isinstance(pr_number, int) or pr_state is None:
        return None
    claimed = claim_pr_binding_for_thread(
        hub_root,
        provider="github",
        repo_slug=repo_slug,
        repo_id=_normalize_text(repo_id),
        pr_number=pr_number,
        pr_state=pr_state,
        head_branch=_normalize_text(summary.get("head_branch")),
        base_branch=_normalize_text(summary.get("base_branch")),
        thread_target_id=managed_thread_id,
    )
    if claimed is None or claimed.thread_target_id != managed_thread_id:
        return None
    return claimed


def self_claim_and_arm_pr_binding(
    *,
    hub_root: Path,
    workspace_root: Path,
    managed_thread_id: str,
    repo_id: Optional[str],
    head_branch_hint: Optional[str],
    assistant_text: str,
    raw_events: tuple[Any, ...],
    raw_config: Optional[dict[str, Any]],
    thread_payload: Optional[Mapping[str, Any]] = None,
) -> Optional[PrBinding]:
    claimed_binding = _claim_existing_branch_binding_for_thread(
        hub_root=hub_root,
        repo_id=repo_id,
        head_branch=_resolve_head_branch(
            workspace_root=workspace_root,
            head_branch_hint=head_branch_hint,
            thread_payload=thread_payload,
        ),
        managed_thread_id=managed_thread_id,
    )
    if claimed_binding is None and not runtime_output_suggests_pr_open(
        assistant_text, raw_events
    ):
        return None
    if claimed_binding is None:
        claimed_binding = _claim_discovered_pr_binding_for_thread(
            hub_root=hub_root,
            workspace_root=workspace_root,
            managed_thread_id=managed_thread_id,
            repo_id=repo_id,
            raw_config=raw_config,
        )
    if claimed_binding is None:
        return None
    try:
        GitHubScmPollingService(
            hub_root,
            raw_config=raw_config,
        ).arm_watch(
            binding=claimed_binding,
            workspace_root=workspace_root,
            reaction_config=raw_config,
        )
    except Exception:
        _LOGGER.warning(
            "Managed-thread PR binding watch arm failed (managed_thread_id=%s, repo_slug=%s, pr_number=%s)",
            managed_thread_id,
            claimed_binding.repo_slug,
            claimed_binding.pr_number,
            exc_info=True,
        )
    return claimed_binding


__all__ = [
    "runtime_output_suggests_pr_open",
    "self_claim_and_arm_pr_binding",
]
