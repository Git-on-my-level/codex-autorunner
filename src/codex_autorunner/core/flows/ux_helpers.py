from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol

from ...tickets.files import list_ticket_paths
from ..ticket_flow_operator import TicketFlowRunSelection
from ..ticket_flow_operator import (
    build_ticket_flow_status_snapshot as _build_ticket_flow_status_snapshot,
)
from ..ticket_flow_operator import (
    ensure_flow_worker as _ensure_flow_worker,
)
from ..ticket_flow_operator import (
    select_default_ticket_flow_run as _select_default_ticket_flow_run,
)
from ..ticket_flow_operator import (
    select_ticket_flow_run as _select_ticket_flow_run,
)
from ..ticket_flow_operator import (
    select_ticket_flow_run_record as _select_ticket_flow_run_record,
)
from ..ticket_flow_operator import (
    ticket_progress as _ticket_progress,
)
from ..ticket_flow_summary import extract_current_step
from .models import (
    FlowRunRecord,
    FlowRunStatus,
    flow_run_duration_seconds,
    format_flow_duration,
)
from .store import FlowStore
from .worker_process import (
    check_worker_health,
    clear_worker_metadata,
    spawn_flow_worker,
)


@dataclass(frozen=True)
class BootstrapCheckResult:
    status: str
    github_available: Optional[bool] = None
    repo_slug: Optional[str] = None


@dataclass(frozen=True)
class IssueSeedResult:
    content: str
    issue_number: int
    repo_slug: str


class GitHubServiceProtocol(Protocol):
    def gh_available(self) -> bool: ...

    def gh_authenticated(self) -> bool: ...

    def repo_info(self) -> Any: ...

    def validate_issue_same_repo(self, issue_ref: str) -> int: ...

    def issue_view(self, number: int) -> dict: ...


def issue_md_path(repo_root: Path) -> Path:
    return repo_root.resolve() / ".codex-autorunner" / "ISSUE.md"


