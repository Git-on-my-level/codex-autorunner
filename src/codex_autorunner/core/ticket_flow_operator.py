from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Sequence, TypedDict

from ..tickets.files import list_ticket_paths, read_ticket, safe_relpath, ticket_is_done
from ..tickets.frontmatter import parse_markdown_frontmatter
from ..tickets.models import Dispatch
from ..tickets.outbox import parse_dispatch, resolve_outbox_paths
from ..tickets.replies import resolve_reply_paths
from .config import load_repo_config
from .config_contract import ConfigError
from .flows.failure_diagnostics import format_failure_summary, get_failure_payload
from .flows.models import (
    FlowEventType,
    FlowRunRecord,
    FlowRunStatus,
    flow_run_duration_seconds,
)
from .flows.start_policy import evaluate_ticket_start_policy
from .flows.store import FlowStore
from .flows.worker_process import (
    check_worker_health,
    clear_worker_metadata,
    read_worker_crash_info,
    spawn_flow_worker,
)
from .flows.workspace_root import resolve_ticket_flow_workspace_root
from .freshness import resolve_stale_threshold_seconds
from .state_roots import resolve_repo_flows_db_path
from .text_utils import _normalize_optional_text
from .ticket_flow_projection import (
    build_canonical_state_v1,
    collect_ticket_flow_census,
    select_authoritative_run_record,
)
from .utils import resolve_executable

logger = logging.getLogger(__name__)

DEFAULT_MAX_TEXT_CHARS = 800
TicketFlowRunSelection = Literal["active", "authoritative", "non_terminal", "paused"]


@dataclass(frozen=True)
class PreflightCheck:
    check_id: str
    status: str
    message: str
    fix: Optional[str] = None
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.check_id,
            "status": self.status,
            "message": self.message,
            "fix": self.fix,
            "details": list(self.details),
        }


@dataclass(frozen=True)
class PreflightReport:
    checks: list[PreflightCheck]

    def has_errors(self) -> bool:
        return any(check.status == "error" for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": sum(1 for check in self.checks if check.status == "ok"),
            "warnings": sum(1 for check in self.checks if check.status == "warning"),
            "errors": sum(1 for check in self.checks if check.status == "error"),
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class RunReuseResult:
    action: str
    run: Optional[FlowRunRecord] = None
    pending_ticket_count: int = 0
    stale_terminal_runs: tuple[FlowRunRecord, ...] = ()


class TicketFlowWorkerCrash(TypedDict):
    summary: Optional[str]
    open_url: str
    path: str


class TicketFlowRunState(TypedDict, total=False):
    state: str
    blocking_reason: Optional[str]
    current_ticket: Optional[str]
    last_progress_at: Optional[str]
    recommended_action: Optional[str]
    recommended_actions: list[str]
    attention_required: bool
    worker_status: Optional[str]
    crash: Optional[TicketFlowWorkerCrash]
    flow_status: str
    duration_seconds: Optional[float]
    repo_id: str
    run_id: str
    active_run_id: Optional[str]


@dataclass(frozen=True)
class TicketFlowOperatorService:
    repo_root: Path
    repo_id: Optional[str] = None
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS

    @property
    def ticket_dir(self) -> Path:
        return self.repo_root.resolve() / ".codex-autorunner" / "tickets"

    def preflight(self, *, config: Any = None) -> PreflightReport:
        return ticket_flow_preflight(self.repo_root, config=config)

    def resolve_run_reuse(
        self,
        records: list[FlowRunRecord],
        *,
        force_new: bool,
    ) -> RunReuseResult:
        return resolve_run_reuse_policy(
            records,
            force_new=force_new,
            ticket_dir=self.ticket_dir,
        )

    def ensure_worker(
        self, run_id: str, *, is_terminal: bool = False
    ) -> dict[str, Any]:
        return ensure_flow_worker(self.repo_root, run_id, is_terminal=is_terminal)

    def latest_dispatch(
        self,
        run_id: str,
        input_data: dict[str, Any],
        *,
        include_turn_summary: bool = False,
    ) -> Optional[dict[str, Any]]:
        return latest_ticket_flow_dispatch(
            self.repo_root,
            run_id,
            input_data,
            max_text_chars=self.max_text_chars,
            include_turn_summary=include_turn_summary,
        )

    def latest_reply_history_seq(self, run_id: str, input_data: dict[str, Any]) -> int:
        return latest_ticket_flow_reply_history_seq(self.repo_root, run_id, input_data)

    def resolve_paused_dispatch_state(
        self,
        *,
        record_status: FlowRunStatus,
        latest_payload: Mapping[str, Any],
        latest_reply_seq: int,
    ) -> tuple[bool, Optional[str]]:
        return resolve_paused_dispatch_state(
            repo_root=self.repo_root,
            record_status=record_status,
            latest_payload=latest_payload,
            latest_reply_seq=latest_reply_seq,
        )

    def build_run_state(
        self,
        *,
        record: FlowRunRecord,
        store: FlowStore,
        has_pending_dispatch: bool,
        dispatch_state_reason: Optional[str] = None,
    ) -> TicketFlowRunState:
        return build_ticket_flow_run_state(
            repo_root=self.repo_root,
            repo_id=self.repo_id or self.repo_root.name,
            record=record,
            store=store,
            has_pending_dispatch=has_pending_dispatch,
            dispatch_state_reason=dispatch_state_reason,
        )

    def build_status_snapshot(
        self,
        record: FlowRunRecord,
        store: Optional[FlowStore],
        *,
        lite: bool = False,
    ) -> dict[str, Any]:
        return build_ticket_flow_status_snapshot(
            self.repo_root,
            record,
            store,
            lite=lite,
        )

    def latest_run_state_with_record(
        self, *, store: Optional[FlowStore] = None
    ) -> tuple[Optional[TicketFlowRunState], Optional[FlowRunRecord]]:
        return get_latest_ticket_flow_run_state_with_record(
            self.repo_root,
            self.repo_id or self.repo_root.name,
            store=store,
            max_text_chars=self.max_text_chars,
        )


def build_ticket_flow_operator_service(
    repo_root: Path,
    *,
    repo_id: Optional[str] = None,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> TicketFlowOperatorService:
    return TicketFlowOperatorService(
        repo_root=repo_root.resolve(),
        repo_id=repo_id,
        max_text_chars=max_text_chars,
    )


def _truncate(text: Optional[str], limit: Optional[int]) -> str:
    raw = text or ""
    if limit is None or len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 3)] + "..."


