from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Optional, cast

from ..domain.refs import AgentRef, ScopeRef
from ..managed_thread_store import ManagedThreadStore
from ..orchestration.turn_execution_contract import (
    TurnExecutionContractError,
    TurnExecutionOrigin,
    TurnExecutionRequest,
    TurnExecutionRequestKind,
)
from ..pma_automation_types import DEFAULT_PMA_LANE_ID
from ..pma_queue import PmaQueue
from ..pma_reactive import PmaReactiveStore
from ..publish_executor import PublishExecutorRegistry, drain_pending_publish_operations
from ..publish_journal import PublishJournalStore
from ..text_utils import _normalize_text
from .engine import render_template
from .models import (
    APPROVAL_AUTO_DECLINE,
    APPROVAL_INHERIT_PROFILE,
    APPROVAL_NEVER_REQUIRE_APPROVAL,
    APPROVAL_PAUSE_AND_REQUEST_USER,
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    AUTOMATION_CHILD_KIND_PMA_OPERATOR,
    AUTOMATION_CHILD_KIND_PUBLISH_OPERATION,
    EXECUTOR_GITHUB_COMMENT,
    EXECUTOR_GITHUB_REACTION,
    EXECUTOR_PMA_OPERATOR_TURN,
    EXECUTOR_PUBLISH_CHAT_NOTIFICATION,
    EXECUTOR_PUBLISH_OPERATION,
    JOB_DEAD_LETTERED,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_RUNNING,
    JOB_SKIPPED,
    JOB_SUCCEEDED,
    AutomationChildExecutionEdge,
    AutomationJob,
    AutomationRuntimeContract,
)
from .store import AutomationStore
from .worker import AutomationExecutorResult

_PUBLISH_KIND_BY_AUTOMATION_EXECUTOR = {
    EXECUTOR_PUBLISH_CHAT_NOTIFICATION: "notify_chat",
    EXECUTOR_GITHUB_REACTION: "react_pr_review_comment",
    EXECUTOR_GITHUB_COMMENT: "post_pr_comment",
}

_TURN_EXECUTION_REQUEST_KINDS = frozenset(
    {"message", "review", "automation", "publish", "recovery", "lifecycle"}
)


def _opencode_model_payload(model: Optional[str]) -> dict[str, str]:
    if model is None or "/" not in model:
        return {}
    provider_id, model_id = (part.strip() for part in model.split("/", 1))
    if not provider_id or not model_id:
        return {}
    return {"providerID": provider_id, "modelID": model_id}


def _turn_request_kind(value: Any) -> TurnExecutionRequestKind:
    normalized = _normalize_text(value)
    if normalized in _TURN_EXECUTION_REQUEST_KINDS:
        return cast(TurnExecutionRequestKind, normalized)
    return "automation"


