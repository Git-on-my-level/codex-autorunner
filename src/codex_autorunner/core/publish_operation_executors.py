from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional, cast

from ..manifest import ManifestError, load_manifest
from .config import load_hub_config
from .orchestration.models import MessageRequest, MessageRequestKind
from .pma_chat_delivery import (
    deliver_pma_notification,
    notify_preferred_bound_chat_for_workspace,
    notify_primary_pma_chat_for_repo,
    start_bound_chat_live_progress_for_thread,
)
from .pma_thread_store import ManagedThreadNotActiveError, PmaThreadStore
from .pr_bindings import PrBinding, PrBindingStore
from .publish_executor import (
    PublishActionExecutor,
    RetryablePublishError,
    TerminalPublishError,
)
from .publish_journal import PublishJournalStore, PublishOperation
from .scm_events import ScmEvent, ScmEventStore
from .scm_feedback_bundle import (
    apply_feedback_bundle_to_publish_payload,
    extract_feedback_bundle,
    merge_feedback_bundles,
)
from .scm_observability import correlation_id_for_operation, correlation_id_from_payload
from .text_utils import _coerce_int, _normalize_optional_text, _parse_iso_timestamp

_LOGGER = logging.getLogger(__name__)
_MANAGED_TURN_START_CONFIRMATION_TIMEOUT_SECONDS = 120
_MANAGED_TURN_START_CONFIRMATION_INITIAL_RETRY_SECONDS = 5
_MANAGED_TURN_START_CONFIRMATION_MAX_RETRY_SECONDS = 30
_MANAGED_TURN_QUEUE_WAIT_RETRY_SECONDS = 30
_MANAGED_TURN_START_CONFIRMATION_MAX_ATTEMPTS = 12
# Counts notify_chat retries after enqueue succeeded; not incremented while
# waiting for enqueue_managed_turn so backlog cannot exhaust the start window.
_MANAGED_TURN_POST_ENQUEUE_WAIT_CYCLES_KEY = "_managed_turn_post_enqueue_wait_cycles"
_RUNTIME_STARTED_AT_KEY = "runtime_started_at"


def _require_text(value: Any, *, field_name: str) -> str:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        raise TerminalPublishError(f"Publish payload is missing '{field_name}'")
    return normalized


def _normalize_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_scm_tracking(payload: dict[str, Any]) -> dict[str, Any]:
    return _normalize_mapping(payload.get("scm_reaction"))


def _normalize_request_kind(value: Any) -> MessageRequestKind:
    return "review" if _normalize_optional_text(value) == "review" else "message"


def _operation_digest(operation: PublishOperation, *, prefix: str) -> str:
    digest = hashlib.sha256(
        f"{operation.operation_kind}:{operation.operation_key}".encode("utf-8")
    ).hexdigest()[:24]
    return f"{prefix}:{digest}"


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


def _managed_turn_request(
    thread_target_id: str,
    payload: dict[str, Any],
) -> tuple[MessageRequest, Optional[Any]]:
    request_payload = _normalize_mapping(payload.get("request"))
    source = request_payload or payload
    message_text = _normalize_optional_text(
        source.get("message_text") or source.get("prompt") or source.get("body")
    )
    if message_text is None:
        raise TerminalPublishError(
            "Publish payload is missing managed-turn message_text"
        )

    metadata = _normalize_mapping(source.get("metadata"))
    scm_metadata = _normalize_mapping(metadata.get("scm"))
    correlation_id = correlation_id_from_payload(payload)
    if correlation_id is not None:
        scm_metadata["correlation_id"] = correlation_id
        metadata["scm"] = scm_metadata
    input_items_raw = source.get("input_items")
    input_items: Optional[list[dict[str, Any]]] = None
    if isinstance(input_items_raw, list):
        items = [dict(item) for item in input_items_raw if isinstance(item, dict)]
        if items:
            input_items = items

    request = MessageRequest(
        target_id=_normalize_optional_text(source.get("target_id")) or thread_target_id,
        target_kind="thread",
        message_text=message_text,
        kind=_normalize_request_kind(source.get("kind") or source.get("request_kind")),
        busy_policy="queue",
        model=_normalize_optional_text(source.get("model")),
        reasoning=_normalize_optional_text(source.get("reasoning")),
        approval_mode=_normalize_optional_text(source.get("approval_mode")),
        input_items=input_items,
        context_profile=source.get("context_profile"),
        metadata=metadata,
    )
    return request, payload.get("sandbox_policy")


def _managed_turn_result(
    *,
    thread_target_id: str,
    client_request_id: str,
    turn: dict[str, Any],
    existed: bool,
    correlation_id: Optional[str] = None,
) -> dict[str, Any]:
    status = _normalize_optional_text(turn.get("status")) or "unknown"
    result = {
        "thread_target_id": thread_target_id,
        "managed_turn_id": _require_text(
            turn.get("managed_turn_id"), field_name="managed_turn_id"
        ),
        "status": status,
        "queued": status == "queued",
        "client_request_id": client_request_id,
        "deduped": existed,
    }
    if correlation_id is not None:
        result["correlation_id"] = correlation_id
    return result


