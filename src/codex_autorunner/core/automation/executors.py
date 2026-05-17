from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

from ..managed_thread_store import ManagedThreadStore
from ..publish_executor import PublishExecutorRegistry, drain_pending_publish_operations
from ..publish_journal import PublishJournalStore
from ..text_utils import _normalize_text
from .engine import render_template
from .models import (
    APPROVAL_AUTO_DECLINE,
    APPROVAL_INHERIT_PROFILE,
    APPROVAL_NEVER_REQUIRE_APPROVAL,
    APPROVAL_PAUSE_AND_REQUEST_USER,
    EXECUTOR_GITHUB_COMMENT,
    EXECUTOR_GITHUB_REACTION,
    EXECUTOR_PUBLISH_CHAT_NOTIFICATION,
    EXECUTOR_PUBLISH_OPERATION,
    JOB_DEAD_LETTERED,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    AutomationJob,
)
from .store import AutomationStore
from .worker import AutomationExecutorResult

_PUBLISH_KIND_BY_AUTOMATION_EXECUTOR = {
    EXECUTOR_PUBLISH_CHAT_NOTIFICATION: "notify_chat",
    EXECUTOR_GITHUB_REACTION: "react_pr_review_comment",
    EXECUTOR_GITHUB_COMMENT: "post_pr_comment",
}


class ManagedThreadTurnAutomationExecutor:
    def __init__(
        self,
        *,
        hub_root: Path,
        automation_store: Optional[AutomationStore] = None,
        thread_store: Optional[ManagedThreadStore] = None,
        unattended: bool = True,
    ) -> None:
        self._store = automation_store
        self._thread_store = thread_store or ManagedThreadStore(hub_root)
        self._unattended = unattended

    def execute(self, job: AutomationJob) -> AutomationExecutorResult:
        approval = str(
            job.policy.get("approval_mode") or APPROVAL_PAUSE_AND_REQUEST_USER
        ).strip()
        if self._unattended and approval == APPROVAL_PAUSE_AND_REQUEST_USER:
            return AutomationExecutorResult(
                status=JOB_PAUSED,
                summary="managed thread automation requires user approval",
                data={"approval_mode": approval},
            )
        if self._unattended and approval == APPROVAL_AUTO_DECLINE:
            return AutomationExecutorResult(
                status=JOB_FAILED,
                summary="managed thread automation auto-declined by approval policy",
                data={"approval_mode": approval},
            )

        request = self._request_config(job)
        thread_id = _require_text(
            request.get("thread_target_id")
            or request.get("managed_thread_id")
            or job.target.get("thread_target_id")
            or job.target.get("managed_thread_id"),
            "thread_target_id",
        )
        prompt = _require_text(
            request.get("message_text") or request.get("prompt") or request.get("body"),
            "message_text",
        )
        event = self._store.get_event(job.event_id) if self._store is not None else None
        prompt = str(
            render_template(
                prompt,
                {
                    "event": event.to_dict() if event is not None else {},
                    "target": job.target,
                    "job": job.to_dict(),
                    "metadata": job.payload.get("metadata", {}),
                    "repo": job.target,
                    "pr": _nested(job.payload, "event", "payload", "pr") or {},
                },
            )
        )
        client_turn_id = _normalize_text(
            request.get("client_turn_id")
            or request.get("client_request_id")
            or job.executor.get("client_turn_id")
            or job.executor.get("client_request_id")
        ) or _stable_client_turn_id(job)
        existing = self._thread_store.get_turn_by_client_turn_id(
            thread_id, client_turn_id
        )
        if existing is not None:
            return _managed_turn_result(
                thread_id=thread_id,
                turn=existing,
                client_turn_id=client_turn_id,
                deduped=True,
            )

        metadata = request.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata = {
            **metadata,
            "automation": {
                "job_id": job.job_id,
                "rule_id": job.rule_id,
                "event_id": job.event_id,
                "approval_mode": approval,
            },
        }
        if approval == APPROVAL_NEVER_REQUIRE_APPROVAL:
            metadata["automation"]["approval_override"] = "never_require_approval"
        elif approval == APPROVAL_INHERIT_PROFILE:
            metadata["automation"]["approval_override"] = "inherit_profile"

        queue_payload = {
            "request": {
                "target_id": thread_id,
                "target_kind": "thread",
                "message_text": prompt,
                "kind": request.get("kind") or request.get("request_kind") or "message",
                "busy_policy": request.get("busy_policy") or "queue",
                "agent_profile": request.get("agent_profile") or request.get("profile"),
                "model": request.get("model"),
                "reasoning": request.get("reasoning"),
                "approval_mode": (
                    request.get("approval_mode")
                    if approval == APPROVAL_INHERIT_PROFILE
                    else None
                ),
                "input_items": request.get("input_items"),
                "context_profile": request.get("context_profile"),
                "metadata": metadata,
            },
            "client_request_id": client_turn_id,
            "sandbox_policy": request.get("sandbox_policy"),
            "stream": request.get("stream"),
        }
        turn = self._thread_store.create_turn(
            thread_id,
            prompt=prompt,
            request_kind=str(
                request.get("kind") or request.get("request_kind") or "message"
            ),
            busy_policy=str(request.get("busy_policy") or "queue"),
            model=_normalize_text(request.get("model")),
            reasoning=_normalize_text(request.get("reasoning")),
            client_turn_id=client_turn_id,
            metadata=metadata,
            queue_payload=queue_payload,
            force_queue=bool(request.get("force_queue")),
        )
        return _managed_turn_result(
            thread_id=thread_id,
            turn=turn,
            client_turn_id=client_turn_id,
            deduped=False,
        )

    def _request_config(self, job: AutomationJob) -> dict[str, Any]:
        request = job.executor.get("request")
        if isinstance(request, dict):
            return {**job.executor, **request}
        return dict(job.executor)