class ManagedThreadTurnAutomationExecutor:
    def __init__(
        self,
        *,
        hub_root: Path,
        automation_store: Optional[AutomationStore] = None,
        thread_store: Optional[ManagedThreadStore] = None,
        safety_checker_fn: Optional[Callable[[], Any]] = None,
        queue_worker_starter_fn: Optional[Callable[[str], None]] = None,
        queue_worker_available_fn: Optional[Callable[[], bool]] = None,
        unattended: bool = True,
        child_kind: Optional[str] = None,
        strict_runtime_contract: bool = False,
    ) -> None:
        self._hub_root = hub_root
        self._store = automation_store
        self._thread_store = thread_store or ManagedThreadStore(hub_root)
        self._safety_checker_fn = safety_checker_fn
        self._queue_worker_starter_fn = queue_worker_starter_fn
        self._queue_worker_available_fn = queue_worker_available_fn
        self._unattended = unattended
        self._child_kind = child_kind
        self._strict_runtime_contract = strict_runtime_contract

    def execute(self, job: AutomationJob) -> AutomationExecutorResult:
        if job.policy.get("requires_pma_safety"):
            safety_checker = (
                self._safety_checker_fn() if self._safety_checker_fn else None
            )
            if safety_checker is not None:
                safety_check = safety_checker.check_reactive_turn()
                if not safety_check.allowed:
                    return AutomationExecutorResult(
                        status=JOB_SKIPPED,
                        summary=safety_check.reason or "reactive_blocked",
                    )
            debounce_seconds = int(job.policy.get("reactive_debounce_seconds") or 0)
            debounce_key = str(job.policy.get("reactive_debounce_key") or "").strip()
            if debounce_seconds > 0 and debounce_key:
                if not PmaReactiveStore(self._hub_root).check_and_update(
                    debounce_key, debounce_seconds
                ):
                    return AutomationExecutorResult(
                        status=JOB_SKIPPED,
                        summary="reactive_debounced",
                    )
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

        try:
            request = self._request_config(job)
        except ValueError as exc:
            return AutomationExecutorResult(status=JOB_FAILED, summary=str(exc))
        if self._child_kind is not None and self._store is None:
            return AutomationExecutorResult(
                status=JOB_FAILED,
                summary="agent task automation requires automation store for child edge",
            )
        thread_id = self._resolve_thread_id(job, request)
        prompt = _require_text(
            request.get("message_text")
            or request.get("message")
            or request.get("prompt")
            or request.get("body"),
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
        existing_without_edge = self._thread_store.get_turn_by_client_turn_id(
            thread_id, client_turn_id
        )
        if existing_without_edge is not None and self._child_kind is None:
            return _managed_turn_result(
                thread_id=thread_id,
                turn=existing_without_edge,
                client_turn_id=client_turn_id,
                deduped=True,
            )
        if not self._queue_worker_can_start():
            return AutomationExecutorResult(
                status=JOB_DEAD_LETTERED,
                summary=(
                    "managed thread automation requires a registered queue worker "
                    "starter"
                ),
            )

        thread = self._thread_store.get_thread(thread_id) or {}
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

        try:
            agent = _require_text(
                request.get("agent")
                or thread.get("agent_id")
                or thread.get("agent")
                or (None if self._strict_runtime_contract else "codex"),
                "agent",
            )
            self._validate_requested_thread_runtime(
                request=request, thread=thread, agent=agent
            )
        except ValueError as exc:
            return AutomationExecutorResult(status=JOB_FAILED, summary=str(exc))
        model = _normalize_text(request.get("model"))
        approval_mode = (
            _normalize_text(request.get("approval_mode"))
            if approval == APPROVAL_INHERIT_PROFILE
            else None
        )
        try:
            turn_request = TurnExecutionRequest(
                request_id=client_turn_id,
                target_id=thread_id,
                target_kind="thread",
                workspace_root=_normalize_text(thread.get("workspace_root")),
                request_kind=_turn_request_kind(
                    request.get("request_kind") or request.get("kind")
                ),
                busy_policy=request.get("busy_policy") or "queue",
                prompt_text=prompt,
                input_items=(
                    tuple(
                        dict(item)
                        for item in request.get("input_items", ())
                        if isinstance(item, dict)
                    )
                    if isinstance(request.get("input_items"), (list, tuple))
                    else ()
                ),
                context_profile=request.get("context_profile"),
                agent=agent,
                profile=request.get("agent_profile") or request.get("profile"),
                model=model,
                model_payload=(
                    _opencode_model_payload(model) if agent == "opencode" else {}
                ),
                reasoning=_normalize_text(request.get("reasoning")),
                approval_policy=(
                    _normalize_text(request.get("approval_policy"))
                    or approval_mode
                    or approval
                ),
                approval_mode=approval_mode,
                sandbox_policy=request.get("sandbox_policy") or "dangerFullAccess",
                client_request_id=client_turn_id,
                idempotency_key=client_turn_id,
                correlation_id=_normalize_text(
                    request.get("correlation_id") or job.payload.get("correlation_id")
                )
                or job.job_id,
                origin=TurnExecutionOrigin(
                    kind="automation",
                    source_id=job.job_id,
                    automation_rule_id=job.rule_id,
                    metadata={"event_id": job.event_id},
                ),
                metadata=metadata,
            )
        except TurnExecutionContractError as exc:
            return AutomationExecutorResult(status=JOB_FAILED, summary=str(exc))

        existing = existing_without_edge
        if existing is not None:
            try:
                edge_id = self._record_child_edge(
                    job=job,
                    child_id=_require_text(
                        existing.get("managed_turn_id"), "managed_turn_id"
                    ),
                    turn_request=turn_request,
                    thread=thread,
                )
            except (RuntimeError, ValueError) as exc:
                return AutomationExecutorResult(status=JOB_FAILED, summary=str(exc))
            return _managed_turn_result(
                thread_id=thread_id,
                turn=existing,
                client_turn_id=client_turn_id,
                deduped=True,
                child_edge_id=edge_id,
            )

        turn = self._thread_store.create_turn(
            thread_id,
            prompt=prompt,
            request_kind=str(
                request.get("kind") or request.get("request_kind") or "message"
            ),
            busy_policy=str(request.get("busy_policy") or "queue"),
            model=model,
            reasoning=_normalize_text(request.get("reasoning")),
            client_turn_id=client_turn_id,
            metadata=metadata,
            turn_request=turn_request,
            force_queue=True,
        )
        try:
            edge_id = self._record_child_edge(
                job=job,
                child_id=_require_text(turn.get("managed_turn_id"), "managed_turn_id"),
                turn_request=turn_request,
                thread=thread,
            )
        except (RuntimeError, ValueError) as exc:
            return AutomationExecutorResult(status=JOB_FAILED, summary=str(exc))
        if edge_id is not None:
            store = self._store
            if store is None:
                return AutomationExecutorResult(
                    status=JOB_FAILED,
                    summary="agent task automation requires automation store for child edge",
                )
            persisted = store.get_child_execution_edge(edge_id)
            if persisted is None:
                return AutomationExecutorResult(
                    status=JOB_FAILED,
                    summary="agent task automation child edge was not durable",
                )
        self._request_queue_worker_start(thread_id)
        return _managed_turn_result(
            thread_id=thread_id,
            turn=turn,
            client_turn_id=client_turn_id,
            deduped=False,
            child_edge_id=edge_id,
        )

    def _record_child_edge(
        self,
        *,
        job: AutomationJob,
        child_id: str,
        turn_request: TurnExecutionRequest,
        thread: dict[str, Any],
    ) -> Optional[str]:
        if self._child_kind is None:
            return None
        if self._store is None:
            raise RuntimeError(
                "agent task automation requires automation store for child edge"
            )
        edge = self._store.upsert_child_execution_edge(
            AutomationChildExecutionEdge.create(
                parent_job_id=job.job_id,
                child_kind=self._child_kind,
                child_id=child_id,
                requested_runtime=_runtime_contract_from_request(
                    turn_request, thread=thread
                ),
                actual_runtime=None,
                authoritative_for_parent_completion=True,
            )
        )
        return edge.edge_id

    def _request_config(self, job: AutomationJob) -> dict[str, Any]:
        request = job.executor.get("request")
        if isinstance(request, dict):
            config = {**job.executor, **request}
        else:
            config = dict(job.executor)
        runtime = config.get("requested_runtime")
        if isinstance(runtime, dict):
            for runtime_key, request_key in (
                ("agent", "agent"),
                ("model", "model"),
                ("profile", "profile"),
                ("reasoning", "reasoning"),
                ("approval_policy", "approval_policy"),
                ("sandbox_policy", "sandbox_policy"),
            ):
                runtime_value = _normalize_text(runtime.get(runtime_key))
                if runtime_value is None:
                    continue
                existing = _normalize_text(config.get(request_key))
                alternate = (
                    _normalize_text(config.get("agent_profile"))
                    if request_key == "profile"
                    else None
                )
                if existing is not None and existing != runtime_value:
                    raise ValueError(
                        f"requested_runtime.{runtime_key} conflicts with executor.{request_key}"
                    )
                if alternate is not None and alternate != runtime_value:
                    raise ValueError(
                        "requested_runtime.profile conflicts with executor.agent_profile"
                    )
                config[request_key] = runtime_value
        if (
            self._strict_runtime_contract
            and _normalize_text(config.get("agent")) is None
        ):
            raise ValueError("agent_task_turn requires requested_runtime.agent")
        return config

    def _validate_requested_thread_runtime(
        self, *, request: dict[str, Any], thread: dict[str, Any], agent: str
    ) -> None:
        requested_agent = _normalize_text(request.get("agent"))
        actual_agent = _normalize_text(thread.get("agent") or thread.get("agent_id"))
        if self._strict_runtime_contract and requested_agent is None:
            raise ValueError("agent_task_turn requires requested_runtime.agent")
        if (
            self._strict_runtime_contract
            and requested_agent is not None
            and actual_agent is not None
            and actual_agent != requested_agent
        ):
            raise ValueError(
                f"requested agent {requested_agent!r} does not match thread agent {actual_agent!r}"
            )
        if (
            self._strict_runtime_contract
            and actual_agent is not None
            and actual_agent != agent
        ):
            raise ValueError(
                f"resolved agent {agent!r} does not match thread agent {actual_agent!r}"
            )
        requested_profile = _normalize_text(
            request.get("profile") or request.get("agent_profile")
        )
        raw_metadata = thread.get("metadata")
        metadata: dict[str, Any] = (
            cast(dict[str, Any], raw_metadata) if isinstance(raw_metadata, dict) else {}
        )
        actual_profile = _normalize_text(metadata.get("agent_profile"))
        if (
            self._strict_runtime_contract
            and requested_profile is not None
            and actual_profile is not None
            and actual_profile != requested_profile
        ):
            raise ValueError(
                f"requested profile {requested_profile!r} does not match thread profile {actual_profile!r}"
            )

    def _resolve_thread_id(self, job: AutomationJob, request: dict[str, Any]) -> str:
        explicit = _normalize_text(
            request.get("thread_target_id")
            or request.get("managed_thread_id")
            or request.get("thread_id")
            or job.target.get("thread_target_id")
            or job.target.get("managed_thread_id")
            or job.target.get("thread_id")
        )
        if explicit:
            return explicit
        return self._create_automation_thread(job, request)

    def _create_automation_thread(
        self, job: AutomationJob, request: dict[str, Any]
    ) -> str:
        agent_id = _normalize_text(request.get("agent")) or "codex"
        profile = _normalize_text(
            request.get("profile") or request.get("agent_profile")
        )
        repo_id = _normalize_text(
            job.target.get("repo_id")
            or job.target.get("base_repo_id")
            or job.payload.get("repo_id")
        )
        resource_kind = _normalize_text(job.target.get("resource_kind"))
        resource_id = _normalize_text(
            job.target.get("resource_id")
            or job.target.get("worktree_id")
            or job.target.get("run_id")
        )
        if resource_kind is None and resource_id is not None:
            resource_kind = "worktree" if job.target.get("worktree_id") else "run"
        scope = (
            ScopeRef(kind="repo", id=repo_id)
            if repo_id and resource_kind is None and resource_id is None
            else None
        )
        metadata = {
            "automation": {
                "job_id": job.job_id,
                "rule_id": job.rule_id,
                "event_id": job.event_id,
            },
            "automation_job_id": job.job_id,
            "automation_rule_id": job.rule_id,
        }
        created = self._thread_store.create_thread(
            AgentRef(agent_id=agent_id, profile=profile),
            self._hub_root,
            scope=scope,
            repo_id=repo_id if scope is None else None,
            resource_kind=resource_kind,
            resource_id=resource_id,
            name=_normalize_text(request.get("thread_name"))
            or f"Automation {job.rule_id}",
            metadata=metadata,
        )
        return _require_text(created.get("managed_thread_id"), "managed_thread_id")

    def _request_queue_worker_start(self, thread_id: str) -> None:
        starter = self._queue_worker_starter_fn
        if starter is None:
            return
        starter(thread_id)

    def _queue_worker_can_start(self) -> bool:
        if self._queue_worker_starter_fn is None:
            return False
        if self._queue_worker_available_fn is None:
            return True
        try:
            return bool(self._queue_worker_available_fn())
        except (RuntimeError, OSError, ValueError, TypeError):
            return False


class AgentTaskTurnAutomationExecutor(ManagedThreadTurnAutomationExecutor):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            **kwargs,
            child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
            strict_runtime_contract=True,
        )