def _managed_turn_dependency_deadline(
    dependency: Mapping[str, Any],
    *,
    enqueue_operation: Optional[PublishOperation],
    turn: Optional[Mapping[str, Any]],
) -> datetime:
    explicit_deadline = _parse_iso_timestamp(dependency.get("deadline_at"))
    if explicit_deadline is not None:
        return explicit_deadline
    bases = [
        _parse_iso_timestamp(dependency.get("created_at")),
        _parse_iso_timestamp((turn or {}).get("started_at")),
        _parse_iso_timestamp(
            enqueue_operation.finished_at if enqueue_operation is not None else None
        ),
        _parse_iso_timestamp(
            enqueue_operation.started_at if enqueue_operation is not None else None
        ),
        _parse_iso_timestamp(
            enqueue_operation.created_at if enqueue_operation is not None else None
        ),
    ]
    base = next((value for value in bases if value is not None), None)
    if base is None:
        base = datetime.now(timezone.utc)
    timeout_seconds = _coerce_int(dependency.get("timeout_seconds"))
    # Missing or non-positive values use the default; explicit positive values are not clamped.
    if timeout_seconds <= 0:
        timeout_seconds = _MANAGED_TURN_START_CONFIRMATION_TIMEOUT_SECONDS
    return base + timedelta(seconds=timeout_seconds)


def _managed_turn_start_retry_seconds(operation: PublishOperation) -> float:
    attempt_index = max(int(operation.attempt_count or 1) - 1, 0)
    delay = _MANAGED_TURN_START_CONFIRMATION_INITIAL_RETRY_SECONDS * (2**attempt_index)
    return float(min(delay, _MANAGED_TURN_START_CONFIRMATION_MAX_RETRY_SECONDS))


def _managed_turn_queue_wait_retry_seconds() -> float:
    return float(_MANAGED_TURN_QUEUE_WAIT_RETRY_SECONDS)


def _managed_turn_post_enqueue_wait_exhausted(dependency: Mapping[str, Any]) -> bool:
    cycles = _coerce_int(dependency.get(_MANAGED_TURN_POST_ENQUEUE_WAIT_CYCLES_KEY))
    return cycles >= _MANAGED_TURN_START_CONFIRMATION_MAX_ATTEMPTS - 1


def _bump_managed_turn_post_enqueue_wait_cycles(
    journal: PublishJournalStore,
    operation_id: str,
    dependency: Mapping[str, Any],
) -> None:
    dep = dict(dependency)
    dep[_MANAGED_TURN_POST_ENQUEUE_WAIT_CYCLES_KEY] = (
        _coerce_int(dep.get(_MANAGED_TURN_POST_ENQUEUE_WAIT_CYCLES_KEY)) + 1
    )
    journal.patch_running_operation_payload(
        operation_id,
        {"managed_turn_dependency": dep},
    )


def _managed_turn_start_failure_message(
    failure_message: Optional[str],
    detail: str,
) -> str:
    base = failure_message or "Failed to wake the bound agent thread."
    return f"{base} {detail}".strip()


def _running_turn_blocking_queue(
    thread_store: PmaThreadStore,
    *,
    thread_target_id: str,
    queued_turn_id: str,
) -> Optional[dict[str, Any]]:
    running_turn = thread_store.get_running_turn(thread_target_id)
    if running_turn is None:
        return None
    if _normalize_optional_text(running_turn.get("managed_turn_id")) == queued_turn_id:
        return None
    return running_turn


def _log_scm_enqueue_managed_turn_queued(
    store: PmaThreadStore,
    *,
    thread_target_id: str,
    created: dict[str, Any],
    tracking: Mapping[str, Any],
    log_context: tuple[Any, ...],
) -> None:
    if not tracking or _normalize_optional_text(created.get("status")) != "queued":
        return
    blocking_turn = _running_turn_blocking_queue(
        store,
        thread_target_id=thread_target_id,
        queued_turn_id=_require_text(
            created.get("managed_turn_id"),
            field_name="managed_turn_id",
        ),
    )
    _LOGGER.info(
        "scm.enqueue_managed_turn.queued "
        "thread_target_id=%s managed_turn_id=%s "
        "blocking_managed_turn_id=%s correlation_id=%s "
        "binding_id=%s repo_slug=%s pr_number=%s",
        thread_target_id,
        created.get("managed_turn_id"),
        (
            _normalize_optional_text(blocking_turn.get("managed_turn_id"))
            if blocking_turn is not None
            else None
        ),
        *log_context,
    )


