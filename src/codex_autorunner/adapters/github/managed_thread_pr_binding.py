from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core.pr_binding_runtime import (
    claim_branch_binding_for_thread,
    claim_pr_binding_for_thread,
    resolve_head_branch,
)
from ...core.pr_bindings import PrBinding
from ...core.text_utils import _normalize_text
from .polling import GitHubScmPollingService
from .service import GitHubError, GitHubService

_LOGGER = logging.getLogger(__name__)
_GITHUB_PR_URL_RE = re.compile(
    r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
    re.IGNORECASE,
)


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
    claimed_binding = claim_branch_binding_for_thread(
        hub_root=hub_root,
        provider="github",
        repo_id=repo_id,
        head_branch=resolve_head_branch(
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