def _trim_extra(extra: Any, limit: Optional[int]) -> Any:
    if extra is None:
        return None
    if limit is None:
        return extra
    if isinstance(extra, str):
        return _truncate(extra, limit)
    try:
        raw = json.dumps(extra, ensure_ascii=True, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(extra)
    if len(raw) <= limit:
        return extra
    return {
        "_omitted": True,
        "note": "extra omitted due to size",
        "preview": _truncate(raw, limit),
    }


def _dispatch_dict(
    dispatch: Dispatch,
    *,
    max_text_chars: Optional[int],
) -> dict[str, Any]:
    return {
        "mode": dispatch.mode,
        "title": _truncate(dispatch.title, max_text_chars),
        "body": _truncate(dispatch.body, max_text_chars),
        "extra": _trim_extra(dispatch.extra, max_text_chars),
        "is_handoff": dispatch.is_handoff,
    }


def _resolve_workspace_root(record_input: dict[str, Any], repo_root: Path) -> Path:
    return resolve_ticket_flow_workspace_root(
        record_input,
        repo_root,
        enforce_repo_boundary=True,
    )


def latest_ticket_flow_reply_history_seq(
    repo_root: Path, run_id: str, record_input: dict[str, Any]
) -> int:
    try:
        workspace_root = _resolve_workspace_root(record_input, repo_root)
        reply_paths = resolve_reply_paths(workspace_root=workspace_root, run_id=run_id)
        history_dir = reply_paths.reply_history_dir
        if not history_dir.exists() or not history_dir.is_dir():
            return 0
        latest = 0
        for child in history_dir.iterdir():
            if child.is_dir() and len(child.name) == 4 and child.name.isdigit():
                latest = max(latest, int(child.name))
        return latest
    except (OSError, ValueError) as exc:
        logger.warning("Could not get latest reply history seq: %s", exc)
        return 0


def latest_ticket_flow_dispatch(
    repo_root: Path,
    run_id: str,
    input_data: dict[str, Any],
    *,
    max_text_chars: Optional[int] = None,
    include_turn_summary: bool = False,
) -> Optional[dict[str, Any]]:
    try:
        workspace_root = _resolve_workspace_root(input_data, repo_root)
        outbox_paths = resolve_outbox_paths(
            workspace_root=workspace_root,
            run_id=run_id,
        )
        history_dir = outbox_paths.dispatch_history_dir
        if not history_dir.exists() or not history_dir.is_dir():
            return None

        seq_dirs = [
            child
            for child in history_dir.iterdir()
            if child.is_dir() and len(child.name) == 4 and child.name.isdigit()
        ]
        if not seq_dirs:
            return None

        def _list_files(dispatch_dir: Path) -> list[str]:
            files: list[str] = []
            for child in sorted(dispatch_dir.iterdir(), key=lambda p: p.name):
                if child.name.startswith(".") or child.name == "DISPATCH.md":
                    continue
                if child.is_file():
                    files.append(child.name)
            return files

        seq_dirs = sorted(seq_dirs, key=lambda p: p.name, reverse=True)
        latest_seq = int(seq_dirs[0].name)
        handoff_candidate: Optional[dict[str, Any]] = None
        non_summary_candidate: Optional[dict[str, Any]] = None
        turn_summary_candidate: Optional[dict[str, Any]] = None
        error_candidate: Optional[dict[str, Any]] = None

        for seq_dir in seq_dirs:
            seq = int(seq_dir.name)
            dispatch_path = seq_dir / "DISPATCH.md"
            dispatch, errors = parse_dispatch(dispatch_path)
            if errors or dispatch is None:
                if seq == latest_seq:
                    return {
                        "seq": seq,
                        "latest_seq": latest_seq,
                        "dir": safe_relpath(seq_dir, repo_root),
                        "dispatch": None,
                        "errors": errors,
                        "files": [],
                    }
                if error_candidate is None:
                    error_candidate = {
                        "seq": seq,
                        "dir": seq_dir,
                        "errors": errors,
                    }
                continue

            candidate = {"seq": seq, "dir": seq_dir, "dispatch": dispatch}
            if dispatch.is_handoff and handoff_candidate is None:
                handoff_candidate = candidate
            if dispatch.mode != "turn_summary" and non_summary_candidate is None:
                non_summary_candidate = candidate
            if dispatch.mode == "turn_summary" and turn_summary_candidate is None:
                turn_summary_candidate = candidate
            if handoff_candidate and non_summary_candidate and turn_summary_candidate:
                break

        selected = handoff_candidate or non_summary_candidate or turn_summary_candidate
        if selected is None:
            if error_candidate is None:
                return None
            return {
                "seq": int(error_candidate["seq"]),
                "latest_seq": latest_seq,
                "dir": safe_relpath(error_candidate["dir"], repo_root),
                "dispatch": None,
                "errors": list(error_candidate["errors"]),
                "files": [],
            }

        selected_dir = selected["dir"]
        payload: dict[str, Any] = {
            "seq": int(selected["seq"]),
            "latest_seq": latest_seq,
            "dir": safe_relpath(selected_dir, repo_root),
            "dispatch": _dispatch_dict(
                selected["dispatch"],
                max_text_chars=max_text_chars,
            ),
            "errors": [],
            "files": _list_files(selected_dir),
        }
        if include_turn_summary and turn_summary_candidate is not None:
            payload["turn_summary_seq"] = int(turn_summary_candidate["seq"])
            payload["turn_summary"] = _dispatch_dict(
                turn_summary_candidate["dispatch"],
                max_text_chars=max_text_chars,
            )
        return payload
    except Exception as exc:  # intentional: shared surface should degrade safely
        logger.warning("Could not get latest dispatch: %s", exc)
        return None


def dispatch_is_actionable(dispatch_payload: Any) -> bool:
    if not isinstance(dispatch_payload, dict):
        return False
    if bool(dispatch_payload.get("is_handoff")):
        return True
    mode = str(dispatch_payload.get("mode") or "").strip().lower()
    return mode == "pause"


def ticket_flow_inbox_preflight(repo_root: Path) -> PreflightCheckResult:
    repo_root = repo_root.resolve()
    if not repo_root.exists():
        return PreflightCheckResult(
            is_recoverable=False,
            reason_code="invalid_state",
            reason=f"Ticket flow workspace is missing: {repo_root}",
        )

    state_root = repo_root / ".codex-autorunner"
    if not state_root.exists() or not state_root.is_dir():
        return PreflightCheckResult(
            is_recoverable=False,
            reason_code="deleted_context",
            reason=(
                "Ticket flow preflight failed because runtime state is missing at "
                f"{safe_relpath(state_root, repo_root)}"
            ),
        )

    ticket_dir = state_root / "tickets"
    if not ticket_dir.exists() or not ticket_dir.is_dir():
        return PreflightCheckResult(
            is_recoverable=False,
            reason_code="deleted_context",
            reason=(
                "Ticket flow preflight failed because the ticket directory is missing at "
                f"{safe_relpath(ticket_dir, repo_root)}"
            ),
        )

    try:
        if list_ticket_paths(ticket_dir):
            return PreflightCheckResult(is_recoverable=True)
    except (OSError, ValueError) as exc:
        logger.warning("Could not inspect ticket dir for inbox preflight: %s", exc)
        return PreflightCheckResult(is_recoverable=True)

    return PreflightCheckResult(
        is_recoverable=False,
        reason_code="no_tickets",
        reason=(
            "Ticket flow preflight failed because no tickets remain in "
            f"{safe_relpath(ticket_dir, repo_root)}"
        ),
    )


@dataclass(frozen=True)
class PreflightCheckResult:
    is_recoverable: bool
    reason_code: Optional[str] = None
    reason: Optional[str] = None


def _paused_dispatch_resume_invalid_reason(repo_root: Path) -> Optional[str]:
    preflight = ticket_flow_inbox_preflight(repo_root)
    if preflight.is_recoverable:
        return None
    if preflight.reason_code == "no_tickets":
        return (
            "Latest dispatch is stale; ticket flow resume preflight would fail because "
            f"no tickets remain in {safe_relpath(repo_root / '.codex-autorunner' / 'tickets', repo_root)}"
        )
    if preflight.reason:
        return (
            "Latest dispatch is stale; ticket flow resume preflight would fail: "
            + preflight.reason
        )
    return (
        "Latest dispatch is stale; ticket flow resume preflight would fail "
        f"in {safe_relpath(repo_root, repo_root)}"
    )


def resolve_paused_dispatch_state(
    *,
    repo_root: Path,
    record_status: FlowRunStatus,
    latest_payload: Mapping[str, Any],
    latest_reply_seq: int,
) -> tuple[bool, Optional[str]]:
    seq = int(latest_payload.get("seq") or 0)
    latest_seq = int(latest_payload.get("latest_seq") or 0)
    dispatch_payload = latest_payload.get("dispatch")
    dispatch_actionable = dispatch_is_actionable(dispatch_payload)
    has_dispatch = bool(dispatch_actionable and seq > 0 and latest_reply_seq < seq)
    if record_status == FlowRunStatus.PAUSED and has_dispatch and latest_seq > seq:
        preflight_invalid_reason = _paused_dispatch_resume_invalid_reason(repo_root)
        if preflight_invalid_reason:
            return False, preflight_invalid_reason

    if record_status != FlowRunStatus.PAUSED or has_dispatch:
        return has_dispatch, None

    if latest_payload.get("errors"):
        return False, "Paused run has unreadable dispatch metadata"
    if dispatch_actionable and seq > 0 and latest_reply_seq >= seq:
        return False, "Latest dispatch already replied; run is still paused"
    if (
        dispatch_payload
        and not dispatch_actionable
        and seq > 0
        and latest_reply_seq < seq
    ):
        return False, "Latest dispatch is informational and does not require reply"
    return False, "Run is paused without an actionable dispatch"


def select_active_or_paused_run(
    records: list[FlowRunRecord],
) -> Optional[FlowRunRecord]:
    for record in records:
        if record.status in (FlowRunStatus.RUNNING, FlowRunStatus.PAUSED):
            return record
    return None


def select_resumable_run(
    records: list[FlowRunRecord],
) -> tuple[Optional[FlowRunRecord], str]:
    if not records:
        return None, "new_run"
    active = select_active_or_paused_run(records)
    if active is not None:
        return active, "active"
    latest = records[0]
    if latest.status == FlowRunStatus.COMPLETED:
        return latest, "completed_pending"
    return None, "new_run"


def resolve_run_reuse_policy(
    records: list[FlowRunRecord],
    *,
    force_new: bool,
    ticket_dir: Path,
) -> RunReuseResult:
    stale = tuple(
        record
        for record in records
        if record.status in (FlowRunStatus.FAILED, FlowRunStatus.STOPPED)
    )
    if force_new:
        return RunReuseResult(action="start_new", stale_terminal_runs=stale)

    existing_run, reason = select_resumable_run(records)
    if existing_run is not None and reason == "active":
        return RunReuseResult(
            action="reuse_active",
            run=existing_run,
            stale_terminal_runs=stale,
        )
    if existing_run is not None and reason == "completed_pending":
        pending = sum(
            1 for path in list_ticket_paths(ticket_dir) if not ticket_is_done(path)
        )
        return RunReuseResult(
            action="completed_pending",
            run=existing_run,
            pending_ticket_count=pending,
            stale_terminal_runs=stale,
        )
    return RunReuseResult(action="start_new", stale_terminal_runs=stale)


def _ticket_lint_details(ticket_dir: Path) -> dict[str, list[str]]:
    policy = evaluate_ticket_start_policy(ticket_dir)
    return {
        "invalid_filenames": list(policy.invalid_filenames),
        "duplicate_indices": list(policy.duplicate_indices),
        "frontmatter": list(policy.frontmatter),
    }


def ticket_flow_preflight(repo_root: Path, *, config: Any = None) -> PreflightReport:
    repo_root = repo_root.resolve()
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    checks: list[PreflightCheck] = []
    state_root = repo_root / ".codex-autorunner"

    if state_root.exists():
        checks.append(
            PreflightCheck(
                check_id="repo_initialized",
                status="ok",
                message="Repo initialized (.codex-autorunner present).",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="repo_initialized",
                status="error",
                message="Repo not initialized (.codex-autorunner missing).",
                fix="Run `car init` in the repo root.",
            )
        )

    if ticket_dir.exists():
        checks.append(
            PreflightCheck(
                check_id="ticket_dir",
                status="ok",
                message=f"Ticket directory found: {safe_relpath(ticket_dir, repo_root)}.",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="ticket_dir",
                status="error",
                message="Ticket directory missing.",
                fix="Run `car ticket-flow bootstrap` to create the ticket dir and seed TICKET-001.",
            )
        )

    ticket_paths = (
        list_ticket_paths(ticket_dir)
        if ticket_dir.exists() and ticket_dir.is_dir()
        else []
    )
    if ticket_paths:
        checks.append(
            PreflightCheck(
                check_id="tickets_present",
                status="ok",
                message=f"Found {len(ticket_paths)} ticket(s).",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="tickets_present",
                status="error",
                message="No tickets found.",
                fix="Create tickets under .codex-autorunner/tickets or run `car ticket-flow bootstrap`.",
            )
        )

    lint_details = _ticket_lint_details(ticket_dir)
    if lint_details["invalid_filenames"]:
        checks.append(
            PreflightCheck(
                check_id="ticket_filenames",
                status="error",
                message="Invalid ticket filenames detected.",
                fix="Rename tickets to TICKET-<number>[suffix].md (e.g. TICKET-001-foo.md).",
                details=lint_details["invalid_filenames"],
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="ticket_filenames",
                status="ok",
                message="Ticket filenames are valid.",
            )
        )

    if lint_details["duplicate_indices"]:
        checks.append(
            PreflightCheck(
                check_id="duplicate_indices",
                status="error",
                message="Duplicate ticket indices detected.",
                fix="Rename or remove duplicates so each index is unique.",
                details=lint_details["duplicate_indices"],
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="duplicate_indices",
                status="ok",
                message="Ticket indices are unique.",
            )
        )

    if lint_details["frontmatter"]:
        checks.append(
            PreflightCheck(
                check_id="frontmatter",
                status="error",
                message="Ticket frontmatter validation failed.",
                fix="Fix the YAML frontmatter in the listed tickets.",
                details=lint_details["frontmatter"],
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="frontmatter",
                status="ok",
                message="Ticket frontmatter passes validation.",
            )
        )

    ticket_docs = []
    for path in ticket_paths:
        doc, errors = read_ticket(path)
        if doc is not None and not errors:
            ticket_docs.append(doc)

    if not ticket_docs:
        checks.append(
            PreflightCheck(
                check_id="agents",
                status="warning",
                message="Agent availability skipped (no valid tickets to inspect).",
            )
        )
        return PreflightReport(checks=checks)

    agents = sorted({doc.frontmatter.agent for doc in ticket_docs})
    agent_errors: list[str] = []
    agent_warnings: list[str] = []

    if "codex" in agents:
        app_cmd = getattr(getattr(config, "app_server", None), "command", None) or []
        app_binary = app_cmd[0] if app_cmd else None
        resolved = resolve_executable(app_binary) if app_binary else None
        if config is not None and not resolved:
            agent_errors.append("codex: app_server command not available in PATH.")

    if "opencode" in agents and config is not None:
        opencode_binary: Optional[str] = None
        serve_command = getattr(config, "agent_serve_command", None)
        if callable(serve_command):
            opencode_cmd = serve_command("opencode")
            if opencode_cmd:
                opencode_binary = resolve_executable(opencode_cmd[0])
        if not opencode_binary:
            agent_binary = getattr(config, "agent_binary", None)
            if callable(agent_binary):
                try:
                    opencode_binary = resolve_executable(agent_binary("opencode"))
                except ConfigError:
                    opencode_binary = None
        if not opencode_binary:
            agent_errors.append(
                "opencode: backend unavailable (missing binary/serve command)."
            )

    for agent in agents:
        if agent in ("codex", "opencode", "user"):
            continue
        agent_warnings.append(
            f"{agent}: availability not verified; ensure its backend is configured."
        )

    if agent_errors:
        checks.append(
            PreflightCheck(
                check_id="agents",
                status="error",
                message="Agent backend validation failed.",
                details=agent_errors,
            )
        )
    elif agent_warnings:
        checks.append(
            PreflightCheck(
                check_id="agents",
                status="warning",
                message="Some agents could not be verified automatically.",
                details=agent_warnings,
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="agents",
                status="ok",
                message="All referenced agents appear available.",
            )
        )

    return PreflightReport(checks=checks)


def ticket_progress(repo_root: Path) -> dict[str, int]:
    census = collect_ticket_flow_census(repo_root)
    return {"done": census.done_count, "total": census.total_count}


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
            (record for record in records if not record.status.is_terminal()),
            None,
        )
    raise ValueError(f"Unsupported ticket flow run selection: {selection}")