def _resolve_notify_message(
    *,
    operation: PublishOperation,
    payload: dict[str, Any],
    journal: PublishJournalStore,
    thread_store: PmaThreadStore,
) -> tuple[str, Optional[tuple[str, str, dict[str, Any]]]]:
    dependency = _normalize_mapping(payload.get("managed_turn_dependency"))
    if not dependency:
        return (
            _require_text(
                payload.get("message") or payload.get("body") or payload.get("text"),
                field_name="notify_chat message",
            ),
            None,
        )

    if (
        _normalize_optional_text(dependency.get("dependency_kind"))
        != "enqueue_managed_turn_started"
    ):
        raise TerminalPublishError(
            "notify_chat has unsupported managed_turn_dependency"
        )

    dependency_operation_id = _require_text(
        dependency.get("operation_id"),
        field_name="managed_turn_dependency.operation_id",
    )
    enqueue_operation = journal.get_operation(dependency_operation_id)
    if enqueue_operation is None:
        raise TerminalPublishError(
            f"notify_chat dependency operation '{dependency_operation_id}' was not found"
        )
    if enqueue_operation.state in {"pending", "running"}:
        raise RetryablePublishError(
            "Waiting for enqueue_managed_turn to finish",
            retry_after_seconds=_managed_turn_start_retry_seconds(operation),
        )

    failure_message = _normalize_optional_text(dependency.get("failure_message"))
    if enqueue_operation.state != "succeeded":
        if failure_message is not None:
            return failure_message, None
        raise TerminalPublishError(
            "notify_chat dependency enqueue_managed_turn did not succeed"
        )

    enqueue_response = _normalize_mapping(enqueue_operation.response)
    thread_target_id = _normalize_optional_text(
        enqueue_response.get("thread_target_id")
    ) or _normalize_optional_text(dependency.get("thread_target_id"))
    managed_turn_id = _normalize_optional_text(enqueue_response.get("managed_turn_id"))
    if thread_target_id is None or managed_turn_id is None:
        if failure_message is not None:
            return failure_message, None
        raise TerminalPublishError(
            "notify_chat dependency is missing managed-turn identifiers"
        )

    turn = thread_store.get_turn(thread_target_id, managed_turn_id)
    if turn is None:
        deadline = _managed_turn_dependency_deadline(
            dependency,
            enqueue_operation=enqueue_operation,
            turn=None,
        )
        if datetime.now(timezone.utc) >= deadline:
            if failure_message is not None:
                return (
                    _managed_turn_start_failure_message(
                        failure_message,
                        "The managed turn record never became visible before timeout.",
                    ),
                    None,
                )
            raise TerminalPublishError(
                "Managed turn record never became visible before timeout"
            )
        _bump_managed_turn_post_enqueue_wait_cycles(
            journal, operation.operation_id, dependency
        )
        raise RetryablePublishError(
            "Waiting for managed turn record to become visible",
            retry_after_seconds=_managed_turn_start_retry_seconds(operation),
        )
    turn_status = _normalize_optional_text(turn.get("status")) or "unknown"
    metadata = _normalize_mapping(turn.get("metadata"))
    runtime_started_at = _normalize_optional_text(metadata.get(_RUNTIME_STARTED_AT_KEY))
    if turn_status == "queued":
        blocking_turn = _running_turn_blocking_queue(
            thread_store,
            thread_target_id=thread_target_id,
            queued_turn_id=managed_turn_id,
        )
        _LOGGER.info(
            "notify_chat waiting for queued managed turn to reach front of queue "
            "thread_target_id=%s managed_turn_id=%s blocking_managed_turn_id=%s",
            thread_target_id,
            managed_turn_id,
            (
                _normalize_optional_text(blocking_turn.get("managed_turn_id"))
                if blocking_turn is not None
                else None
            ),
        )
        raise RetryablePublishError(
            "Waiting for queued managed turn to reach front of queue",
            retry_after_seconds=_managed_turn_queue_wait_retry_seconds(),
        )
    if runtime_started_at is not None:
        return (
            _require_text(
                payload.get("message") or payload.get("body") or payload.get("text"),
                field_name="notify_chat message",
            ),
            (thread_target_id, managed_turn_id, turn),
        )
    if turn_status in {"error", "interrupted", "ok"}:
        if failure_message is not None:
            return failure_message, None
        raise TerminalPublishError(
            f"Managed turn reached terminal status '{turn_status}' before start confirmation"
        )

    deadline = _managed_turn_dependency_deadline(
        dependency,
        enqueue_operation=enqueue_operation,
        turn=turn,
    )
    if datetime.now(
        timezone.utc
    ) >= deadline or _managed_turn_post_enqueue_wait_exhausted(dependency):
        detail = (
            "The execution was created but the runtime never launched it."
            if _normalize_optional_text(turn.get("backend_turn_id")) is None
            else "The managed turn did not reach a confirmed running state before timeout."
        )
        if failure_message is not None:
            return _managed_turn_start_failure_message(failure_message, detail), None
        raise TerminalPublishError(
            "Managed turn did not reach a confirmed running state before timeout"
        )
    _bump_managed_turn_post_enqueue_wait_cycles(
        journal, operation.operation_id, dependency
    )
    raise RetryablePublishError(
        "Waiting for managed turn start confirmation",
        retry_after_seconds=_managed_turn_start_retry_seconds(operation),
    )