class PmaOperatorTurnAutomationExecutor:
    def __init__(
        self,
        *,
        hub_root: Path,
        automation_store: AutomationStore,
        pma_queue: Optional[PmaQueue] = None,
        safety_checker_fn: Optional[Callable[[], Any]] = None,
        lane_worker_starter_fn: Optional[Callable[[str], Any]] = None,
        unattended: bool = True,
    ) -> None:
        self._hub_root = hub_root
        self._store = automation_store
        self._queue = pma_queue or PmaQueue(hub_root)
        self._safety_checker_fn = safety_checker_fn
        self._lane_worker_starter_fn = lane_worker_starter_fn
        self._unattended = unattended

    def execute(self, job: AutomationJob) -> AutomationExecutorResult:
        blocked = _check_reactive_safety(
            job,
            hub_root=self._hub_root,
            safety_checker_fn=self._safety_checker_fn,
        )
        if blocked is not None:
            return blocked
        approval = str(
            job.policy.get("approval_mode") or APPROVAL_PAUSE_AND_REQUEST_USER
        ).strip()
        if self._unattended and approval == APPROVAL_PAUSE_AND_REQUEST_USER:
            return AutomationExecutorResult(
                status=JOB_PAUSED,
                summary="PMA operator automation requires user approval",
                data={"approval_mode": approval},
            )
        if self._unattended and approval == APPROVAL_AUTO_DECLINE:
            return AutomationExecutorResult(
                status=JOB_FAILED,
                summary="PMA operator automation auto-declined by approval policy",
                data={"approval_mode": approval},
            )

        try:
            request = self._request_config(job)
            lane_id = _normalize_text(request.get("lane_id")) or DEFAULT_PMA_LANE_ID
            prompt = self._render_prompt(job, request)
            client_turn_id = _normalize_text(
                request.get("client_turn_id")
                or request.get("client_request_id")
                or job.executor.get("client_turn_id")
                or job.executor.get("client_request_id")
            ) or _stable_client_turn_id(job)
            coordinator_request = self._turn_request(
                job=job,
                request=request,
                lane_id=lane_id,
                prompt=prompt,
                client_turn_id=client_turn_id,
                approval=approval,
            )
        except (TurnExecutionContractError, ValueError) as exc:
            return AutomationExecutorResult(status=JOB_FAILED, summary=str(exc))

        idempotency_key = _normalize_text(
            request.get("idempotency_key")
        ) or _stable_pma_idempotency_key(
            job,
            lane_id=lane_id,
            client_turn_id=client_turn_id,
        )
        item, dupe_reason = self._queue.enqueue_sync(
            lane_id,
            idempotency_key,
            {"turn_request": coordinator_request.to_dict()},
        )
        try:
            edge = self._store.upsert_child_execution_edge(
                AutomationChildExecutionEdge.create(
                    parent_job_id=job.job_id,
                    child_kind=AUTOMATION_CHILD_KIND_PMA_OPERATOR,
                    child_id=item.item_id,
                    requested_runtime=_runtime_contract_from_request(
                        coordinator_request
                    ),
                    actual_runtime=None,
                    authoritative_for_parent_completion=bool(
                        request.get("coordinator_authoritative", True)
                    ),
                )
            )
        except (RuntimeError, ValueError) as exc:
            return AutomationExecutorResult(status=JOB_FAILED, summary=str(exc))
        if self._store.get_child_execution_edge(edge.edge_id) is None:
            return AutomationExecutorResult(
                status=JOB_FAILED,
                summary="PMA operator automation child edge was not durable",
            )
        worker_edge_ids = _record_declared_worker_child_edges(
            self._store, job=job, request=request
        )

        starter_result = self._request_lane_worker_start(lane_id)
        if starter_result is False:
            return AutomationExecutorResult(
                status=JOB_DEAD_LETTERED,
                summary="PMA operator automation lane worker starter failed",
            )
        refs = {
            "pma_lane_id": lane_id,
            "pma_queue_item_id": item.item_id,
            "automation_child_edge_id": edge.edge_id,
        }
        return AutomationExecutorResult(
            status=JOB_RUNNING,
            summary="PMA operator turn queued",
            data={
                "execution_phase": "waiting",
                "lane_id": lane_id,
                "pma_queue_item_id": item.item_id,
                "client_turn_id": client_turn_id,
                "deduped": bool(dupe_reason),
                "automation_child_edge_id": edge.edge_id,
                "worker_child_edge_ids": worker_edge_ids,
            },
            execution_refs=refs,
        )

    def _request_config(self, job: AutomationJob) -> dict[str, Any]:
        request = job.executor.get("request")
        config = (
            {**job.executor, **request}
            if isinstance(request, dict)
            else dict(job.executor)
        )
        runtime = config.get("requested_runtime")
        if isinstance(runtime, dict):
            for runtime_key, request_key in (
                ("agent", "agent"),
                ("model", "model"),
                ("profile", "profile"),
                ("reasoning", "reasoning"),
                ("approval_policy", "approval_policy"),
                ("sandbox_policy", "sandbox_policy"),
            ):
                runtime_value = _normalize_text(runtime.get(runtime_key))
                if runtime_value is None:
                    continue
                existing = _normalize_text(config.get(request_key))
                if existing is not None and existing != runtime_value:
                    raise ValueError(
                        f"requested_runtime.{runtime_key} conflicts with executor.{request_key}"
                    )
                config[request_key] = runtime_value
        return config

    def _render_prompt(self, job: AutomationJob, request: dict[str, Any]) -> str:
        prompt = _require_text(
            request.get("message_text")
            or request.get("message")
            or request.get("prompt")
            or request.get("body"),
            "message_text",
        )
        event = self._store.get_event(job.event_id)
        return str(
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

    def _turn_request(
        self,
        *,
        job: AutomationJob,
        request: dict[str, Any],
        lane_id: str,
        prompt: str,
        client_turn_id: str,
        approval: str,
    ) -> TurnExecutionRequest:
        agent = _require_text(request.get("agent") or "codex", "agent")
        model = _normalize_text(request.get("model"))
        approval_mode = (
            _normalize_text(request.get("approval_mode"))
            if approval == APPROVAL_INHERIT_PROFILE
            else None
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
                "executor_kind": EXECUTOR_PMA_OPERATOR_TURN,
            },
        }
        prompt = (
            f"{prompt}\n\n"
            "Automation child execution contract:\n"
            f"- parent_job_id: {job.job_id}\n"
            "- If you launch worker agent turns for this automation, attach them "
            "to the durable automation graph at launch time by passing "
            f"`--automation-parent-job-id {job.job_id}` to `car pma thread send`."
        )
        return TurnExecutionRequest(
            request_id=client_turn_id,
            target_id=lane_id,
            target_kind="thread",
            workspace_root=str(self._hub_root),
            request_kind=_turn_request_kind(
                request.get("request_kind") or "automation"
            ),
            busy_policy=request.get("busy_policy") or "queue",
            prompt_text=prompt,
            input_items=(
                tuple(
                    dict(item)
                    for item in request.get("input_items", ())
                    if isinstance(item, dict)
                )
                if isinstance(request.get("input_items"), (list, tuple))
                else ()
            ),
            context_profile=request.get("context_profile"),
            agent=agent,
            profile=request.get("agent_profile") or request.get("profile"),
            model=model,
            model_payload=_opencode_model_payload(model) if agent == "opencode" else {},
            reasoning=_normalize_text(request.get("reasoning")),
            approval_policy=(
                _normalize_text(request.get("approval_policy"))
                or approval_mode
                or approval
            ),
            approval_mode=approval_mode,
            sandbox_policy=request.get("sandbox_policy") or "dangerFullAccess",
            client_request_id=client_turn_id,
            idempotency_key=client_turn_id,
            correlation_id=_normalize_text(
                request.get("correlation_id") or job.payload.get("correlation_id")
            )
            or job.job_id,
            origin=TurnExecutionOrigin(
                kind="automation",
                source_id=job.job_id,
                automation_rule_id=job.rule_id,
                metadata={"event_id": job.event_id},
            ),
            metadata=metadata,
        )

    def _request_lane_worker_start(self, lane_id: str) -> Optional[bool]:
        starter = self._lane_worker_starter_fn
        if starter is None:
            return None
        try:
            result = starter(lane_id)
        except (RuntimeError, OSError, ValueError, TypeError):
            return False
        accepted = getattr(result, "accepted", None)
        if accepted is not None:
            return bool(accepted)
        return None