def select_ticket_flow_run(
    store: FlowStore,
    *,
    selection: TicketFlowRunSelection,
) -> Optional[FlowRunRecord]:
    return select_ticket_flow_run_record(
        store.list_flow_runs(flow_type="ticket_flow"),
        selection=selection,
    )


def select_default_ticket_flow_run(store: FlowStore) -> Optional[FlowRunRecord]:
    return select_ticket_flow_run(store, selection="authoritative")


def _derive_effective_current_ticket(
    record: FlowRunRecord,
    store: Optional[FlowStore],
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
            record.id,
            [FlowEventType.STEP_STARTED],
        )
        last_finished = store.get_last_event_seq_by_types(
            record.id,
            [FlowEventType.STEP_COMPLETED, FlowEventType.STEP_FAILED],
        )
        in_progress = bool(
            last_started is not None
            and (last_finished is None or last_started > last_finished)
        )
        if not in_progress:
            return None
        return store.get_latest_step_progress_current_ticket(
            record.id,
            after_seq=last_finished,
        )
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        return None


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
        latest = latest_ticket_flow_dispatch(
            repo_root,
            str(record.id),
            dict(record.input_data or {}),
            max_text_chars=DEFAULT_MAX_TEXT_CHARS,
        )
        latest_payload = latest if isinstance(latest, dict) else {}
        latest_reply_seq = latest_ticket_flow_reply_history_seq(
            repo_root,
            str(record.id),
            dict(record.input_data or {}),
        )
        has_dispatch, reason = resolve_paused_dispatch_state(
            repo_root=repo_root,
            record_status=record.status,
            latest_payload=latest_payload,
            latest_reply_seq=latest_reply_seq,
        )
        run_state = build_ticket_flow_run_state(
            repo_root=repo_root,
            repo_id=repo_root.name,
            record=record,
            store=store,
            has_pending_dispatch=has_dispatch,
            dispatch_state_reason=reason,
        )
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
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
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _resolve_ticket_path(repo_root: Path, ticket_ref: str) -> Optional[Path]:
    candidate = Path(ticket_ref)
    candidates: list[Path]
    if candidate.is_absolute():
        candidates = [candidate]
    else:
        candidates = [
            repo_root / candidate,
            repo_root / ".codex-autorunner" / "tickets" / candidate,
        ]
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _read_ticket_flow_app_metadata(
    repo_root: Path, ticket_ref: Optional[str]
) -> Optional[dict[str, str]]:
    if not ticket_ref:
        return None
    ticket_path = _resolve_ticket_path(repo_root, ticket_ref)
    if ticket_path is None:
        return None
    try:
        data, _body = parse_markdown_frontmatter(
            ticket_path.read_text(encoding="utf-8")
        )
    except OSError:
        return None
    app_id = _normalize_optional_text(data.get("app"))
    if app_id is None:
        return None
    metadata = {"id": app_id}
    app_version = _normalize_optional_text(data.get("app_version"))
    if app_version is not None:
        metadata["version"] = app_version
    app_source = _normalize_optional_text(data.get("app_source"))
    if app_source is not None:
        metadata["source"] = app_source
    return metadata


