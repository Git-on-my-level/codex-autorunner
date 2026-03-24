from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional, Protocol, Sequence

from ...tickets.files import list_ticket_paths, ticket_is_done
from ..config import ConfigError, load_repo_config
from ..freshness import resolve_stale_threshold_seconds
from ..ticket_flow_projection import (
    build_canonical_state_v1,
    select_authoritative_run_record,
)
from .models import FlowEventType, FlowRunRecord, FlowRunStatus
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


TicketFlowRunSelection = Literal["active", "authoritative", "non_terminal", "paused"]


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
    ticket_dir = _ticket_dir(repo_root)
    ticket_paths = list_ticket_paths(ticket_dir)
    total = len(ticket_paths)
    done = 0
    if total:
        for path in ticket_paths:
            if ticket_is_done(path):
                done += 1
    return {"done": done, "total": total}


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
        except Exception:
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


def _derive_effective_current_ticket(
    record: FlowRunRecord, store: Optional[FlowStore]
) -> Optional[str]:
    if store is None:
        return None
    try:
        if (
            getattr(record, "flow_type", None) != "ticket_flow"
            or not record.status.is_active()
        ):
            return None
        last_started = store.get_last_event_seq_by_types(
            record.id, [FlowEventType.STEP_STARTED]
        )
        last_finished = store.get_last_event_seq_by_types(
            record.id, [FlowEventType.STEP_COMPLETED, FlowEventType.STEP_FAILED]
        )
        in_progress = bool(
            last_started is not None
            and (last_finished is None or last_started > last_finished)
        )
        if not in_progress:
            return None
        return store.get_latest_step_progress_current_ticket(
            record.id, after_seq=last_finished
        )
    except Exception:
        return None


def select_default_ticket_flow_run(
    store: FlowStore,
) -> Optional[FlowRunRecord]:
    return select_ticket_flow_run(store, selection="authoritative")


def select_ticket_flow_run_record(
    records: Sequence[FlowRunRecord],
    *,
    selection: TicketFlowRunSelection,
) -> Optional[FlowRunRecord]:
    if selection == "authoritative":
        return select_authoritative_run_record(list(records))
    if selection == "paused":
        return next((record for record in records if record.status.is_paused()), None)
    if selection == "active":
        return next((record for record in records if record.status.is_active()), None)
    if selection == "non_terminal":
        return next(
            (record for record in records if not record.status.is_terminal()), None
        )
    raise ValueError(f"Unsupported ticket flow run selection: {selection}")


def select_ticket_flow_run(
    store: FlowStore,
    *,
    selection: TicketFlowRunSelection,
) -> Optional[FlowRunRecord]:
    records = store.list_flow_runs(flow_type="ticket_flow")
    return select_ticket_flow_run_record(records, selection=selection)


def resolve_ticket_flow_archive_mode(record: FlowRunRecord) -> str:
    if record.status.is_terminal():
        return "ready"
    if record.status in {FlowRunStatus.PAUSED, FlowRunStatus.STOPPING}:
        return "confirm"
    return "blocked"


def ticket_flow_archive_requires_force(record: FlowRunRecord) -> bool:
    return record.status in {FlowRunStatus.PAUSED, FlowRunStatus.STOPPING}


def _canonical_flow_status_state(
    repo_root: Path,
    record: FlowRunRecord,
    store: Optional[FlowStore],
) -> Optional[dict[str, Any]]:
    if store is None:
        return None
    try:
        repo_config = load_repo_config(repo_root)
        pma_config = getattr(repo_config, "pma", None)
        stale_threshold_seconds = resolve_stale_threshold_seconds(
            getattr(pma_config, "freshness_stale_threshold_seconds", None)
        )
    except ConfigError:
        stale_threshold_seconds = resolve_stale_threshold_seconds(None)

    run_state = None
    try:
        from ..pma_context import build_ticket_flow_run_state

        run_state = build_ticket_flow_run_state(
            repo_root=repo_root,
            repo_id=repo_root.name,
            record=record,
            store=store,
            has_pending_dispatch=False,
        )
    except Exception:
        run_state = None
    run_state_payload = dict(run_state) if isinstance(run_state, dict) else None
    try:
        return build_canonical_state_v1(
            repo_root=repo_root,
            repo_id=repo_root.name,
            run_state=run_state_payload,
            record=record,
            store=store,
            stale_threshold_seconds=stale_threshold_seconds,
        )
    except Exception:
        return None


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


def build_flow_status_snapshot(
    repo_root: Path, record: FlowRunRecord, store: Optional[FlowStore]
) -> dict:
    last_event_seq = None
    last_event_at = None
    if store:
        try:
            last_event_seq, last_event_at = store.get_last_event_meta(record.id)
        except Exception:
            last_event_seq, last_event_at = None, None
    health = check_worker_health(repo_root, record.id)

    state = record.state or {}
    current_ticket = None
    if isinstance(state, dict):
        ticket_engine = state.get("ticket_engine")
        if isinstance(ticket_engine, dict):
            current_ticket = ticket_engine.get("current_ticket")
            if not (isinstance(current_ticket, str) and current_ticket.strip()):
                current_ticket = None
    effective_ticket = current_ticket
    if not effective_ticket:
        effective_ticket = _derive_effective_current_ticket(record, store)

    updated_state: Optional[dict] = None
    if effective_ticket and not current_ticket and isinstance(state, dict):
        ticket_engine = state.get("ticket_engine")
        ticket_engine = dict(ticket_engine) if isinstance(ticket_engine, dict) else {}
        ticket_engine["current_ticket"] = effective_ticket
        updated_state = dict(state)
        updated_state["ticket_engine"] = ticket_engine
    canonical_state = _canonical_flow_status_state(repo_root, record, store)
    freshness = (
        canonical_state.get("freshness") if isinstance(canonical_state, dict) else None
    )

    return {
        "last_event_seq": last_event_seq,
        "last_event_at": last_event_at,
        "worker_health": health,
        "effective_current_ticket": effective_ticket,
        "ticket_progress": ticket_progress(repo_root),
        "state": updated_state,
        "canonical_state_v1": canonical_state,
        "freshness": freshness,
    }


def ensure_worker(repo_root: Path, run_id: str, is_terminal: bool = False) -> dict:
    health = check_worker_health(repo_root, run_id)
    # Only clear metadata for dead/mismatch/invalid workers if not terminal
    if not is_terminal and health.status in {"dead", "mismatch", "invalid"}:
        try:
            clear_worker_metadata(health.artifact_path.parent)
        except Exception:
            pass
    if health.is_alive:
        return {"status": "reused", "health": health}

    proc, stdout_handle, stderr_handle = spawn_flow_worker(repo_root, run_id)
    # Parent-side stream handles are only needed for process spawn wiring.
    # Closing immediately avoids leaking file descriptors in long-lived services.
    for stream in (stdout_handle, stderr_handle):
        try:
            stream.close()
        except Exception:
            pass
    return {
        "status": "spawned",
        "health": health,
        "proc": proc,
        "stdout": None,
        "stderr": None,
    }


__all__ = [
    "BootstrapCheckResult",
    "IssueSeedResult",
    "bootstrap_check",
    "build_flow_status_snapshot",
    "ensure_worker",
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