class PublishOperationAutomationExecutor:
    def __init__(
        self,
        *,
        hub_root: Path,
        executor_registry: PublishExecutorRegistry,
        automation_store: Optional[AutomationStore] = None,
        journal_store: Optional[PublishJournalStore] = None,
    ) -> None:
        self._journal = journal_store or PublishJournalStore(hub_root)
        self._executor_registry = executor_registry
        self._store = automation_store

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
        edge_id = self._record_child_edge(
            job,
            operation_id=operation.operation_id,
            operation_state=operation.state,
        )
        if edge_id is not None:
            refs["automation_child_edge_id"] = edge_id
        data = {
            "operation": operation.to_dict(),
            "deduped": deduped,
            "automation_child_edge_id": edge_id,
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

    def _record_child_edge(
        self, job: AutomationJob, *, operation_id: str, operation_state: str
    ) -> Optional[str]:
        if self._store is None:
            return None
        runtime = AutomationRuntimeContract(
            input_ref={"kind": "automation_job", "job_id": job.job_id},
            workspace_scope={"kind": "publish_operation"},
        )
        edge = self._store.upsert_child_execution_edge(
            AutomationChildExecutionEdge.create(
                parent_job_id=job.job_id,
                child_kind=AUTOMATION_CHILD_KIND_PUBLISH_OPERATION,
                child_id=operation_id,
                requested_runtime=runtime,
                actual_runtime=runtime,
                authoritative_for_parent_completion=True,
                terminal_state=_publish_child_terminal_state(operation_state),
            )
        )
        return edge.edge_id


def _managed_turn_result(
    *,
    thread_id: str,
    turn: dict[str, Any],
    client_turn_id: str,
    deduped: bool,
    child_edge_id: Optional[str] = None,
) -> AutomationExecutorResult:
    turn_id = _require_text(turn.get("managed_turn_id"), "managed_turn_id")
    refs = {
        "managed_thread_target_id": thread_id,
        "managed_thread_execution_id": turn_id,
    }
    if child_edge_id is not None:
        refs["automation_child_edge_id"] = child_edge_id
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
            "automation_child_edge_id": child_edge_id,
            "turn": turn,
        },
        execution_refs=refs,
    )