def build_ticket_flow_status_snapshot(
    repo_root: Path,
    record: FlowRunRecord,
    store: Optional[FlowStore],
    *,
    lite: bool = False,
) -> dict[str, Any]:
    state = record.state or {}
    current_ticket = None
    if isinstance(state, dict):
        ticket_engine = state.get("ticket_engine")
        if isinstance(ticket_engine, dict):
            current_ticket = ticket_engine.get("current_ticket")
            if not (isinstance(current_ticket, str) and current_ticket.strip()):
                current_ticket = None

    effective_ticket = current_ticket or _derive_effective_current_ticket(record, store)
    app_metadata = _read_ticket_flow_app_metadata(repo_root, effective_ticket)
    updated_state: Optional[dict[str, Any]] = None
    if effective_ticket and not current_ticket and isinstance(state, dict):
        ticket_engine = state.get("ticket_engine")
        ticket_engine = dict(ticket_engine) if isinstance(ticket_engine, dict) else {}
        ticket_engine["current_ticket"] = effective_ticket
        updated_state = dict(state)
        updated_state["ticket_engine"] = ticket_engine

    if lite:
        return {
            "last_event_seq": None,
            "last_event_at": None,
            "worker_health": None,
            "effective_current_ticket": effective_ticket,
            "app": app_metadata,
            "ticket_progress": None,
            "state": updated_state,
            "canonical_state_v1": None,
            "freshness": None,
        }

    last_event_seq = None
    last_event_at = None
    if store is not None:
        try:
            last_event_seq, last_event_at = store.get_last_event_meta(record.id)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            last_event_seq, last_event_at = None, None

    health = check_worker_health(repo_root, record.id)
    canonical_state = _canonical_flow_status_state(repo_root, record, store)
    freshness = (
        canonical_state.get("freshness") if isinstance(canonical_state, dict) else None
    )
    return {
        "last_event_seq": last_event_seq,
        "last_event_at": last_event_at,
        "worker_health": health,
        "effective_current_ticket": effective_ticket,
        "app": app_metadata,
        "ticket_progress": ticket_progress(repo_root),
        "state": updated_state,
        "canonical_state_v1": canonical_state,
        "freshness": freshness,
    }