def _maybe_start_bound_live_progress_for_notify(
    *,
    hub_root: Path,
    thread_store: PmaThreadStore,
    run_coroutine: Callable[[Coroutine[Any, Any, Any]], Any],
    workspace_root: Optional[Path],
    repo_id: Optional[str],
    confirmed: Optional[tuple[str, str, dict[str, Any]]],
) -> Optional[dict[str, Any]]:
    if confirmed is None:
        return None
    thread_target_id, managed_turn_id, turn = confirmed
    try:
        thread = thread_store.get_thread(thread_target_id) or {}
        raw_config = load_hub_config(hub_root).raw
        return cast(
            dict[str, Any],
            run_coroutine(
                start_bound_chat_live_progress_for_thread(
                    hub_root=hub_root,
                    raw_config=raw_config if isinstance(raw_config, Mapping) else {},
                    managed_thread_id=thread_target_id,
                    managed_turn_id=managed_turn_id,
                    agent=(
                        _normalize_optional_text(thread.get("agent_id"))
                        or _normalize_optional_text(thread.get("agent"))
                        or "agent"
                    ),
                    model=_normalize_optional_text(turn.get("model")),
                    workspace_root=workspace_root,
                    repo_id=repo_id,
                )
            ),
        )
    except Exception:
        _LOGGER.exception(
            "Failed to seed bound chat live progress for managed turn "
            "thread_target_id=%s managed_turn_id=%s",
            thread_target_id,
            managed_turn_id,
        )
        return None


def _merge_into_existing_queued_scm_turn(
    store: PmaThreadStore,
    *,
    thread_target_id: str,
    payload: dict[str, Any],
) -> Optional[dict[str, Any]]:
    incoming_bundle = extract_feedback_bundle(payload)
    if incoming_bundle is None:
        return None
    queued_turns = store.list_queued_turns(thread_target_id, limit=1)
    if not queued_turns:
        return None
    queued_turn = queued_turns[0]
    existing_payload = store.get_queued_turn_queue_payload(
        thread_target_id,
        queued_turn["managed_turn_id"],
    )
    if existing_payload is None:
        return None
    existing_bundle = extract_feedback_bundle(existing_payload)
    if existing_bundle is None:
        return None
    merged_bundle = merge_feedback_bundles(existing_bundle, incoming_bundle)
    updated_payload = apply_feedback_bundle_to_publish_payload(
        existing_payload,
        merged_bundle,
    )
    updated_request = updated_payload.get("request")
    if not isinstance(updated_request, Mapping):
        return None
    updated_prompt = _normalize_optional_text(updated_request.get("message_text"))
    if updated_prompt is None:
        return None
    updated_turn = store.update_queued_turn_request(
        thread_target_id,
        queued_turn["managed_turn_id"],
        prompt=updated_prompt,
        queue_payload=updated_payload,
    )
    if updated_turn is None:
        return None
    return updated_turn


def _resolve_scm_binding(
    hub_root: Path,
    *,
    tracking: Mapping[str, Any],
) -> Optional[PrBinding]:
    provider = _normalize_optional_text(tracking.get("provider"))
    repo_slug = _normalize_optional_text(tracking.get("repo_slug"))
    pr_number = _coerce_int(tracking.get("pr_number"))
    if provider is None or repo_slug is None or pr_number is None or pr_number <= 0:
        return None
    return PrBindingStore(hub_root).get_binding_by_pr(
        provider=provider,
        repo_slug=repo_slug,
        pr_number=pr_number,
    )


def _resolve_scm_event(
    hub_root: Path,
    *,
    tracking: Mapping[str, Any],
) -> Optional[ScmEvent]:
    event_id = _normalize_optional_text(tracking.get("event_id"))
    if event_id is None:
        return None
    return ScmEventStore(hub_root).get_event(event_id)


def _active_thread_record(
    store: PmaThreadStore,
    thread_target_id: Optional[str],
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    normalized_thread_target_id = _normalize_optional_text(thread_target_id)
    if normalized_thread_target_id is None:
        return None, None
    thread = store.get_thread(normalized_thread_target_id)
    if thread is None:
        return None, None
    lifecycle_status = _normalize_optional_text(thread.get("lifecycle_status"))
    if lifecycle_status != "active":
        return None, thread
    return normalized_thread_target_id, thread


def _thread_runtime_status(thread: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not isinstance(thread, Mapping):
        return None
    return _normalize_optional_text(thread.get("normalized_status"))


def _resolve_manifest_workspace(
    hub_root: Path,
    *,
    repo_id: Optional[str],
) -> Optional[Path]:
    normalized_repo_id = _normalize_optional_text(repo_id)
    if normalized_repo_id is None:
        return None
    try:
        manifest = load_manifest(load_hub_config(hub_root).manifest_path, hub_root)
    except (ManifestError, OSError, ValueError):
        return None
    entry = manifest.get(normalized_repo_id)
    if entry is None:
        return None
    workspace_root = (hub_root / entry.path).resolve()
    return workspace_root if workspace_root.exists() else None


def _resolve_scm_workspace_root(
    hub_root: Path,
    *,
    tracking: Mapping[str, Any],
    source_thread: Optional[Mapping[str, Any]],
) -> Optional[Path]:
    if source_thread is not None:
        workspace_text = _normalize_optional_text(source_thread.get("workspace_root"))
        if workspace_text is not None:
            workspace_root = Path(workspace_text)
            if workspace_root.exists():
                return workspace_root
    return _resolve_manifest_workspace(
        hub_root,
        repo_id=_normalize_optional_text(tracking.get("repo_id")),
    )


def _scm_pr_url(*, repo_slug: Optional[str], pr_number: Optional[int]) -> Optional[str]:
    if repo_slug is None or pr_number is None or pr_number <= 0:
        return None
    return f"https://github.com/{repo_slug}/pull/{pr_number}"


def _trimmed_summary(value: Any, *, limit: int = 140) -> Optional[str]:
    text = _normalize_optional_text(value)
    if text is None:
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3].rstrip()}..."