def _runtime_contract_from_request(
    request: TurnExecutionRequest, *, thread: Optional[dict[str, Any]] = None
) -> AutomationRuntimeContract:
    thread = thread or {}
    raw_metadata = thread.get("metadata")
    metadata: dict[str, Any] = (
        cast(dict[str, Any], raw_metadata) if isinstance(raw_metadata, dict) else {}
    )
    raw_backend_binding = thread.get("backend_binding")
    backend_binding: dict[str, Any] = (
        cast(dict[str, Any], raw_backend_binding)
        if isinstance(raw_backend_binding, dict)
        else {}
    )
    return AutomationRuntimeContract(
        agent=request.agent,
        model=request.model,
        profile=request.profile or _normalize_text(metadata.get("agent_profile")),
        reasoning=request.reasoning,
        approval_policy=request.approval_policy,
        sandbox_policy=(
            request.sandbox_policy
            if isinstance(request.sandbox_policy, str)
            else str(request.sandbox_policy)
        ),
        prompt_ref={
            "kind": "turn_execution_request",
            "request_id": request.request_id,
        },
        input_ref={
            "kind": "automation_job",
            "job_id": request.origin.source_id,
            "event_id": request.origin.metadata.get("event_id"),
        },
        workspace_scope={
            "target_kind": request.target_kind,
            "target_id": request.target_id,
            "workspace_root": request.workspace_root,
        },
        backend_runtime_id=_normalize_text(
            backend_binding.get("backend_runtime_instance_id")
        ),
        provider_payload=dict(request.model_payload) or None,
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


def _publish_child_terminal_state(operation_state: str) -> Optional[str]:
    normalized = str(operation_state or "").strip().lower()
    if normalized == "succeeded":
        return "succeeded"
    if normalized == "failed":
        return "failed"
    return None


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


def _stable_pma_idempotency_key(
    job: AutomationJob, *, lane_id: str, client_turn_id: str
) -> str:
    digest = hashlib.sha256(
        f"{job.rule_id}:{job.event_id}:{job.job_id}:{lane_id}:{client_turn_id}".encode()
    ).hexdigest()
    return f"automation-pma:{digest[:32]}"


def _check_reactive_safety(
    job: AutomationJob,
    *,
    hub_root: Path,
    safety_checker_fn: Optional[Callable[[], Any]],
) -> Optional[AutomationExecutorResult]:
    if not job.policy.get("requires_pma_safety"):
        return None
    safety_checker = safety_checker_fn() if safety_checker_fn else None
    if safety_checker is not None:
        safety_check = safety_checker.check_reactive_turn()
        if not safety_check.allowed:
            return AutomationExecutorResult(
                status=JOB_SKIPPED,
                summary=safety_check.reason or "reactive_blocked",
            )
    debounce_seconds = int(job.policy.get("reactive_debounce_seconds") or 0)
    debounce_key = str(job.policy.get("reactive_debounce_key") or "").strip()
    if debounce_seconds > 0 and debounce_key:
        if not PmaReactiveStore(hub_root).check_and_update(
            debounce_key, debounce_seconds
        ):
            return AutomationExecutorResult(
                status=JOB_SKIPPED,
                summary="reactive_debounced",
            )
    return None


def _record_declared_worker_child_edges(
    store: AutomationStore, *, job: AutomationJob, request: dict[str, Any]
) -> list[str]:
    worker_children = request.get("worker_children")
    if worker_children is None:
        worker_child = request.get("worker_child")
        worker_children = [worker_child] if isinstance(worker_child, dict) else []
    if not isinstance(worker_children, list):
        return []
    edge_ids: list[str] = []
    for raw_child in worker_children:
        if not isinstance(raw_child, dict):
            continue
        child_id = _normalize_text(
            raw_child.get("child_id")
            or raw_child.get("managed_turn_id")
            or raw_child.get("execution_id")
        )
        requested_runtime = raw_child.get("requested_runtime")
        if child_id is None or not isinstance(requested_runtime, dict):
            continue
        actual_runtime = raw_child.get("actual_runtime")
        edge = store.upsert_child_execution_edge(
            AutomationChildExecutionEdge.create(
                parent_job_id=job.job_id,
                child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
                child_id=child_id,
                requested_runtime=requested_runtime,
                actual_runtime=(
                    actual_runtime if isinstance(actual_runtime, dict) else None
                ),
                authoritative_for_parent_completion=bool(
                    raw_child.get("authoritative_for_parent_completion", True)
                ),
            )
        )
        edge_ids.append(edge.edge_id)
    return edge_ids


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
    "AgentTaskTurnAutomationExecutor",
    "ManagedThreadTurnAutomationExecutor",
    "PmaOperatorTurnAutomationExecutor",
    "PublishOperationAutomationExecutor",
]