def ensure_flow_worker(
    repo_root: Path,
    run_id: str,
    *,
    is_terminal: bool = False,
    check_worker_health_fn=check_worker_health,
    clear_worker_metadata_fn=clear_worker_metadata,
    spawn_flow_worker_fn=spawn_flow_worker,
) -> dict[str, Any]:
    health = check_worker_health_fn(repo_root, run_id)
    if not is_terminal and health.status in {"dead", "mismatch", "invalid"}:
        try:
            clear_worker_metadata_fn(health.artifact_path.parent)
        except OSError:
            pass
    if health.is_alive:
        return {"status": "reused", "health": health}

    proc, stdout_handle, stderr_handle = spawn_flow_worker_fn(repo_root, run_id)
    for stream in (stdout_handle, stderr_handle):
        try:
            stream.close()
        except OSError:
            pass
    return {
        "status": "spawned",
        "health": health,
        "proc": proc,
        "stdout": None,
        "stderr": None,
    }


def _ticket_flow_recommended_actions(
    *,
    repo_root: Path,
    state: str,
    record_status: FlowRunStatus,
    run_id: str,
    has_pending_dispatch: bool,
) -> list[str]:
    quoted_repo = shlex.quote(str(repo_root))
    archive_cmd = f"car ticket-flow archive --repo {quoted_repo} --run-id {run_id}"
    status_cmd = f"car ticket-flow status --repo {quoted_repo} --run-id {run_id}"
    resume_cmd = f"car ticket-flow start --repo {quoted_repo}"
    start_cmd = f"car ticket-flow start --repo {quoted_repo}"
    stop_cmd = f"car ticket-flow stop --repo {quoted_repo} --run-id {run_id}"
    if state == "completed":
        return [start_cmd]
    if record_status in {FlowRunStatus.FAILED, FlowRunStatus.STOPPED}:
        return [archive_cmd, status_cmd]
    if state == "dead":
        return [f"{resume_cmd} --force-new", status_cmd, stop_cmd]
    if record_status == FlowRunStatus.PAUSED:
        if has_pending_dispatch:
            return [resume_cmd, status_cmd, stop_cmd]
        return [f"{resume_cmd} --force-new", status_cmd, stop_cmd]
    if state == "blocked":
        return [f"{resume_cmd} --force-new", status_cmd, stop_cmd]
    return [status_cmd]