def _scm_trigger_summary(
    *,
    event: Optional[ScmEvent],
    tracking: Mapping[str, Any],
) -> Optional[str]:
    reaction_kind = _normalize_optional_text(tracking.get("reaction_kind"))
    if event is None:
        if reaction_kind is None:
            return None
        return reaction_kind.replace("_", " ")
    payload = _normalize_mapping(event.payload)
    if event.event_type == "check_run":
        name = _normalize_optional_text(payload.get("name"))
        conclusion = _normalize_optional_text(payload.get("conclusion"))
        if name and conclusion:
            return f"CI failed: {name} ({conclusion})"
        if name:
            return f"CI failed: {name}"
    if event.event_type == "pull_request_review":
        reviewer = _normalize_optional_text(payload.get("author_login"))
        state = _normalize_optional_text(payload.get("review_state"))
        if reviewer and state:
            return f"Review {state} from {reviewer}"
        if state:
            return f"Review {state}"
    if event.event_type in {"issue_comment", "pull_request_review_comment"}:
        reviewer = _normalize_optional_text(payload.get("author_login"))
        comment_id = _normalize_optional_text(payload.get("comment_id"))
        if reviewer and comment_id:
            return f"New review comment {comment_id} from {reviewer}"
        if reviewer:
            return f"New review comment from {reviewer}"
    if reaction_kind is None:
        return event.event_type
    return reaction_kind.replace("_", " ")


def _scm_review_focus_lines(
    *,
    event: Optional[ScmEvent],
) -> list[str]:
    if event is None:
        return []
    payload = _normalize_mapping(event.payload)
    body = _trimmed_summary(payload.get("body"))
    path = _normalize_optional_text(payload.get("path"))
    line = _coerce_int(payload.get("line"))
    location = path
    if location is not None and line is not None and line > 0:
        location = f"{location}:{line}"
    comment_id = _normalize_optional_text(payload.get("comment_id"))
    lines: list[str] = []
    if event.event_type in {"issue_comment", "pull_request_review_comment"}:
        detail = "Start with the latest review comment"
        if comment_id is not None:
            detail = f"{detail} {comment_id}"
        if location is not None:
            detail = f"{detail} at {location}"
        if body is not None:
            detail = f"{detail}: {body}"
        lines.append(detail)
    elif event.event_type == "pull_request_review" and body is not None:
        reviewer = _normalize_optional_text(payload.get("author_login"))
        if reviewer is not None:
            lines.append(f"Review summary from {reviewer}: {body}")
        else:
            lines.append(f"Review summary: {body}")
    return lines


def _build_scm_rebootstrap_message(
    *,
    binding: PrBinding,
    event: Optional[ScmEvent],
    tracking: Mapping[str, Any],
    previous_thread_target_id: str,
) -> str:
    subject = f"{binding.repo_slug}#{binding.pr_number}"
    pr_url = _scm_pr_url(repo_slug=binding.repo_slug, pr_number=binding.pr_number)
    lines = [
        "Bootstrap a fresh SCM PR follow-up session because the previous bound managed thread is archived or unavailable.",
        f"Target PR: {subject}",
    ]
    if pr_url is not None:
        lines.append(f"PR URL: {pr_url}")
    if binding.head_branch is not None:
        lines.append(f"Branch: {binding.head_branch}")
    trigger = _scm_trigger_summary(event=event, tracking=tracking)
    if trigger is not None:
        lines.append(f"Trigger: {trigger}")
    lines.append(f"Previous thread: {previous_thread_target_id}")
    focus_lines = _scm_review_focus_lines(event=event)
    if focus_lines:
        lines.append("Review focus:")
        lines.extend(f"- {line}" for line in focus_lines)
    lines.extend(
        [
            "Useful commands:",
            f"- gh pr view {binding.pr_number} --repo {binding.repo_slug} --comments",
            f"- gh pr checks {binding.pr_number} --repo {binding.repo_slug}",
            "Task:",
            "- Inspect the latest review comments and current PR status.",
            "- Address any unresolved feedback and any failing checks relevant to this PR.",
            "- If you make changes, push them to the PR branch.",
            "- Reply on the PR summarizing what you addressed.",
        ]
    )
    return "\n".join(lines)


def _build_scm_rebootstrap_request(
    request: MessageRequest,
    *,
    replacement_thread_target_id: str,
    previous_thread_target_id: str,
    binding: PrBinding,
    event: Optional[ScmEvent],
    tracking: Mapping[str, Any],
) -> MessageRequest:
    metadata = dict(request.metadata)
    scm_metadata = _normalize_mapping(metadata.get("scm"))
    scm_metadata["binding_id"] = binding.binding_id
    scm_metadata["previous_thread_target_id"] = previous_thread_target_id
    scm_metadata["thread_target_id"] = replacement_thread_target_id
    metadata["scm"] = scm_metadata
    return MessageRequest(
        target_id=replacement_thread_target_id,
        target_kind=request.target_kind,
        message_text=_build_scm_rebootstrap_message(
            binding=binding,
            event=event,
            tracking=tracking,
            previous_thread_target_id=previous_thread_target_id,
        ),
        kind=request.kind,
        busy_policy=request.busy_policy,
        agent_profile=request.agent_profile,
        model=request.model,
        reasoning=request.reasoning,
        approval_mode=request.approval_mode,
        input_items=request.input_items,
        context_profile=request.context_profile,
        metadata=metadata,
    )


