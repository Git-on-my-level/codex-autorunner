from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

from ...core.config import load_repo_config
from ...core.logging_utils import log_event
from ...core.surface_context_capsules import (
    append_capsules_to_prompt,
    build_github_context_capsule,
    build_model_only_text_capsule,
)
from ...core.utils import RepoNotFoundError, find_repo_root
from .service import GitHubService, find_github_links, parse_github_url

_ISSUE_ONLY_LINK_WRAPPERS = (
    "{link}",
    "<{link}>",
    "({link})",
    "[{link}]",
    "`{link}`",
)
_ISSUE_ONLY_LEADING_MENTION_RE = re.compile(
    r"^(?:(?:<@!?\d+>|<@&\d+>|<#\d+>)\s*[:,]?\s*)+"
)
READY_FOR_REVIEW_PR_DEFAULT_HINT = (
    "Default to a ready-for-review PR unless the user explicitly asks for a draft."
)


def issue_only_link(prompt_text: str, links: list[str]) -> Optional[str]:
    if not prompt_text or not links or len(links) != 1:
        return None
    stripped = prompt_text.strip()
    if not stripped:
        return None
    stripped = _ISSUE_ONLY_LEADING_MENTION_RE.sub("", stripped).strip()
    if not stripped:
        return None
    link = links[0]
    for wrapper in _ISSUE_ONLY_LINK_WRAPPERS:
        if stripped == wrapper.format(link=link):
            return link
    return None


def issue_only_workflow_hint(issue_number: int) -> str:
    capsule = build_model_only_text_capsule(
        capsule_id="github.issue_only_workflow",
        text=(
            "Issue-only GitHub message detected (no extra context).\n"
            f"Treat this as a request to implement issue #{issue_number}.\n"
            "Create a new branch from the latest head branch, "
            "sync with the current origin default branch first,\n"
            "implement the fix, and open a PR.\n"
            f"{READY_FOR_REVIEW_PR_DEFAULT_HINT}\n"
            f"Ensure the PR description includes `Closes #{issue_number}` "
            "so GitHub auto-closes the issue when merged."
        ),
        reason="github_issue_only_link",
        source={"issue_number": issue_number},
    )
    if capsule is None:
        return ""
    injected, _ = append_capsules_to_prompt("", (capsule,))
    return injected


async def maybe_inject_github_context(
    *,
    prompt_text: str,
    link_source_text: str,
    workspace_root: Optional[Path],
    logger: logging.Logger,
    event_prefix: str,
    allow_cross_repo: bool = False,
) -> tuple[str, bool]:
    if not prompt_text or not workspace_root:
        return prompt_text, False

    links = find_github_links(link_source_text or "")
    if not links:
        log_event(
            logger,
            logging.DEBUG,
            f"{event_prefix}.skip",
            reason="no_links",
            source="user_message",
        )
        return prompt_text, False

    repo_root: Optional[Path]
    try:
        repo_root = find_repo_root(workspace_root)
    except RepoNotFoundError:
        repo_root = workspace_root if allow_cross_repo else None
    if repo_root is None:
        log_event(
            logger,
            logging.WARNING,
            f"{event_prefix}.skip",
            reason="repo_not_found",
            workspace_path=str(workspace_root),
            source="user_message",
        )
        return prompt_text, False

    try:
        repo_config = load_repo_config(repo_root)
        raw_config = repo_config.raw if repo_config else None
    except (OSError, ValueError):
        raw_config = None

    svc = GitHubService(repo_root, raw_config=raw_config)
    if not svc.gh_available():
        log_event(
            logger,
            logging.WARNING,
            f"{event_prefix}.skip",
            reason="gh_unavailable",
            repo_root=str(repo_root),
            source="user_message",
        )
        return prompt_text, False
    if not svc.gh_authenticated():
        log_event(
            logger,
            logging.WARNING,
            f"{event_prefix}.skip",
            reason="gh_unauthenticated",
            repo_root=str(repo_root),
            source="user_message",
        )
        return prompt_text, False

    issue_only = issue_only_link(link_source_text, links)
    for link in links:
        try:
            result = await asyncio.to_thread(
                svc.build_context_file_from_url,
                link,
                allow_cross_repo=allow_cross_repo,
            )
        except (
            Exception
        ):  # intentional: GitHub API/network errors are non-fatal, best-effort injection
            result = None
        if result and result.get("hint"):
            hint = str(result["hint"])
            parsed = parse_github_url(link)
            if issue_only and link == issue_only and parsed and parsed[1] == "issue":
                hint = f"{hint}\n\n{issue_only_workflow_hint(parsed[2])}"
            capsule = build_github_context_capsule(
                hint_text=hint,
                url=link,
                path=str(result.get("path") or ""),
                kind=str(result.get("kind") or (parsed[1] if parsed else "")),
            )
            log_event(
                logger,
                logging.INFO,
                f"{event_prefix}.injected",
                repo_root=str(repo_root),
                path=result.get("path"),
                source="user_message",
            )
            return append_capsules_to_prompt(prompt_text, (capsule,))

    log_event(
        logger,
        logging.INFO,
        f"{event_prefix}.skip",
        reason="no_context",
        repo_root=str(repo_root),
        source="user_message",
    )
    return prompt_text, False


__all__ = [
    "issue_only_link",
    "issue_only_workflow_hint",
    "maybe_inject_github_context",
]
