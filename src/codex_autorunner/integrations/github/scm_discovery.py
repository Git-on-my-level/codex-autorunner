"""SCM discovery helpers for PR binding discovery, normalization, and persistence.

Extracted from GitHubService to separate generic gh reads from higher-level
SCM automation workflows.  GitHubService retains thin delegate methods for
backward compatibility; the canonical implementations live here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ...core.pr_binding_runtime import (
    binding_summary,
    find_hub_binding_context,
    upsert_pr_binding,
)
from ...core.pr_bindings import PrBinding, PrBindingStore
from ...core.text_utils import _normalize_optional_text

if TYPE_CHECKING:
    from .service import GitHubService

logger = logging.getLogger(__name__)


def normalize_positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def normalize_binding_pr_state(state: Any, *, is_draft: Any = False) -> Optional[str]:
    normalized = _normalize_optional_text(state)
    if normalized is None:
        return None
    lowered = normalized.lower()
    if lowered == "open":
        return "draft" if bool(is_draft) else "open"
    if lowered == "closed":
        return "closed"
    if lowered == "merged":
        return "merged"
    return None


def normalize_pr_binding_summary(
    *, pr: dict[str, Any], repo_slug: str
) -> Optional[dict[str, Any]]:
    normalized_repo_slug = _normalize_optional_text(repo_slug)
    if normalized_repo_slug is None:
        return None

    pr_number = normalize_positive_int(pr.get("number"))
    pr_state = normalize_binding_pr_state(pr.get("state"), is_draft=pr.get("isDraft"))
    if pr_number is None or pr_state is None:
        return None

    summary: dict[str, Any] = {
        "repo_slug": normalized_repo_slug,
        "pr_number": pr_number,
        "pr_state": pr_state,
    }
    head_branch = _normalize_optional_text(pr.get("headRefName"))
    if head_branch is not None:
        summary["head_branch"] = head_branch
    base_branch = _normalize_optional_text(pr.get("baseRefName"))
    if base_branch is not None:
        summary["base_branch"] = base_branch
    return summary


def binding_context_from_root(
    repo_root: Path,
) -> tuple[Optional[Path], Optional[str]]:
    return find_hub_binding_context(repo_root)


def pr_binding_store_from_root(repo_root: Path) -> Optional[PrBindingStore]:
    hub_root, _repo_id = binding_context_from_root(repo_root)
    if hub_root is None:
        return None
    return PrBindingStore(hub_root)


def persist_pr_binding(
    *,
    repo_root: Path,
    repo_slug: str,
    summary: dict[str, Any],
    existing_binding: Optional[PrBinding] = None,
) -> Optional[PrBinding]:
    normalized_repo_slug = _normalize_optional_text(repo_slug)
    pr_number = normalize_positive_int(summary.get("pr_number"))
    pr_state = normalize_binding_pr_state(summary.get("pr_state"))
    if normalized_repo_slug is None or pr_number is None or pr_state is None:
        return None

    hub_root, repo_id = binding_context_from_root(repo_root)
    if hub_root is None:
        return None
    return upsert_pr_binding(
        hub_root,
        provider="github",
        repo_slug=normalized_repo_slug,
        repo_id=repo_id,
        pr_number=pr_number,
        pr_state=pr_state,
        head_branch=_normalize_optional_text(summary.get("head_branch")),
        base_branch=_normalize_optional_text(summary.get("base_branch")),
        existing_binding=existing_binding,
    )


def discover_pr_binding_summary(
    service: GitHubService,
    *,
    branch: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    resolved_branch = _normalize_optional_text(branch) or service.current_branch(
        cwd=cwd
    )
    if not resolved_branch or resolved_branch == "HEAD":
        return None

    hub_root, repo_id = binding_context_from_root(service.repo_root)
    store = PrBindingStore(hub_root) if hub_root is not None else None
    if store is not None and repo_id is not None:
        canonical_bindings: list[PrBinding] = []
        for pr_state in ("open", "draft"):
            canonical_bindings.extend(
                store.list_bindings(
                    provider="github",
                    repo_id=repo_id,
                    pr_state=pr_state,
                    head_branch=resolved_branch,
                    limit=1,
                )
            )
        if canonical_bindings:
            canonical_bindings.sort(
                key=lambda b: (b.updated_at, b.pr_number),
                reverse=True,
            )
            return binding_summary(canonical_bindings[0])

    pr = service.pr_for_branch(branch=resolved_branch, cwd=cwd)
    if not isinstance(pr, dict):
        return None

    try:
        repo_slug = service.repo_info().name_with_owner
    except Exception:
        return None

    fallback_binding: Optional[PrBinding] = None
    if store is not None:
        fallback_binding = store.find_active_binding_for_branch(
            provider="github",
            repo_slug=repo_slug,
            branch_name=resolved_branch,
        )

    summary = normalize_pr_binding_summary(pr=pr, repo_slug=repo_slug)
    if summary is None:
        return (
            binding_summary(fallback_binding) if fallback_binding is not None else None
        )
    return summary


def discover_pr_binding(
    service: GitHubService,
    *,
    branch: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> Optional[PrBinding]:
    summary = discover_pr_binding_summary(service, branch=branch, cwd=cwd)
    if summary is None:
        return None

    store = pr_binding_store_from_root(service.repo_root)
    existing_binding: Optional[PrBinding] = None
    repo_slug = _normalize_optional_text(summary.get("repo_slug"))
    head_branch = _normalize_optional_text(summary.get("head_branch"))
    if store is not None and repo_slug is not None and head_branch is not None:
        existing_binding = store.find_active_binding_for_branch(
            provider="github",
            repo_slug=repo_slug,
            branch_name=head_branch,
        )

    return (
        persist_pr_binding(
            repo_root=service.repo_root,
            repo_slug=str(summary["repo_slug"]),
            summary=summary,
            existing_binding=existing_binding,
        )
        if repo_slug is not None
        else None
    )


def arm_polling_watch_best_effort(
    *,
    repo_root: Path,
    raw_config: dict[str, Any],
    persisted_binding: PrBinding,
    workspace_root: Path,
    reaction_config: Any,
) -> None:
    """Best-effort SCM polling watch arming.

    Intentionally swallows exceptions so callers (sync_pr, etc.) are not blocked.
    """
    from .polling import GitHubScmPollingService

    try:
        GitHubScmPollingService(
            repo_root,
            raw_config=raw_config if isinstance(raw_config, dict) else None,
        ).arm_watch(
            binding=persisted_binding,
            workspace_root=workspace_root,
            reaction_config=reaction_config,
        )
    except Exception:
        logger.warning(
            "Failed arming SCM polling watch for %s#%s",
            persisted_binding.repo_slug,
            persisted_binding.pr_number,
            exc_info=True,
        )