def _repair_scm_thread_binding(
    hub_root: Path,
    store: PmaThreadStore,
    *,
    current_thread_target_id: str,
    request: MessageRequest,
    tracking: Mapping[str, Any],
    source_status: Optional[str] = None,
) -> tuple[str, MessageRequest]:
    binding = _resolve_scm_binding(hub_root, tracking=tracking)
    if binding is None:
        raise ManagedThreadNotActiveError(current_thread_target_id, source_status)
    source_thread = store.get_thread(current_thread_target_id)
    workspace_root = _resolve_scm_workspace_root(
        hub_root,
        tracking=tracking,
        source_thread=source_thread,
    )
    if workspace_root is None:
        raise ManagedThreadNotActiveError(current_thread_target_id, source_status)
    try:
        default_agent = load_hub_config(hub_root).pma.default_agent
    except (OSError, ValueError):
        default_agent = "codex"
    source_metadata = (
        _normalize_mapping(source_thread.get("metadata"))
        if isinstance(source_thread, Mapping)
        else {}
    )
    metadata = dict(source_metadata)
    scm_metadata = _normalize_mapping(metadata.get("scm"))
    scm_metadata.update(
        {
            "provider": binding.provider,
            "repo_slug": binding.repo_slug,
            "repo_id": binding.repo_id,
            "pr_number": binding.pr_number,
            "pr_url": _scm_pr_url(
                repo_slug=binding.repo_slug,
                pr_number=binding.pr_number,
            ),
        }
    )
    if binding.head_branch is not None:
        scm_metadata["head_branch"] = binding.head_branch
        metadata["head_branch"] = binding.head_branch
    if binding.base_branch is not None:
        scm_metadata["base_branch"] = binding.base_branch
    metadata["scm"] = scm_metadata
    metadata["pr_number"] = binding.pr_number
    if scm_metadata.get("pr_url") is not None:
        metadata["pr_url"] = scm_metadata["pr_url"]
    replacement = store.create_thread(
        _normalize_optional_text(
            source_thread.get("agent_id")
            if isinstance(source_thread, Mapping)
            else None
        )
        or default_agent
        or "codex",
        workspace_root,
        repo_id=binding.repo_id,
        resource_kind=_normalize_optional_text(
            source_thread.get("resource_kind")
            if isinstance(source_thread, Mapping)
            else None
        ),
        resource_id=_normalize_optional_text(
            source_thread.get("resource_id")
            if isinstance(source_thread, Mapping)
            else None
        ),
        name=_normalize_optional_text(
            source_thread.get("display_name")
            if isinstance(source_thread, Mapping)
            else None
        )
        or f"PR #{binding.pr_number} follow-up",
        metadata=metadata,
    )
    replacement_thread_target_id = _normalize_optional_text(
        replacement.get("managed_thread_id")
    )
    if replacement_thread_target_id is None:
        raise TerminalPublishError("Failed to create replacement managed thread")
    rebound = PrBindingStore(hub_root).attach_thread_target(
        provider=binding.provider,
        repo_slug=binding.repo_slug,
        pr_number=binding.pr_number,
        thread_target_id=replacement_thread_target_id,
    )
    effective_binding = rebound or binding
    event = _resolve_scm_event(hub_root, tracking=tracking)
    return replacement_thread_target_id, _build_scm_rebootstrap_request(
        request,
        replacement_thread_target_id=replacement_thread_target_id,
        previous_thread_target_id=current_thread_target_id,
        binding=effective_binding,
        event=event,
        tracking=tracking,
    )