class PublishOperationAutomationExecutor:
    def __init__(
        self,
        *,
        hub_root: Path,
        executor_registry: PublishExecutorRegistry,
        journal_store: Optional[PublishJournalStore] = None,
    ) -> None:
        self._journal = journal_store or PublishJournalStore(hub_root)
        self._executor_registry = executor_registry

    def execute(self, job: AutomationJob) -> AutomationExecutorResult:
        operation_kind = _publish_operation_kind(job)
        payload = _publish_payload(job)
        operation, deduped = self._journal.create_operation(
            operation_key=_publish_operation_key(job, operation_kind, payload),
            operation_kind=operation_kind,
            payload=payload,
        )
        if operation.state not in {"succeeded", "failed"}:
            processed = drain_pending_publish_operations(
                self._journal,
                executor_registry=self._executor_registry,
                limit=1,
            )
            operation = next(
                (
                    item
                    for item in processed
                    if item.operation_id == operation.operation_id
                ),
                self._journal.get_operation(operation.operation_id) or operation,
            )
        refs = {"publish_operation_id": operation.operation_id}
        data = {
            "operation": operation.to_dict(),
            "deduped": deduped,
        }
        if operation.state == "succeeded":
            return AutomationExecutorResult(
                status=JOB_SUCCEEDED,
                summary=f"publish operation succeeded: {operation_kind}",
                data=data,
                execution_refs=refs,
            )
        if operation.state == "pending":
            return AutomationExecutorResult(
                status=JOB_FAILED,
                summary=operation.last_error_text
                or "publish operation scheduled retry",
                data=data,
                execution_refs=refs,
            )
        return AutomationExecutorResult(
            status=JOB_DEAD_LETTERED,
            summary=operation.last_error_text or f"publish operation {operation.state}",
            data=data,
            execution_refs=refs,
        )


def _managed_turn_result(
    *,
    thread_id: str,
    turn: dict[str, Any],
    client_turn_id: str,
    deduped: bool,
) -> AutomationExecutorResult:
    turn_id = _require_text(turn.get("managed_turn_id"), "managed_turn_id")
    return AutomationExecutorResult(
        status=JOB_RUNNING,
        summary=f"managed thread turn {turn.get('status') or 'created'}",
        data={
            "execution_phase": (
                "waiting" if turn.get("status") == "queued" else "running"
            ),
            "thread_target_id": thread_id,
            "managed_turn_id": turn_id,
            "client_turn_id": client_turn_id,
            "deduped": deduped,
            "turn": turn,
        },
        execution_refs={
            "managed_thread_target_id": thread_id,
            "managed_thread_execution_id": turn_id,
        },
    )


def _publish_operation_kind(job: AutomationJob) -> str:
    kind = str(job.executor.get("kind") or "").strip()
    if kind == EXECUTOR_PUBLISH_OPERATION:
        return _require_text(job.executor.get("operation_kind"), "operation_kind")
    mapped = _PUBLISH_KIND_BY_AUTOMATION_EXECUTOR.get(kind)
    if mapped is None:
        return _require_text(job.executor.get("operation_kind"), "operation_kind")
    return mapped


def _publish_payload(job: AutomationJob) -> dict[str, Any]:
    payload = job.executor.get("payload")
    if isinstance(payload, dict):
        return dict(payload)
    payload = {key: value for key, value in job.executor.items() if key != "kind"}
    payload.pop("operation_kind", None)
    payload.pop("operation_key", None)
    return payload


def _publish_operation_key(
    job: AutomationJob, operation_kind: str, payload: dict[str, Any]
) -> str:
    key = _normalize_text(job.executor.get("operation_key"))
    if key is not None:
        return key
    digest = hashlib.sha256(
        repr((job.rule_id, job.event_id, operation_kind, payload)).encode("utf-8")
    ).hexdigest()[:32]
    return f"automation:{job.job_id}:{digest}"


def _stable_client_turn_id(job: AutomationJob) -> str:
    digest = hashlib.sha256(
        f"{job.rule_id}:{job.event_id}:{job.job_id}".encode()
    ).hexdigest()
    return f"automation-turn:{digest[:24]}"


def _require_text(value: Any, field_name: str) -> str:
    text = _normalize_text(value)
    if text is None:
        raise ValueError(f"{field_name} is required")
    return text


def _nested(value: Any, *path: str) -> Any:
    current = value
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


__all__ = [
    "ManagedThreadTurnAutomationExecutor",
    "PublishOperationAutomationExecutor",
]
