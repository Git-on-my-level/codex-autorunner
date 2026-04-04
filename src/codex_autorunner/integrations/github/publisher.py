from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Protocol, cast

from ...core.publish_executor import PublishActionExecutor, TerminalPublishError
from ...core.publish_journal import PublishOperation
from ...core.text_utils import _normalize_optional_text
from .service import GitHubError, GitHubService, RepoInfo, parse_pr_input


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


GitHubServiceFactory = Callable[
    [Path, Optional[dict[str, Any]]], GitHubCommentPublisher
]


def _normalize_payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


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


__all__ = [
    "build_post_pr_comment_executor",
    "publish_pr_comment",
]