def build_enqueue_managed_turn_executor(
    *,
    hub_root: Path,
    thread_store: Optional[PmaThreadStore] = None,
) -> PublishActionExecutor:
    store = thread_store or PmaThreadStore(hub_root)

    def executor(operation: PublishOperation) -> dict[str, Any]:
        payload = _normalize_mapping(operation.payload)
        correlation_id = correlation_id_for_operation(operation)
        requested_thread_target_id = _require_text(
            payload.get("thread_target_id"),
            field_name="thread_target_id",
        )
        tracking = _normalize_scm_tracking(payload)
        binding = _resolve_scm_binding(hub_root, tracking=tracking)
        thread_target_id, _active_thread = _active_thread_record(
            store,
            (
                binding.thread_target_id
                if binding is not None and binding.thread_target_id is not None
                else requested_thread_target_id
            ),
        )
        active_lifecycle_match_id = thread_target_id
        if thread_target_id is None:
            thread_target_id = requested_thread_target_id
        client_request_id = _operation_digest(operation, prefix="publish-turn")
        existing = store.get_turn_by_client_turn_id_any_thread(client_request_id)
        if existing is not None:
            return _managed_turn_result(
                thread_target_id=existing["managed_thread_id"],
                client_request_id=client_request_id,
                turn=existing,
                existed=True,
                correlation_id=correlation_id,
            )
        runtime_status = _thread_runtime_status(_active_thread)
        log_context = (
            correlation_id,
            _normalize_optional_text(tracking.get("binding_id")),
            _normalize_optional_text(tracking.get("repo_slug")),
            _coerce_int(tracking.get("pr_number")),
        )

        request, sandbox_policy = _managed_turn_request(thread_target_id, payload)
        queue_payload = {
            "request": request.to_dict(),
            "client_request_id": client_request_id,
            "sandbox_policy": sandbox_policy,
        }
        rebound_from_thread_target_id: Optional[str] = None
        try:
            if (
                tracking
                and active_lifecycle_match_id is not None
                and runtime_status not in {None, "idle"}
            ):
                _LOGGER.info(
                    "scm.enqueue_managed_turn.reusing_thread "
                    "thread_target_id=%s lifecycle_status=active normalized_status=%s "
                    "correlation_id=%s binding_id=%s repo_slug=%s pr_number=%s",
                    thread_target_id,
                    runtime_status,
                    *log_context,
                )
            if active_lifecycle_match_id is not None:
                merged_turn = _merge_into_existing_queued_scm_turn(
                    store,
                    thread_target_id=thread_target_id,
                    payload=queue_payload,
                )
                if merged_turn is not None:
                    return _managed_turn_result(
                        thread_target_id=thread_target_id,
                        client_request_id=(
                            _normalize_optional_text(merged_turn.get("client_turn_id"))
                            or client_request_id
                        ),
                        turn=merged_turn,
                        existed=True,
                        correlation_id=correlation_id,
                    )
            created = store.create_turn(
                thread_target_id,
                prompt=request.message_text,
                request_kind=request.kind,
                busy_policy="queue",
                model=request.model,
                reasoning=request.reasoning,
                client_turn_id=client_request_id,
                metadata=request.metadata,
                queue_payload=queue_payload,
                force_queue=bool(tracking),
            )
            _log_scm_enqueue_managed_turn_queued(
                store,
                thread_target_id=thread_target_id,
                created=created,
                tracking=tracking,
                log_context=log_context,
            )
        except ManagedThreadNotActiveError as exc:
            if not tracking:
                raise
            rebound_from_thread_target_id = thread_target_id
            _LOGGER.info(
                "scm.enqueue_managed_turn.rebinding_thread "
                "previous_thread_target_id=%s status=%s "
                "correlation_id=%s binding_id=%s repo_slug=%s pr_number=%s",
                thread_target_id,
                exc.status,
                *log_context,
            )
            thread_target_id, request = _repair_scm_thread_binding(
                hub_root,
                store,
                current_thread_target_id=thread_target_id,
                request=request,
                tracking=tracking,
                source_status=exc.status,
            )
            existing = store.get_turn_by_client_turn_id(
                thread_target_id, client_request_id
            )
            if existing is not None:
                _LOGGER.info(
                    "scm.enqueue_managed_turn.rebound_to_existing_turn "
                    "previous_thread_target_id=%s thread_target_id=%s "
                    "correlation_id=%s binding_id=%s repo_slug=%s pr_number=%s",
                    rebound_from_thread_target_id,
                    thread_target_id,
                    *log_context,
                )
                result = _managed_turn_result(
                    thread_target_id=thread_target_id,
                    client_request_id=client_request_id,
                    turn=existing,
                    existed=True,
                    correlation_id=correlation_id,
                )
                result["rebound_from_thread_target_id"] = rebound_from_thread_target_id
                return result
            queue_payload = {
                "request": request.to_dict(),
                "client_request_id": client_request_id,
                "sandbox_policy": sandbox_policy,
            }
            created = store.create_turn(
                thread_target_id,
                prompt=request.message_text,
                request_kind=request.kind,
                busy_policy="queue",
                model=request.model,
                reasoning=request.reasoning,
                client_turn_id=client_request_id,
                metadata=request.metadata,
                queue_payload=queue_payload,
                force_queue=bool(tracking),
            )
            _log_scm_enqueue_managed_turn_queued(
                store,
                thread_target_id=thread_target_id,
                created=created,
                tracking=tracking,
                log_context=log_context,
            )
            _LOGGER.info(
                "scm.enqueue_managed_turn.rebound_thread "
                "previous_thread_target_id=%s thread_target_id=%s "
                "correlation_id=%s binding_id=%s repo_slug=%s pr_number=%s",
                rebound_from_thread_target_id,
                thread_target_id,
                *log_context,
            )
        result = _managed_turn_result(
            thread_target_id=thread_target_id,
            client_request_id=client_request_id,
            turn=created,
            existed=False,
            correlation_id=correlation_id,
        )
        if rebound_from_thread_target_id is not None:
            result["rebound_from_thread_target_id"] = rebound_from_thread_target_id
        return result

    return executor