def build_ticket_flow_run_state(
    *,
    repo_root: Path,
    repo_id: str,
    record: FlowRunRecord,
    store: FlowStore,
    has_pending_dispatch: bool,
    dispatch_state_reason: Optional[str] = None,
) -> TicketFlowRunState:
    run_id = str(record.id)
    failure_payload = get_failure_payload(record)
    failure_summary = (
        format_failure_summary(failure_payload) if failure_payload is not None else None
    )
    state_payload = record.state if isinstance(record.state, Mapping) else {}
    reason_summary = state_payload.get("reason_summary")
    if not isinstance(reason_summary, str):
        reason_summary = None
    if reason_summary:
        reason_summary = reason_summary.strip() or None
    error_message = (
        record.error_message.strip()
        if isinstance(record.error_message, str) and record.error_message.strip()
        else None
    )

    current_ticket = store.get_latest_step_progress_current_ticket(run_id)
    if not current_ticket:
        engine = state_payload.get("ticket_engine")
        if isinstance(engine, dict):
            candidate = engine.get("current_ticket")
            if isinstance(candidate, str) and candidate.strip():
                current_ticket = candidate.strip()

    _, last_event_at = store.get_last_event_meta(run_id)
    last_progress_at = (
        last_event_at or record.started_at or record.created_at or record.finished_at
    )
    duration_seconds = flow_run_duration_seconds(record)

    health = None
    dead_worker = False
    if record.status in (
        FlowRunStatus.PAUSED,
        FlowRunStatus.RUNNING,
        FlowRunStatus.STOPPING,
    ):
        try:
            health = check_worker_health(repo_root, run_id)
            dead_worker = health.status in {"dead", "invalid", "mismatch"}
        except (OSError, ValueError) as exc:
            logger.warning("Could not check worker health: %s", exc)
            health = None
            dead_worker = False

    crash_info = None
    crash_summary = None
    if dead_worker:
        try:
            crash_info = read_worker_crash_info(repo_root, run_id)
        except Exception as exc:  # intentional: defensive postmortem guard
            logger.warning("Could not read worker crash info: %s", exc)
            crash_info = None
        if isinstance(crash_info, dict):
            parts: list[str] = []
            exception = crash_info.get("exception")
            if isinstance(exception, str) and exception.strip():
                parts.append(exception.strip())
            last_event = crash_info.get("last_event")
            if isinstance(last_event, str) and last_event.strip():
                parts.append(f"last_event={last_event.strip()}")
            exit_code = crash_info.get("exit_code")
            if isinstance(exit_code, int):
                parts.append(f"exit_code={exit_code}")
            signal = crash_info.get("signal")
            if isinstance(signal, str) and signal.strip():
                parts.append(f"signal={signal.strip()}")
            if parts:
                crash_summary = " | ".join(parts)

    state = "running"
    if record.status == FlowRunStatus.COMPLETED:
        state = "completed"
    elif dead_worker:
        state = "dead"
    elif record.status == FlowRunStatus.PAUSED:
        state = "paused" if has_pending_dispatch else "blocked"
    elif record.status in (FlowRunStatus.FAILED, FlowRunStatus.STOPPED):
        state = "blocked"

    is_terminal = record.status.is_terminal()
    attention_required = not is_terminal and (
        state in ("dead", "blocked") or record.status == FlowRunStatus.PAUSED
    )

    worker_status = None
    if is_terminal:
        worker_status = "exited_expected"
    elif dead_worker:
        worker_status = "dead_unexpected"
    elif health is not None and health.is_alive:
        worker_status = "alive"

    blocking_reason = None
    if state == "dead":
        detail = crash_summary or (health.message if health is not None else None)
        blocking_reason = (
            f"Worker not running ({detail})"
            if isinstance(detail, str) and detail.strip()
            else "Worker not running"
        )
    elif state == "blocked":
        blocking_reason = (
            dispatch_state_reason
            or failure_summary
            or reason_summary
            or error_message
            or "Run is blocked and needs operator attention"
        )
    elif record.status == FlowRunStatus.PAUSED:
        blocking_reason = reason_summary or "Waiting for user input"

    recommended_actions = _ticket_flow_recommended_actions(
        repo_root=repo_root,
        state=state,
        record_status=record.status,
        run_id=run_id,
        has_pending_dispatch=has_pending_dispatch,
    )

    return {
        "state": state,
        "blocking_reason": blocking_reason,
        "current_ticket": current_ticket,
        "last_progress_at": last_progress_at,
        "recommended_action": recommended_actions[0] if recommended_actions else None,
        "recommended_actions": recommended_actions,
        "attention_required": attention_required,
        "worker_status": worker_status,
        "crash": (
            {
                "summary": crash_summary,
                "open_url": f"/repos/{repo_id}/api/flows/{run_id}/artifact?kind=worker_crash",
                "path": f".codex-autorunner/flows/{run_id}/crash.json",
            }
            if isinstance(crash_info, dict)
            else None
        ),
        "flow_status": record.status.value,
        "duration_seconds": duration_seconds,
        "repo_id": repo_id,
        "run_id": run_id,
    }


