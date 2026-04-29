"""GitHub-specific publish adapters for PR comments and review-comment reactions.

Ownership boundaries:
- This module is a thin provider-specific execution adapter.  It converts
  canonical ``PublishOperation`` payloads into GitHub API calls via
  ``GitHubCommentPublisher`` and returns structured response dicts.
- Retry scheduling, deduplication, and mutation-policy enforcement are owned
  exclusively by ``core/publish_executor.py`` and ``core/publish_journal.py``.
- This module must **not** carry its own retry counters, backoff state, or
  deduplication keys.
- ``GitHubError`` exceptions from the service layer are converted to
  ``TerminalPublishError`` so the publish executor classifies them as
  non-retryable.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional, Protocol, cast

from ...core.publish_executor import PublishActionExecutor, TerminalPublishError
from ...core.publish_journal import PublishOperation
from ...core.publish_operation_executors import build_enqueue_managed_turn_executor
from ...core.text_utils import _normalize_optional_text
from ..chat.bound_chat_execution_metadata import merge_bound_chat_execution_metadata
from .service import GitHubError, GitHubService, RepoInfo, parse_pr_input

_LOGGER = logging.getLogger(__name__)


class GitHubCommentPublisher(Protocol):
    def repo_info(self) -> RepoInfo: ...

    def create_issue_comment(
        self,
        *,
        owner: str,
        repo: str,
        number: int,
        body: str,
        cwd: Optional[Path] = None,
    ) -> dict[str, Any]: ...

    def create_pull_request_review_comment_reaction(
        self,
        *,
        owner: str,
        repo: str,
        comment_id: int,
        content: str,
        cwd: Optional[Path] = None,
    ) -> dict[str, Any]: ...


GitHubServiceFactory = Callable[
    [Path, Optional[dict[str, Any]]], GitHubCommentPublisher
]


def _normalize_payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _run_coroutine_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[Any] = []
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error.append(exc)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


def _require_text(value: Any, *, field_name: str) -> str:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        raise TerminalPublishError(f"Publish payload is missing '{field_name}'")
    return normalized


def _resolve_repo_and_pr(
    payload: dict[str, Any],
    *,
    service: GitHubCommentPublisher,
) -> tuple[str, int]:
    explicit_repo_slug = _normalize_optional_text(
        payload.get("repo_slug") or payload.get("repository")
    )
    explicit_pr_number = payload.get("pr_number")
    if isinstance(explicit_pr_number, str) and explicit_pr_number.strip().isdigit():
        explicit_pr_number = int(explicit_pr_number.strip())
    if explicit_repo_slug and isinstance(explicit_pr_number, int):
        return explicit_repo_slug, int(explicit_pr_number)

    pr_ref = _normalize_optional_text(
        payload.get("pr_ref") or payload.get("pr_url") or payload.get("pr")
    )
    if pr_ref:
        repo_slug, pr_number = parse_pr_input(pr_ref)
        if repo_slug is None:
            repo_slug = service.repo_info().name_with_owner
        return repo_slug, pr_number

    if isinstance(explicit_pr_number, int):
        return service.repo_info().name_with_owner, int(explicit_pr_number)
    raise TerminalPublishError(
        "Publish payload must include repo_slug+pr_number or a pr_ref/pr_url"
    )


def publish_pr_comment(
    payload: dict[str, Any],
    *,
    service: GitHubCommentPublisher,
    cwd: Optional[Path] = None,
) -> dict[str, Any]:
    body = _require_text(
        payload.get("body") or payload.get("message") or payload.get("text"),
        field_name="body",
    )
    repo_slug, pr_number = _resolve_repo_and_pr(payload, service=service)
    owner, repo = repo_slug.split("/", 1)
    created = service.create_issue_comment(
        owner=owner,
        repo=repo,
        number=pr_number,
        body=body,
        cwd=cwd,
    )
    comment_payload = _normalize_payload(created)
    comment_id = comment_payload.get("id")
    if isinstance(comment_id, str) and comment_id.strip().isdigit():
        comment_id = int(comment_id.strip())
    return {
        "repo_slug": repo_slug,
        "pr_number": pr_number,
        "comment_id": comment_id,
        "url": _normalize_optional_text(
            comment_payload.get("html_url") or comment_payload.get("url")
        ),
    }


def publish_pr_review_comment_reaction(
    payload: dict[str, Any],
    *,
    service: GitHubCommentPublisher,
    cwd: Optional[Path] = None,
) -> dict[str, Any]:
    repo_slug = _require_text(
        payload.get("repo_slug") or payload.get("repository"),
        field_name="repo_slug",
    )
    content = _normalize_optional_text(payload.get("content")) or "eyes"
    raw_comment_id = payload.get("comment_id")
    if raw_comment_id is None:
        raise TerminalPublishError("Publish payload is missing 'comment_id'")
    try:
        comment_id = int(raw_comment_id)
    except (TypeError, ValueError) as exc:
        raise TerminalPublishError(
            "Publish payload 'comment_id' must be an integer"
        ) from exc
    owner, repo = repo_slug.split("/", 1)
    created = service.create_pull_request_review_comment_reaction(
        owner=owner,
        repo=repo,
        comment_id=comment_id,
        content=content,
        cwd=cwd,
    )
    reaction_payload = _normalize_payload(created)
    reaction_id = reaction_payload.get("id")
    if isinstance(reaction_id, str) and reaction_id.strip().isdigit():
        reaction_id = int(reaction_id.strip())
    return {
        "repo_slug": repo_slug,
        "comment_id": comment_id,
        "content": content,
        "reaction_id": reaction_id,
        "url": _normalize_optional_text(
            reaction_payload.get("html_url") or reaction_payload.get("url")
        ),
    }


def build_post_pr_comment_executor(
    *,
    repo_root: Path,
    raw_config: Optional[dict[str, Any]] = None,
    github_service_factory: Optional[GitHubServiceFactory] = None,
) -> PublishActionExecutor:
    service_factory = github_service_factory or GitHubService

    def executor(operation: PublishOperation) -> dict[str, Any]:
        payload = _normalize_payload(operation.payload)
        workspace_override = _normalize_optional_text(payload.get("workspace_root"))
        operation_repo_root = (
            Path(workspace_override).resolve() if workspace_override else repo_root
        )
        try:
            service = service_factory(operation_repo_root, raw_config)
            return publish_pr_comment(payload, service=service, cwd=operation_repo_root)
        except GitHubError as exc:
            raise TerminalPublishError(str(exc)) from exc

    cast(Any, executor).mutation_policy_config = raw_config
    return executor


def build_react_pr_review_comment_executor(
    *,
    repo_root: Path,
    raw_config: Optional[dict[str, Any]] = None,
    github_service_factory: Optional[GitHubServiceFactory] = None,
) -> PublishActionExecutor:
    service_factory = github_service_factory or GitHubService

    def executor(operation: PublishOperation) -> dict[str, Any]:
        payload = _normalize_payload(operation.payload)
        workspace_override = _normalize_optional_text(payload.get("workspace_root"))
        operation_repo_root = (
            Path(workspace_override).resolve() if workspace_override else repo_root
        )
        try:
            service = service_factory(operation_repo_root, raw_config)
            return publish_pr_review_comment_reaction(
                payload,
                service=service,
                cwd=operation_repo_root,
            )
        except GitHubError as exc:
            raise TerminalPublishError(str(exc)) from exc

    cast(Any, executor).mutation_policy_config = raw_config
    return executor


def _scm_progress_subject(tracking: dict[str, Any]) -> str:
    repo_slug = _normalize_optional_text(tracking.get("repo_slug"))
    pr_number = tracking.get("pr_number")
    if isinstance(pr_number, str) and pr_number.strip().isdigit():
        pr_number = int(pr_number.strip())
    if isinstance(pr_number, int):
        if repo_slug:
            return f"{repo_slug}#{pr_number}"
        return f"PR #{pr_number}"
    return repo_slug or "the bound PR"


def _scm_progress_message(payload: dict[str, Any]) -> str:
    tracking = _normalize_mapping(payload.get("scm_reaction"))
    reaction_kind = _normalize_optional_text(tracking.get("reaction_kind"))
    subject = _scm_progress_subject(tracking)
    if reaction_kind == "ci_failed":
        return f"Investigating CI failure on {subject}..."
    if reaction_kind == "review_comment":
        return f"Processing review comment on {subject}..."
    if reaction_kind == "changes_requested":
        return f"Addressing requested changes on {subject}..."
    if reaction_kind:
        return f"Processing {reaction_kind.replace('_', ' ')} on {subject}..."
    request = _normalize_mapping(payload.get("request")) or payload
    prompt = _normalize_optional_text(
        request.get("message_text") or request.get("prompt") or request.get("body")
    )
    if prompt:
        return prompt
    return f"Processing SCM wake-up for {subject}..."


def _payload_with_bound_progress_metadata(
    payload: dict[str, Any],
    *,
    progress_targets: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    if not progress_targets:
        return payload
    request = _normalize_mapping(payload.get("request"))
    source = request if request else payload
    metadata = _normalize_mapping(source.get("metadata"))
    merged_metadata = merge_bound_chat_execution_metadata(
        metadata,
        # SCM wake-ups are not user chat turns; use a distinct origin so
        # execution_mapping_has_chat_surface_origin does not skip orphan recovery.
        origin_kind="github_scm",
        progress_targets=progress_targets,
    )
    if request:
        return {
            **payload,
            "request": {
                **request,
                "metadata": merged_metadata,
            },
        }
    return {
        **payload,
        "metadata": merged_metadata,
    }


async def _start_scm_bound_live_progress(
    *,
    hub_root: Path,
    raw_config: Optional[dict[str, Any]],
    managed_thread_id: str,
    managed_turn_id: str,
    agent: str,
    model: Optional[str],
    progress_targets: tuple[tuple[str, str], ...],
    message: str,
) -> bool:
    from ..chat.bound_live_progress import build_bound_chat_live_progress_session

    session = build_bound_chat_live_progress_session(
        hub_root=hub_root,
        raw_config=raw_config or {},
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        agent=agent,
        model=model,
        surface_targets=progress_targets,
    )
    session.tracker.note_commentary(message)
    try:
        return await session.start()
    finally:
        await session.close()


def build_github_enqueue_managed_turn_executor(
    *,
    repo_root: Path,
    raw_config: Optional[dict[str, Any]] = None,
) -> PublishActionExecutor:
    base_executor = build_enqueue_managed_turn_executor(hub_root=repo_root)

    def executor(operation: PublishOperation) -> Optional[dict[str, Any]]:
        from ..chat.bound_live_progress import bound_chat_live_progress_targets

        payload = _normalize_payload(operation.payload)
        requested_thread_id = _normalize_optional_text(payload.get("thread_target_id"))
        progress_targets = (
            bound_chat_live_progress_targets(
                hub_root=repo_root,
                managed_thread_id=requested_thread_id,
            )
            if requested_thread_id is not None
            else ()
        )
        progress_payload = _payload_with_bound_progress_metadata(
            payload,
            progress_targets=progress_targets,
        )
        effective_operation = (
            operation
            if progress_payload == payload
            else replace(operation, payload=progress_payload)
        )
        result = base_executor(effective_operation)
        if result is None:
            return None
        if progress_targets and result.get("deduped") is not True:
            managed_thread_id = _normalize_optional_text(result.get("thread_target_id"))
            managed_turn_id = _normalize_optional_text(result.get("managed_turn_id"))
            request = _normalize_mapping(progress_payload.get("request"))
            source = request if request else progress_payload
            if managed_thread_id is not None and managed_turn_id is not None:
                try:
                    published = _run_coroutine_sync(
                        _start_scm_bound_live_progress(
                            hub_root=repo_root,
                            raw_config=raw_config,
                            managed_thread_id=managed_thread_id,
                            managed_turn_id=managed_turn_id,
                            agent="agent",
                            model=_normalize_optional_text(source.get("model")),
                            progress_targets=progress_targets,
                            message=_scm_progress_message(progress_payload),
                        )
                    )
                except Exception:
                    _LOGGER.warning(
                        "GitHub SCM managed-turn progress placeholder failed "
                        "(thread_target_id=%s, managed_turn_id=%s)",
                        managed_thread_id,
                        managed_turn_id,
                        exc_info=True,
                    )
                else:
                    result["progress_published"] = bool(published)
                    result["progress_targets"] = [
                        {"surface_kind": kind, "surface_key": key}
                        for kind, key in progress_targets
                    ]
        return result

    cast(Any, executor).mutation_policy_config = raw_config
    return executor


def build_github_publish_executors(
    *,
    repo_root: Path,
    raw_config: Optional[dict[str, Any]] = None,
    github_service_factory: Optional[GitHubServiceFactory] = None,
) -> dict[str, PublishActionExecutor]:
    return {
        "enqueue_managed_turn": build_github_enqueue_managed_turn_executor(
            repo_root=repo_root,
            raw_config=raw_config,
        ),
        "react_pr_review_comment": build_react_pr_review_comment_executor(
            repo_root=repo_root,
            raw_config=raw_config,
            github_service_factory=github_service_factory,
        ),
    }


__all__ = [
    "build_github_enqueue_managed_turn_executor",
    "build_github_publish_executors",
    "build_react_pr_review_comment_executor",
    "build_post_pr_comment_executor",
    "publish_pr_review_comment_reaction",
    "publish_pr_comment",
]