def _resolve_thread_context(
    store: PmaThreadStore,
    *,
    payload: dict[str, Any],
) -> tuple[Optional[str], Optional[Path]]:
    repo_id = _normalize_optional_text(payload.get("repo_id"))
    workspace_root_raw = _normalize_optional_text(payload.get("workspace_root"))
    workspace_root = Path(workspace_root_raw) if workspace_root_raw else None
    thread_target_id = _normalize_optional_text(payload.get("thread_target_id"))
    if thread_target_id is None:
        return repo_id, workspace_root
    thread = store.get_thread(thread_target_id)
    if thread is None:
        raise TerminalPublishError(
            f"Unknown managed thread '{thread_target_id}' for notify_chat"
        )
    resolved_repo_id = repo_id or _normalize_optional_text(thread.get("repo_id"))
    if workspace_root is None:
        thread_workspace = _normalize_optional_text(thread.get("workspace_root"))
        workspace_root = Path(thread_workspace) if thread_workspace else None
    return resolved_repo_id, workspace_root


def build_notify_chat_executor(
    *,
    hub_root: Path,
    run_coroutine: Optional[Callable[[Coroutine[Any, Any, Any]], Any]] = None,
    thread_store: Optional[PmaThreadStore] = None,
    journal_store: Optional[PublishJournalStore] = None,
) -> PublishActionExecutor:
    store = thread_store or PmaThreadStore(hub_root)
    journal = journal_store or PublishJournalStore(hub_root)
    coroutine_runner = run_coroutine or _run_coroutine_sync

    def executor(operation: PublishOperation) -> dict[str, Any]:
        payload = dict(_normalize_mapping(operation.payload))
        dependency_for_delivery = _normalize_mapping(
            payload.get("managed_turn_dependency")
        )
        if (
            _normalize_optional_text(dependency_for_delivery.get("dependency_kind"))
            == "enqueue_managed_turn_started"
        ):
            dep_operation_id = _normalize_optional_text(
                dependency_for_delivery.get("operation_id")
            )
            if dep_operation_id:
                enqueue_operation = journal.get_operation(dep_operation_id)
                if enqueue_operation is not None:
                    enqueue_response = _normalize_mapping(enqueue_operation.response)
                    resolved_thread_id = _normalize_optional_text(
                        enqueue_response.get("thread_target_id")
                    )
                    if resolved_thread_id is not None:
                        payload["thread_target_id"] = resolved_thread_id
        raw_message, confirmed_managed_turn = _resolve_notify_message(
            operation=operation,
            payload=payload,
            journal=journal,
            thread_store=store,
        )
        message = _normalize_optional_text(raw_message)
        if message is None:
            raise TerminalPublishError("Publish payload is missing notify_chat message")

        delivery = (
            _normalize_optional_text(
                payload.get("delivery")
                or payload.get("target")
                or payload.get("delivery_target")
            )
            or "auto"
        )
        correlation_id = correlation_id_for_operation(operation)
        delivery_correlation_id = correlation_id or _operation_digest(
            operation,
            prefix="publish-chat",
        )
        repo_id, workspace_root = _resolve_thread_context(store, payload=payload)
        # Do not enqueue bound live-progress when notify is muted (none) or when only
        # primary_pma is selected (invalid/missing repo_id would still have skipped chat).
        should_seed_bound_progress = delivery not in {"none", "primary_pma"}
        progress_start = _maybe_start_bound_live_progress_for_notify(
            hub_root=hub_root,
            thread_store=store,
            run_coroutine=coroutine_runner,
            workspace_root=workspace_root,
            repo_id=repo_id,
            confirmed=(confirmed_managed_turn if should_seed_bound_progress else None),
        )

        if delivery == "none":
            outcome = {"route": "none", "targets": 0, "published": 0}
        elif delivery == "primary_pma":
            if repo_id is None:
                raise TerminalPublishError(
                    "notify_chat primary_pma delivery requires repo_id"
                )
            outcome = coroutine_runner(
                notify_primary_pma_chat_for_repo(
                    hub_root=hub_root,
                    repo_id=repo_id,
                    message=message,
                    correlation_id=delivery_correlation_id,
                )
            )
        elif delivery == "bound":
            if workspace_root is None:
                raise TerminalPublishError(
                    "notify_chat bound delivery requires workspace_root or thread_target_id"
                )
            outcome = coroutine_runner(
                notify_preferred_bound_chat_for_workspace(
                    hub_root=hub_root,
                    workspace_root=workspace_root,
                    repo_id=repo_id,
                    message=message,
                    correlation_id=delivery_correlation_id,
                )
            )
        else:
            outcome = coroutine_runner(
                deliver_pma_notification(
                    hub_root=hub_root,
                    workspace_root=workspace_root,
                    repo_id=repo_id,
                    message=message,
                    correlation_id=delivery_correlation_id,
                    delivery=delivery,
                    source_kind="publish_operation",
                )
            )

        targets = _coerce_int((outcome or {}).get("targets", 0))
        published = _coerce_int((outcome or {}).get("published", 0))
        result: dict[str, Any] = {
            "delivery": delivery,
            "repo_id": repo_id,
            "targets": targets,
            "published": published,
        }
        route = _normalize_optional_text((outcome or {}).get("route"))
        if route is not None:
            result["route"] = route
        if correlation_id is not None:
            result["correlation_id"] = correlation_id
        if progress_start is not None:
            result["progress_start"] = progress_start
        return result

    return executor


__all__ = [
    "build_enqueue_managed_turn_executor",
    "build_notify_chat_executor",
]