def issue_md_has_content(repo_root: Path) -> bool:
    issue_path = issue_md_path(repo_root)
    if not issue_path.exists():
        return False
    try:
        return bool(issue_path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _ticket_dir(repo_root: Path) -> Path:
    return repo_root.resolve() / ".codex-autorunner" / "tickets"


def ticket_progress(repo_root: Path) -> dict[str, int]:
    return _ticket_progress(repo_root)


def bootstrap_check(
    repo_root: Path,
    github_service_factory: Optional[Callable[[Path], GitHubServiceProtocol]] = None,
) -> BootstrapCheckResult:
    if list_ticket_paths(_ticket_dir(repo_root)):
        return BootstrapCheckResult(status="ready")

    if issue_md_has_content(repo_root):
        return BootstrapCheckResult(status="ready")

    gh_available = False
    repo_slug: Optional[str] = None
    if github_service_factory is not None:
        try:
            gh = github_service_factory(repo_root)
            gh_available = gh.gh_available() and gh.gh_authenticated()
            if gh_available:
                repo_info = gh.repo_info()
                repo_slug = getattr(repo_info, "name_with_owner", None)
        except (AttributeError, TypeError, RuntimeError, ValueError, OSError):
            gh_available = False
            repo_slug = None

    return BootstrapCheckResult(
        status="needs_issue", github_available=gh_available, repo_slug=repo_slug
    )


def format_issue_as_markdown(issue: dict, repo_slug: Optional[str] = None) -> str:
    number = issue.get("number")
    title = issue.get("title") or ""
    url = issue.get("url") or ""
    state = issue.get("state") or ""
    author = issue.get("author") or {}
    author_name = (
        author.get("login") if isinstance(author, dict) else str(author or "unknown")
    )
    labels = issue.get("labels")
    label_names: list[str] = []
    if isinstance(labels, list):
        for label in labels:
            if isinstance(label, dict):
                name = label.get("name")
            else:
                name = label
            if name:
                label_names.append(str(name))
    comments = issue.get("comments")
    comment_count = None
    if isinstance(comments, dict):
        total = comments.get("totalCount")
        if isinstance(total, int):
            comment_count = total

    body = issue.get("body") or "(no description)"
    lines = [
        f"# Issue #{number}: {title}".strip(),
        "",
        f"**Repo:** {repo_slug or 'unknown'}",
        f"**URL:** {url}",
        f"**State:** {state}",
        f"**Author:** {author_name}",
    ]
    if label_names:
        lines.append(f"**Labels:** {', '.join(label_names)}")
    if comment_count is not None:
        lines.append(f"**Comments:** {comment_count}")
    lines.extend(["", "## Description", "", str(body).strip(), ""])
    return "\n".join(lines)


def seed_issue_from_github(
    repo_root: Path,
    issue_ref: str,
    github_service_factory: Optional[Callable[[Path], GitHubServiceProtocol]] = None,
) -> IssueSeedResult:
    if github_service_factory is None:
        raise RuntimeError("GitHub service unavailable.")
    gh = github_service_factory(repo_root)
    if not (gh.gh_available() and gh.gh_authenticated()):
        raise RuntimeError("GitHub CLI is not available or not authenticated.")
    number = gh.validate_issue_same_repo(issue_ref)
    issue = gh.issue_view(number=number)
    repo_info = gh.repo_info()
    content = format_issue_as_markdown(issue, repo_info.name_with_owner)
    return IssueSeedResult(
        content=content, issue_number=number, repo_slug=repo_info.name_with_owner
    )


def seed_issue_from_text(plan_text: str) -> str:
    return f"# Issue\n\n{plan_text.strip()}\n"


def select_default_ticket_flow_run(
    store: FlowStore,
) -> Optional[FlowRunRecord]:
    return _select_default_ticket_flow_run(store)


def select_ticket_flow_run_record(
    records: list[FlowRunRecord],
    *,
    selection: TicketFlowRunSelection,
) -> Optional[FlowRunRecord]:
    return _select_ticket_flow_run_record(records, selection=selection)


def select_ticket_flow_run(
    store: FlowStore,
    *,
    selection: TicketFlowRunSelection,
) -> Optional[FlowRunRecord]:
    return _select_ticket_flow_run(store, selection=selection)


def resolve_ticket_flow_archive_mode(record: FlowRunRecord) -> str:
    if record.status.is_terminal():
        return "ready"
    if record.status in {FlowRunStatus.PAUSED, FlowRunStatus.STOPPING}:
        return "confirm"
    return "blocked"


def ticket_flow_archive_requires_force(record: FlowRunRecord) -> bool:
    return record.status in {FlowRunStatus.PAUSED, FlowRunStatus.STOPPING}


def _format_age_compact(age_seconds: Any) -> Optional[str]:
    if not isinstance(age_seconds, int):
        return None
    if age_seconds < 60:
        return f"{age_seconds}s ago"
    if age_seconds < 3600:
        return f"{age_seconds // 60}m ago"
    if age_seconds < 86400:
        return f"{age_seconds // 3600}h ago"
    return f"{age_seconds // 86400}d ago"


def _freshness_basis_label(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    basis = value.strip()
    if not basis:
        return None
    labels = {
        "run_state_last_progress_at": "last progress",
        "last_event_at": "last event",
        "latest_run_finished_at": "run finished",
        "latest_run_started_at": "run started",
        "latest_run_created_at": "run created",
        "ticket_ingested_at": "ticket ingest",
        "snapshot_generated_at": "snapshot time",
    }
    return labels.get(basis, basis.replace("_", " "))


def summarize_flow_freshness(payload: Any) -> Optional[str]:
    if not isinstance(payload, Mapping):
        return None
    status_raw = payload.get("status")
    status = str(status_raw).strip().lower() if status_raw is not None else ""
    if not status:
        return None
    parts = [status]
    basis = _freshness_basis_label(payload.get("recency_basis"))
    age_text = _format_age_compact(payload.get("age_seconds"))
    if basis and age_text:
        parts.append(f"{basis} {age_text}")
    elif basis:
        parts.append(basis)
    elif age_text:
        parts.append(age_text)
    return " · ".join(parts)


def _format_ticket_flow_archive_status(record: FlowRunRecord) -> str:
    mode = resolve_ticket_flow_archive_mode(record)
    if mode == "ready":
        return "ready"
    if mode == "confirm":
        return "confirm required"
    return "blocked until run stops"


def _flow_status_current_step(record: FlowRunRecord) -> Optional[str]:
    return extract_current_step(record)


def format_ticket_flow_status_lines(
    record: FlowRunRecord,
    snapshot: Mapping[str, Any],
) -> list[str]:
    worker = snapshot.get("worker_health")
    worker_status = getattr(worker, "status", "unknown")
    worker_pid = getattr(worker, "pid", None)
    worker_text = (
        f"{worker_status} (pid={worker_pid})"
        if isinstance(worker_pid, int)
        else str(worker_status)
    )
    last_event_seq = snapshot.get("last_event_seq")
    last_event_at = snapshot.get("last_event_at")
    current_ticket = snapshot.get("effective_current_ticket")
    ticket_progress_payload = snapshot.get("ticket_progress")
    progress_label = None
    if isinstance(ticket_progress_payload, Mapping):
        done = ticket_progress_payload.get("done")
        total = ticket_progress_payload.get("total")
        if isinstance(done, int) and isinstance(total, int) and total >= 0:
            progress_label = f"{done}/{total}"

    lines = [
        f"Run: {record.id}",
        f"Status: {record.status.value}",
    ]
    if progress_label:
        lines.append(f"Tickets: {progress_label}")

    current_step = _flow_status_current_step(record)
    if current_step:
        lines.append(f"Step: {current_step}")

    duration_label = format_flow_duration(flow_run_duration_seconds(record))
    if duration_label:
        lines.append(f"Elapsed: {duration_label}")

    lines.extend(
        [
            f"Last event: {last_event_seq if last_event_seq is not None else '-'} at {last_event_at or '-'}",
            f"Worker: {worker_text}",
            f"Current ticket: {current_ticket or '-'}",
        ]
    )

    freshness_summary = summarize_flow_freshness(snapshot.get("freshness"))
    if freshness_summary:
        lines.append(f"Freshness: {freshness_summary}")

    lines.append(f"Archive: {_format_ticket_flow_archive_status(record)}")
    return lines


def build_flow_status_snapshot(
    repo_root: Path,
    record: FlowRunRecord,
    store: Optional[FlowStore],
    *,
    lite: bool = False,
) -> dict:
    return _build_ticket_flow_status_snapshot(repo_root, record, store, lite=lite)


def ensure_worker(repo_root: Path, run_id: str, is_terminal: bool = False) -> dict:
    return _ensure_flow_worker(
        repo_root,
        run_id,
        is_terminal=is_terminal,
        check_worker_health_fn=check_worker_health,
        clear_worker_metadata_fn=clear_worker_metadata,
        spawn_flow_worker_fn=spawn_flow_worker,
    )


__all__ = [
    "BootstrapCheckResult",
    "IssueSeedResult",
    "bootstrap_check",
    "build_flow_status_snapshot",
    "ensure_worker",
    "format_ticket_flow_status_lines",
    "format_issue_as_markdown",
    "issue_md_has_content",
    "issue_md_path",
    "resolve_ticket_flow_archive_mode",
    "seed_issue_from_github",
    "seed_issue_from_text",
    "select_default_ticket_flow_run",
    "select_ticket_flow_run",
    "select_ticket_flow_run_record",
    "summarize_flow_freshness",
    "ticket_flow_archive_requires_force",
    "ticket_progress",
]