def get_latest_ticket_flow_run_state_with_record(
    repo_root: Path,
    repo_id: str,
    *,
    store: Optional[FlowStore] = None,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> tuple[Optional[TicketFlowRunState], Optional[FlowRunRecord]]:
    def _load_from_store(
        active_store: FlowStore,
    ) -> tuple[Optional[TicketFlowRunState], Optional[FlowRunRecord]]:
        records = active_store.list_flow_runs(flow_type="ticket_flow")
        if not records:
            return None, None
        record = select_authoritative_run_record(records)
        if record is None:
            return None, None
        latest = latest_ticket_flow_dispatch(
            repo_root,
            str(record.id),
            dict(record.input_data or {}),
            max_text_chars=max_text_chars,
        )
        latest_payload = latest if isinstance(latest, dict) else {}
        latest_reply_seq = latest_ticket_flow_reply_history_seq(
            repo_root,
            str(record.id),
            dict(record.input_data or {}),
        )
        has_dispatch, reason = resolve_paused_dispatch_state(
            repo_root=repo_root,
            record_status=record.status,
            latest_payload=latest_payload,
            latest_reply_seq=latest_reply_seq,
        )
        run_state = build_ticket_flow_run_state(
            repo_root=repo_root,
            repo_id=repo_id,
            record=record,
            store=active_store,
            has_pending_dispatch=has_dispatch,
            dispatch_state_reason=reason,
        )
        return run_state, record

    if store is not None:
        return _load_from_store(store)

    db_path = resolve_repo_flows_db_path(repo_root)
    if not db_path.exists():
        return None, None

    active_store = FlowStore.connect_readonly(db_path)
    try:
        active_store.initialize()
        return _load_from_store(active_store)
    except (OSError, RuntimeError, ValueError):
        return None, None
    finally:
        active_store.close()


def summarize_flow_freshness(payload: Any) -> Optional[str]:
    if not isinstance(payload, Mapping):
        return None
    status_raw = payload.get("status")
    status = str(status_raw).strip().lower() if status_raw is not None else ""
    if not status:
        return None
    parts = [status]
    basis = payload.get("recency_basis")
    basis_label = basis.replace("_", " ") if isinstance(basis, str) and basis else None
    age_seconds = payload.get("age_seconds")
    age_text = None
    if isinstance(age_seconds, int):
        if age_seconds < 60:
            age_text = f"{age_seconds}s ago"
        elif age_seconds < 3600:
            age_text = f"{age_seconds // 60}m ago"
        elif age_seconds < 86400:
            age_text = f"{age_seconds // 3600}h ago"
        else:
            age_text = f"{age_seconds // 86400}d ago"
    if basis_label and age_text:
        parts.append(f"{basis_label} {age_text}")
    elif basis_label:
        parts.append(basis_label)
    elif age_text:
        parts.append(age_text)
    return " · ".join(parts)


__all__ = [
    "DEFAULT_MAX_TEXT_CHARS",
    "PreflightCheck",
    "PreflightCheckResult",
    "PreflightReport",
    "RunReuseResult",
    "TicketFlowOperatorService",
    "TicketFlowRunSelection",
    "TicketFlowRunState",
    "TicketFlowWorkerCrash",
    "build_ticket_flow_operator_service",
    "build_ticket_flow_run_state",
    "build_ticket_flow_status_snapshot",
    "dispatch_is_actionable",
    "ensure_flow_worker",
    "get_latest_ticket_flow_run_state_with_record",
    "latest_ticket_flow_dispatch",
    "latest_ticket_flow_reply_history_seq",
    "resolve_paused_dispatch_state",
    "resolve_run_reuse_policy",
    "select_active_or_paused_run",
    "select_default_ticket_flow_run",
    "select_resumable_run",
    "select_ticket_flow_run",
    "select_ticket_flow_run_record",
    "summarize_flow_freshness",
    "ticket_flow_inbox_preflight",
    "ticket_flow_preflight",
    "ticket_progress",
]
