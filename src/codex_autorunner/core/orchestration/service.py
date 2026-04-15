from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, cast

from ..car_context import CarContextProfile, normalize_car_context_profile
from ..logging_utils import log_event
from ..pma_automation_store import PmaAutomationStore
from ..pma_thread_store import PmaThreadStore
from ..text_utils import _truncate_text
from ..time_utils import now_iso
from .bindings import ActiveWorkSummary, OrchestrationBindingStore
from .catalog import MappingAgentDefinitionCatalog, RuntimeAgentDescriptor
from .events import OrchestrationEvent
from .flows import (
    PausedFlowTarget,
    TicketFlowTargetWrapper,
    build_ticket_flow_target_wrapper,
)
from .interfaces import (
    AgentDefinitionCatalog,
    FreshConversationRequiredError,
    OrchestrationFlowService,
    OrchestrationThreadService,
    RuntimeThreadHarness,
    ThreadExecutionStore,
    WorkspaceRuntimeAcquisition,
)
from .models import (
    AgentDefinition,
    ExecutionRecord,
    FlowRunTarget,
    FlowTarget,
    MessageRequest,
    MessageRequestKind,
    QueuedExecutionRequest,
    ThreadStopOutcome,
    ThreadTarget,
)
from .runtime_bindings import RuntimeThreadBinding, get_runtime_thread_binding
from .threads import SurfaceThreadMessageRequest, ThreadControlRequest
from .transcript_mirror import TranscriptMirrorStore

MessagePreviewLimit = 120
LOST_BACKEND_THREAD_ERROR = "Backend thread lost after restart"
MISSING_BACKEND_THREAD_ERROR = "Backend thread missing from orchestration state"
CLAIMED_EXECUTION_START_CANCELLED_ERROR = (
    "Runtime thread start cancelled before completion"
)
_MISSING_THREAD_MARKERS = (
    "missing thread",
    "thread not found",
    "no rollout found for thread id",
    "unknown hermes turn",
    "no active hermes turn tracked",
)
_RECOVERABLE_BACKEND_MARKERS = _MISSING_THREAD_MARKERS + ("event loop is closed",)
_REHYDRATION_TRANSCRIPT_LIMIT = 3
_REHYDRATION_TEXT_LIMIT = 4_000
_FRESH_BACKEND_SESSION_NOTICE = (
    "Notice: the previous live session was unavailable, so I started a new " "session."
)
_FRESH_BACKEND_SESSION_REHYDRATED_NOTICE = (
    "Notice: the previous live session was unavailable, so I started a new "
    "session and recovered context from durable history."
)
logger = logging.getLogger(__name__)

HarnessFactory = Callable[..., RuntimeThreadHarness]


class BusyInterruptFailedError(RuntimeError):
    """Busy-policy interrupt failed while the original execution remained active."""

    def __init__(
        self,
        *,
        thread_target_id: str,
        active_execution_id: Optional[str],
        backend_thread_id: Optional[str],
        detail: str = "Interrupt attempt failed; original turn is still running",
    ) -> None:
        super().__init__(detail)
        self.thread_target_id = thread_target_id
        self.active_execution_id = active_execution_id
        self.backend_thread_id = backend_thread_id
        self.detail = detail


def _truncate_rehydration_text(value: str, limit: int = _REHYDRATION_TEXT_LIMIT) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    if limit <= 3:
        return stripped[:limit]
    return stripped[: limit - 3] + "..."


def _thread_target_from_store_row(record: Mapping[str, Any]) -> ThreadTarget:
    return ThreadTarget.from_mapping(record)


def _thread_target_from_store_row_with_runtime_binding(
    store: PmaThreadStore, record: Mapping[str, Any]
) -> ThreadTarget:
    thread_record = dict(record)
    managed_thread_id = str(thread_record.get("managed_thread_id") or "").strip()
    runtime_binding = (
        store.get_thread_runtime_binding(managed_thread_id)
        if managed_thread_id
        else None
    )
    if runtime_binding is not None:
        thread_record["backend_thread_id"] = runtime_binding.backend_thread_id
        thread_record["backend_runtime_instance_id"] = (
            runtime_binding.backend_runtime_instance_id
        )
    return ThreadTarget.from_mapping(thread_record)


def _normalize_request_kind(value: Any) -> MessageRequestKind:
    normalized = str(value or "").strip().lower()
    if normalized == "review":
        return "review"
    return "message"


def _is_missing_thread_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _MISSING_THREAD_MARKERS)


def _is_recoverable_backend_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _RECOVERABLE_BACKEND_MARKERS)


async def _resolve_harness_runtime_instance_id(
    harness: RuntimeThreadHarness, workspace_root: Path
) -> Optional[str]:
    resolver = getattr(harness, "backend_runtime_instance_id", None)
    if not callable(resolver):
        return None
    try:
        runtime_instance_id = await resolver(workspace_root)
    except (AttributeError, TypeError, RuntimeError, OSError, ValueError):
        logger.debug(
            "Failed to resolve backend runtime instance id",
            exc_info=True,
        )
        return None
    if not isinstance(runtime_instance_id, str):
        return None
    normalized = runtime_instance_id.strip()
    return normalized or None


def _resolve_thread_runtime_binding(
    thread_store: ThreadExecutionStore, thread_target_id: str
) -> Optional[RuntimeThreadBinding]:
    getter = getattr(thread_store, "get_thread_runtime_binding", None)
    if callable(getter):
        binding = getter(thread_target_id)
        if binding is not None:
            return cast(RuntimeThreadBinding, binding)
    hub_root = getattr(thread_store, "hub_root", None)
    if isinstance(hub_root, Path):
        return get_runtime_thread_binding(hub_root, thread_target_id)
    return None


def _execution_record_from_store_row(record: Mapping[str, Any]) -> ExecutionRecord:
    return ExecutionRecord.from_mapping(record)


class PmaThreadExecutionStore(ThreadExecutionStore):
    """Adapter that hides PMA thread-store details behind orchestration nouns."""

    def __init__(self, store: PmaThreadStore) -> None:
        self._store = store

    @property
    def hub_root(self) -> Path:
        return self._store.hub_root

    def create_thread_target(
        self,
        agent_id: str,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        context_profile: Optional[CarContextProfile] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ThreadTarget:
        metadata_payload = dict(metadata or {})
        normalized_context_profile = normalize_car_context_profile(context_profile)
        if normalized_context_profile is not None:
            metadata_payload["context_profile"] = normalized_context_profile
        created = self._store.create_thread(
            agent_id,
            workspace_root,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            name=display_name,
            backend_thread_id=backend_thread_id,
            metadata=metadata_payload,
        )
        return _thread_target_from_store_row(created)

    def get_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        record = self._store.get_thread(thread_target_id)
        if record is None:
            return None
        return _thread_target_from_store_row_with_runtime_binding(self._store, record)

    def get_thread_runtime_binding(
        self, thread_target_id: str
    ) -> Optional[RuntimeThreadBinding]:
        return self._store.get_thread_runtime_binding(thread_target_id)

    def list_thread_targets(
        self,
        *,
        agent_id: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        runtime_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[ThreadTarget]:
        return [
            _thread_target_from_store_row_with_runtime_binding(self._store, record)
            for record in self._store.list_threads(
                agent=agent_id,
                status=lifecycle_status,
                normalized_status=runtime_status,
                repo_id=repo_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
                limit=limit,
            )
        ]

    def resume_thread_target(
        self,
        thread_target_id: str,
        *,
        backend_thread_id: Optional[str] = None,
        backend_runtime_instance_id: Optional[str] = None,
    ) -> Optional[ThreadTarget]:
        record = self._store.get_thread(thread_target_id)
        if record is None:
            return None
        if backend_thread_id is not None:
            self._store.set_thread_backend_id(
                thread_target_id,
                backend_thread_id,
                backend_runtime_instance_id=backend_runtime_instance_id,
            )
        self._store.activate_thread(thread_target_id)
        updated = self._store.get_thread(thread_target_id)
        if updated is None:
            return None
        return _thread_target_from_store_row(updated)

    def archive_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        record = self._store.get_thread(thread_target_id)
        if record is None:
            return None
        self._store.archive_thread(thread_target_id)
        updated = self._store.get_thread(thread_target_id)
        if updated is None:
            return None
        return _thread_target_from_store_row(updated)

    def set_thread_backend_id(
        self,
        thread_target_id: str,
        backend_thread_id: Optional[str],
        *,
        backend_runtime_instance_id: Optional[str] = None,
    ) -> None:
        self._store.set_thread_backend_id(
            thread_target_id,
            backend_thread_id,
            backend_runtime_instance_id=backend_runtime_instance_id,
        )

    def create_execution(
        self,
        thread_target_id: str,
        *,
        prompt: str,
        request_kind: MessageRequestKind = "message",
        busy_policy: str = "reject",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        client_request_id: Optional[str] = None,
        queue_payload: Optional[dict[str, Any]] = None,
    ) -> ExecutionRecord:
        created = self._store.create_turn(
            thread_target_id,
            prompt=prompt,
            request_kind=request_kind,
            busy_policy=busy_policy,
            model=model,
            reasoning=reasoning,
            client_turn_id=client_request_id,
            queue_payload=queue_payload,
        )
        return _execution_record_from_store_row(created)

    def get_execution(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[ExecutionRecord]:
        record = self._store.get_turn(thread_target_id, execution_id)
        if record is None:
            return None
        return _execution_record_from_store_row(record)

    def get_running_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        record = self._store.get_running_turn(thread_target_id)
        if record is None:
            return None
        return _execution_record_from_store_row(record)

    def get_latest_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        record = self._store.get_running_turn(thread_target_id)
        if record is None:
            record = next(iter(self._store.list_turns(thread_target_id, limit=1)), None)
        if record is None:
            return None
        return _execution_record_from_store_row(record)

    def list_queued_executions(
        self, thread_target_id: str, *, limit: int = 200
    ) -> list[ExecutionRecord]:
        return [
            _execution_record_from_store_row(record)
            for record in self._store.list_queued_turns(thread_target_id, limit=limit)
        ]

    def get_queue_depth(self, thread_target_id: str) -> int:
        return self._store.get_queue_depth(thread_target_id)

    def cancel_queued_execution(self, thread_target_id: str, execution_id: str) -> bool:
        return self._store.cancel_queued_turn(thread_target_id, execution_id)

    def promote_queued_execution(
        self, thread_target_id: str, execution_id: str
    ) -> bool:
        return self._store.promote_queued_turn(thread_target_id, execution_id)

    def claim_next_queued_execution(
        self, thread_target_id: str
    ) -> Optional[tuple[ExecutionRecord, dict[str, Any]]]:
        claimed = self._store.claim_next_queued_turn(thread_target_id)
        if claimed is None:
            return None
        execution, payload = claimed
        return _execution_record_from_store_row(execution), payload

    def set_execution_backend_id(
        self, execution_id: str, backend_turn_id: Optional[str]
    ) -> None:
        self._store.set_turn_backend_turn_id(execution_id, backend_turn_id)

    def _notify_terminal_transition(
        self,
        *,
        thread_target_id: str,
        execution_id: str,
        status: Optional[str],
        error: Optional[str] = None,
    ) -> None:
        normalized_status = str(status or "").strip().lower()
        if normalized_status == "ok":
            to_state = "completed"
            reason = "managed_turn_completed"
        elif normalized_status == "interrupted":
            to_state = "interrupted"
            reason = "managed_turn_interrupted"
        else:
            to_state = "failed"
            reason = str(error or "").strip() or "managed_turn_failed"

        thread = self.get_thread_target(thread_target_id)
        payload: dict[str, Any] = {
            "thread_id": thread_target_id,
            "from_state": "running",
            "to_state": to_state,
            "reason": reason,
            "timestamp": now_iso(),
            "event_type": f"managed_thread_{to_state}",
            "transition_id": f"managed_turn:{execution_id}:{to_state}",
            "idempotency_key": f"managed_turn:{execution_id}:{to_state}",
            "managed_thread_id": thread_target_id,
            "managed_turn_id": execution_id,
        }
        if thread is not None:
            if thread.repo_id:
                payload["repo_id"] = thread.repo_id
            if thread.resource_kind:
                payload["resource_kind"] = thread.resource_kind
            if thread.resource_id:
                payload["resource_id"] = thread.resource_id
            payload["agent"] = thread.agent_id

        try:
            result = PmaAutomationStore(self._store.hub_root).notify_transition(payload)
        except (OSError, RuntimeError, TypeError, ValueError):
            logger.exception(
                "Failed to notify PMA automation for terminal managed-thread transition "
                "(thread_target_id=%s, execution_id=%s, to_state=%s)",
                thread_target_id,
                execution_id,
                to_state,
            )
            return

        try:
            created = int(result.get("created") or 0)
        except (TypeError, ValueError):
            created = 0
        if created > 0:
            logger.info(
                "Managed-thread PMA transition enqueued wakeups "
                "(thread_target_id=%s, execution_id=%s, event_type=%s, created=%s)",
                thread_target_id,
                execution_id,
                payload["event_type"],
                created,
            )

    def record_execution_result(
        self,
        thread_target_id: str,
        execution_id: str,
        *,
        status: str,
        assistant_text: Optional[str] = None,
        error: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
        transcript_turn_id: Optional[str] = None,
    ) -> ExecutionRecord:
        updated = self._store.mark_turn_finished(
            execution_id,
            status=status,
            assistant_text=assistant_text,
            error=error,
            backend_turn_id=backend_turn_id,
            transcript_turn_id=transcript_turn_id,
        )
        if not updated:
            raise KeyError(f"Execution '{execution_id}' was not running")
        execution = self.get_execution(thread_target_id, execution_id)
        if execution is None:
            raise KeyError(
                f"Execution '{execution_id}' is missing after result recording"
            )
        self._notify_terminal_transition(
            thread_target_id=thread_target_id,
            execution_id=execution_id,
            status=execution.status,
            error=execution.error,
        )
        return execution

    def record_execution_interrupted(
        self, thread_target_id: str, execution_id: str
    ) -> ExecutionRecord:
        updated = self._store.mark_turn_interrupted(execution_id)
        execution = self.get_execution(thread_target_id, execution_id)
        if not updated:
            if execution is not None and execution.status == "interrupted":
                return execution
            raise KeyError(f"Execution '{execution_id}' was not running")
        if execution is None:
            raise KeyError(
                f"Execution '{execution_id}' is missing after interrupt recording"
            )
        self._notify_terminal_transition(
            thread_target_id=thread_target_id,
            execution_id=execution_id,
            status=execution.status,
            error=execution.error,
        )
        return execution

    def cancel_queued_executions(self, thread_target_id: str) -> int:
        return self._store.cancel_queued_turns(thread_target_id)

    def record_thread_activity(
        self,
        thread_target_id: str,
        *,
        execution_id: Optional[str],
        message_preview: Optional[str],
    ) -> None:
        self._store.update_thread_after_turn(
            thread_target_id,
            last_turn_id=execution_id,
            last_message_preview=message_preview,
        )


@dataclass(frozen=True)
class _ClaimedThreadExecutionRequest:
    """Typed queued execution context carried through queue replay."""

    thread: ThreadTarget
    execution: ExecutionRecord
    queued_request: QueuedExecutionRequest

    @property
    def request(self) -> MessageRequest:
        return self.queued_request.request

    @property
    def client_request_id(self) -> Optional[str]:
        return self.queued_request.client_request_id

    @property
    def sandbox_policy(self) -> Optional[Any]:
        return self.queued_request.sandbox_policy

    def as_legacy_tuple(
        self,
    ) -> tuple[ThreadTarget, ExecutionRecord, MessageRequest, Optional[str], Any]:
        return (
            self.thread,
            self.execution,
            self.request,
            self.client_request_id,
            self.sandbox_policy,
        )


@dataclass
class _ThreadRuntimeAdapter:
    """Thread resolution and runtime acquisition boundary for orchestration."""

    definition_catalog: AgentDefinitionCatalog
    thread_store: ThreadExecutionStore
    harness_factory: HarnessFactory

    @staticmethod
    def resolve_thread_agent_profile(thread: ThreadTarget) -> Optional[str]:
        return (
            str(thread.agent_profile).strip().lower()
            if isinstance(thread.agent_profile, str) and thread.agent_profile.strip()
            else None
        )

    def harness_for_agent(
        self, agent_id: str, profile: Optional[str] = None
    ) -> RuntimeThreadHarness:
        factory = self.harness_factory
        try:
            return factory(agent_id, profile)
        except TypeError as exc:
            if "positional argument" not in str(exc):
                raise
            return factory(agent_id)

    def harness_for_thread(self, thread: ThreadTarget) -> RuntimeThreadHarness:
        return self.harness_for_agent(
            thread.agent_id,
            self.resolve_thread_agent_profile(thread),
        )

    def create_thread_target(
        self,
        agent_id: str,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        context_profile: Optional[CarContextProfile] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ThreadTarget:
        definition = self.definition_catalog.get_definition(agent_id)
        if definition is None:
            raise KeyError(f"Unknown agent definition '{agent_id}'")
        if "durable_threads" not in definition.capabilities:
            raise ValueError(
                f"Agent definition '{agent_id}' does not support durable_threads"
            )
        return self.thread_store.create_thread_target(
            agent_id,
            workspace_root,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            display_name=display_name,
            backend_thread_id=backend_thread_id,
            context_profile=context_profile,
            metadata=metadata,
        )

    def resolve_thread_target(
        self,
        *,
        thread_target_id: Optional[str],
        agent_id: str,
        workspace_root: Path,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        context_profile: Optional[CarContextProfile] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ThreadTarget:
        if thread_target_id:
            thread = self.thread_store.get_thread_target(thread_target_id)
            if thread is None:
                raise KeyError(f"Unknown thread target '{thread_target_id}'")
            return thread
        return self.create_thread_target(
            agent_id,
            workspace_root,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            display_name=display_name,
            backend_thread_id=backend_thread_id,
            context_profile=context_profile,
            metadata=metadata,
        )

    def resume_thread_target(
        self,
        thread_target_id: str,
        *,
        backend_thread_id: Optional[str] = None,
        backend_runtime_instance_id: Optional[str] = None,
    ) -> ThreadTarget:
        thread = self.thread_store.resume_thread_target(
            thread_target_id,
            backend_thread_id=backend_thread_id,
            backend_runtime_instance_id=backend_runtime_instance_id,
        )
        if thread is None:
            raise KeyError(f"Unknown thread target '{thread_target_id}'")
        return thread

    async def acquire_workspace_runtime(
        self, agent_id: str, workspace_root: Path
    ) -> WorkspaceRuntimeAcquisition:
        harness = self.harness_for_agent(agent_id)
        await harness.ensure_ready(workspace_root)
        return WorkspaceRuntimeAcquisition(
            harness=harness,
            backend_runtime_instance_id=await _resolve_harness_runtime_instance_id(
                harness,
                workspace_root,
            ),
        )

    async def resolve_backend_runtime_instance_id(
        self, agent_id: str, workspace_root: Path
    ) -> Optional[str]:
        runtime = await self.acquire_workspace_runtime(agent_id, workspace_root)
        return runtime.backend_runtime_instance_id

    def archive_thread_target(self, thread_target_id: str) -> ThreadTarget:
        thread = self.thread_store.archive_thread_target(thread_target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{thread_target_id}'")
        return thread

    def get_thread_runtime_binding(
        self, thread_target_id: str
    ) -> Optional[RuntimeThreadBinding]:
        return _resolve_thread_runtime_binding(self.thread_store, thread_target_id)


@dataclass
class _ThreadQueueRequestAdapter:
    """Owns queued-request serialization and claim/replay reconstruction."""

    thread_store: ThreadExecutionStore
    get_thread_target: Callable[[str], Optional[ThreadTarget]]

    def payload_for_request(
        self,
        request: MessageRequest,
        *,
        client_request_id: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> dict[str, Any]:
        return QueuedExecutionRequest(
            request=request,
            client_request_id=client_request_id,
            sandbox_policy=sandbox_policy,
        ).to_payload()

    def claim_next_queued_execution(
        self, thread_target_id: str
    ) -> Optional[_ClaimedThreadExecutionRequest]:
        claimed = self.thread_store.claim_next_queued_execution(thread_target_id)
        if claimed is None:
            return None
        execution, payload = claimed
        thread = self.get_thread_target(thread_target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{thread_target_id}'")
        if not thread.workspace_root:
            raise RuntimeError("Thread target is missing workspace_root")
        queued_request = QueuedExecutionRequest.from_payload(
            payload,
            thread_target_id=thread_target_id,
        )
        return _ClaimedThreadExecutionRequest(
            thread=thread,
            execution=execution,
            queued_request=queued_request,
        )


@dataclass
class _ThreadExecutionLifecycle:
    """Owns runtime-thread start and queued replay lifecycle concerns.

    Ownership contract:
    - This class is responsible for starting new executions and replaying
      queued executions.  It handles harness preparation, conversation
      creation/resumption, rehydration prefix assembly, and fresh-conversation
      retries.
    - It must **not** own recovery or completion-gap logic.  Stale-backend
      binding validation is delegated to ``_ThreadRecoveryHelper``.
    - It never records terminal execution results directly; that responsibility
      belongs to the thread store and recovery helper.
    """

    thread_store: ThreadExecutionStore
    get_execution: Callable[[str, str], Optional[ExecutionRecord]]
    harness_for_thread: Callable[[ThreadTarget], RuntimeThreadHarness]
    _stale_binding_checker: Optional[Callable[..., bool]] = None

    @staticmethod
    def resolve_runtime_prompt(request: MessageRequest) -> str:
        runtime_prompt = request.message_text
        raw_runtime_prompt = request.metadata.get("runtime_prompt")
        if isinstance(raw_runtime_prompt, str) and raw_runtime_prompt.strip():
            runtime_prompt = raw_runtime_prompt
        return runtime_prompt

    def build_rehydration_prefix(
        self, thread: ThreadTarget, *, include_compact_seed: bool
    ) -> Optional[str]:
        sections: list[str] = []
        compact_seed = _truncate_rehydration_text(thread.compact_seed or "")
        if include_compact_seed and compact_seed:
            sections.append(f"Compacted context summary:\n{compact_seed}")

        hub_root = getattr(self.thread_store, "hub_root", None)
        if isinstance(hub_root, Path):
            transcript_store = TranscriptMirrorStore(hub_root)
            transcript_entries = transcript_store.list_target_history(
                target_kind="thread_target",
                target_id=thread.thread_target_id,
                limit=_REHYDRATION_TRANSCRIPT_LIMIT,
            )
            transcript_sections: list[str] = []
            for index, entry in enumerate(reversed(transcript_entries), start=1):
                content = _truncate_rehydration_text(str(entry.get("content") or ""))
                if not content:
                    continue
                transcript_sections.append(f"Recent transcript {index}:\n{content}")
            if transcript_sections:
                sections.append("\n\n".join(transcript_sections))

        if not sections:
            return None
        return (
            "Recovered durable conversation state for this managed thread. "
            "A fresh backend conversation was started because no live backend "
            "binding was available.\n\n" + "\n\n".join(sections)
        )

    def rehydrated_runtime_prompt(
        self, thread: ThreadTarget, runtime_prompt: str
    ) -> str:
        prefix = self.build_rehydration_prefix(
            thread,
            include_compact_seed="Context summary (from compaction):"
            not in runtime_prompt,
        )
        if not prefix:
            return runtime_prompt
        return f"{prefix}\n\n{runtime_prompt}"

    @staticmethod
    def mark_fresh_backend_session(
        request: MessageRequest,
        *,
        reason: str,
        rehydrated: bool,
    ) -> None:
        request.metadata["fresh_backend_session_started"] = True
        request.metadata["fresh_backend_session_reason"] = reason
        request.metadata["fresh_backend_session_notice"] = (
            _FRESH_BACKEND_SESSION_REHYDRATED_NOTICE
            if rehydrated
            else _FRESH_BACKEND_SESSION_NOTICE
        )

    async def start_execution(
        self,
        thread: ThreadTarget,
        request: MessageRequest,
        execution: ExecutionRecord,
        *,
        harness: RuntimeThreadHarness,
        workspace_root: Path,
        sandbox_policy: Optional[Any],
    ) -> ExecutionRecord:
        runtime_prompt = self.resolve_runtime_prompt(request)
        fresh_conversation_retry_attempted = False
        rehydrated_runtime_prompt = False
        fresh_backend_session_reason: Optional[str] = None
        previous_backend_thread_id: Optional[str] = None
        runtime_instance_id: Optional[str] = None
        conversation_id: Optional[str] = None
        used_existing_conversation = False
        try:
            await harness.ensure_ready(workspace_root)
            runtime_instance_id = await _resolve_harness_runtime_instance_id(
                harness, workspace_root
            )
            runtime_binding = _resolve_thread_runtime_binding(
                self.thread_store, thread.thread_target_id
            )
            conversation_id = (
                runtime_binding.backend_thread_id
                if runtime_binding is not None
                else None
            )
            if self._stale_binding_checker is not None:
                if self._stale_binding_checker(
                    thread_target_id=thread.thread_target_id,
                    backend_thread_id=conversation_id,
                    runtime_instance_id=runtime_instance_id,
                ):
                    fresh_backend_session_reason = "stale_runtime_instance"
                    previous_backend_thread_id = conversation_id
                    conversation_id = None
            while True:
                used_existing_conversation = conversation_id is not None
                try:
                    if conversation_id:
                        try:
                            conversation = await harness.resume_conversation(
                                workspace_root, conversation_id
                            )
                        except (
                            RuntimeError,
                            OSError,
                            ValueError,
                            TypeError,
                            AttributeError,
                            ConnectionError,
                        ) as exc:
                            if not _is_recoverable_backend_error(exc):
                                raise
                            log_event(
                                logger,
                                logging.INFO,
                                "orchestration.thread.resume_recoverable_backend_error",
                                exc=exc,
                                thread_target_id=thread.thread_target_id,
                                backend_thread_id=conversation_id,
                                action="start_new_conversation",
                            )
                            fresh_backend_session_reason = "resume_recoverable_error"
                            previous_backend_thread_id = conversation_id
                            self.thread_store.set_thread_backend_id(
                                thread.thread_target_id,
                                None,
                                backend_runtime_instance_id=None,
                            )
                            conversation_id = None
                            continue
                        resumed_conversation_id = getattr(conversation, "id", None)
                        if (
                            isinstance(resumed_conversation_id, str)
                            and resumed_conversation_id
                            and resumed_conversation_id != conversation_id
                        ):
                            conversation_id = resumed_conversation_id
                            self.thread_store.set_thread_backend_id(
                                thread.thread_target_id,
                                conversation_id,
                                backend_runtime_instance_id=runtime_instance_id,
                            )
                        elif (
                            runtime_instance_id
                            and runtime_binding
                            and runtime_binding.backend_runtime_instance_id
                            != runtime_instance_id
                        ):
                            self.thread_store.set_thread_backend_id(
                                thread.thread_target_id,
                                conversation_id,
                                backend_runtime_instance_id=runtime_instance_id,
                            )
                    else:
                        if not rehydrated_runtime_prompt:
                            prefix = self.build_rehydration_prefix(
                                thread,
                                include_compact_seed="Context summary (from compaction):"
                                not in runtime_prompt,
                            )
                            should_mark_fresh_backend_session = bool(
                                previous_backend_thread_id
                                or str(
                                    getattr(thread, "last_execution_id", "") or ""
                                ).strip()
                                or str(
                                    getattr(thread, "compact_seed", "") or ""
                                ).strip()
                            )
                            if should_mark_fresh_backend_session:
                                fresh_backend_session_reason = (
                                    fresh_backend_session_reason
                                    or "missing_backend_binding"
                                )
                                self.mark_fresh_backend_session(
                                    request,
                                    reason=fresh_backend_session_reason,
                                    rehydrated=bool(prefix),
                                )
                                log_event(
                                    logger,
                                    logging.INFO,
                                    "orchestration.thread.fresh_backend_session_started",
                                    thread_target_id=thread.thread_target_id,
                                    execution_id=execution.execution_id,
                                    previous_backend_thread_id=(
                                        previous_backend_thread_id
                                    ),
                                    request_kind=request.kind,
                                    reason=fresh_backend_session_reason,
                                    rehydrated=bool(prefix),
                                )
                            if prefix:
                                runtime_prompt = f"{prefix}\n\n{runtime_prompt}"
                            rehydrated_runtime_prompt = True
                        conversation = await harness.new_conversation(
                            workspace_root,
                            title=thread.display_name,
                        )
                        conversation_id = conversation.id
                        self.thread_store.set_thread_backend_id(
                            thread.thread_target_id,
                            conversation_id,
                            backend_runtime_instance_id=runtime_instance_id,
                        )
                    provisional_turn_id = f"{conversation_id}:{int(time.time() * 1000)}"
                    self.thread_store.set_execution_backend_id(
                        execution.execution_id, provisional_turn_id
                    )
                    log_event(
                        logger,
                        logging.INFO,
                        "orchestration.thread.provisional_backend_turn_id",
                        thread_target_id=thread.thread_target_id,
                        execution_id=execution.execution_id,
                        conversation_id=conversation_id,
                        provisional_turn_id=provisional_turn_id,
                    )
                    if request.kind == "review":
                        if not harness.supports("review"):
                            raise RuntimeError(
                                f"Agent '{thread.agent_id}' does not support review mode"
                            )
                        turn = await harness.start_review(
                            workspace_root,
                            conversation_id,
                            runtime_prompt,
                            request.model,
                            request.reasoning,
                            approval_mode=request.approval_mode,
                            sandbox_policy=sandbox_policy,
                        )
                    else:
                        turn = await harness.start_turn(
                            workspace_root,
                            conversation_id,
                            runtime_prompt,
                            request.model,
                            request.reasoning,
                            approval_mode=request.approval_mode,
                            sandbox_policy=sandbox_policy,
                            input_items=request.input_items,
                        )
                    resolved_turn_id = str(getattr(turn, "turn_id", "") or "").strip()
                    if not resolved_turn_id:
                        raise RuntimeError(
                            f"Agent '{thread.agent_id}' returned an empty turn id"
                        )
                    break
                except FreshConversationRequiredError as exc:
                    if (
                        not used_existing_conversation
                        or fresh_conversation_retry_attempted
                    ):
                        raise
                    fresh_conversation_retry_attempted = True
                    log_event(
                        logger,
                        logging.INFO,
                        "orchestration.thread.refreshing_backend_binding",
                        thread_target_id=thread.thread_target_id,
                        execution_id=execution.execution_id,
                        backend_thread_id=conversation_id,
                        operation=exc.operation,
                        status_code=exc.status_code,
                        reason=str(exc),
                    )
                    fresh_backend_session_reason = "fresh_conversation_required"
                    previous_backend_thread_id = conversation_id
                    self.thread_store.set_thread_backend_id(
                        thread.thread_target_id,
                        None,
                        backend_runtime_instance_id=None,
                    )
                    conversation_id = None
                    continue
                except (
                    RuntimeError,
                    OSError,
                    ValueError,
                    TypeError,
                    AttributeError,
                    ConnectionError,
                ) as exc:
                    if (
                        not used_existing_conversation
                        or fresh_conversation_retry_attempted
                        or not _is_recoverable_backend_error(exc)
                    ):
                        raise
                    fresh_conversation_retry_attempted = True
                    log_event(
                        logger,
                        logging.INFO,
                        "orchestration.thread.refreshing_backend_binding",
                        thread_target_id=thread.thread_target_id,
                        execution_id=execution.execution_id,
                        backend_thread_id=conversation_id,
                        operation=(
                            "start_review" if request.kind == "review" else "start_turn"
                        ),
                        status_code=None,
                        reason=str(exc),
                    )
                    fresh_backend_session_reason = "start_turn_recoverable_error"
                    previous_backend_thread_id = conversation_id
                    self.thread_store.set_thread_backend_id(
                        thread.thread_target_id,
                        None,
                        backend_runtime_instance_id=None,
                    )
                    conversation_id = None
                    continue
        except asyncio.CancelledError as exc:
            detail = (
                str(request.metadata.get("execution_error_message") or "").strip()
                or CLAIMED_EXECUTION_START_CANCELLED_ERROR
            )
            log_event(
                logger,
                logging.WARNING,
                "orchestration.thread.start_failed",
                thread_target_id=thread.thread_target_id,
                execution_id=execution.execution_id,
                backend_thread_id=conversation_id,
                request_kind=request.kind,
                fresh_conversation_retry_attempted=fresh_conversation_retry_attempted,
                reported_error=detail,
                error_type=type(exc).__name__,
            )
            try:
                self.thread_store.record_execution_result(
                    thread.thread_target_id,
                    execution.execution_id,
                    status="error",
                    assistant_text="",
                    error=detail,
                    backend_turn_id=None,
                    transcript_turn_id=None,
                )
            except KeyError:
                refreshed = self.get_execution(
                    thread.thread_target_id, execution.execution_id
                )
                if refreshed is None:
                    raise
            raise
        except (
            Exception
        ) as exc:  # intentional: top-level execution boundary records all harness failures
            detail = (
                str(request.metadata.get("execution_error_message") or "").strip()
                or str(exc).strip()
                or "Runtime thread execution failed"
            )
            runtime_binding = _resolve_thread_runtime_binding(
                self.thread_store, thread.thread_target_id
            )
            log_event(
                logger,
                logging.WARNING,
                "orchestration.thread.start_failed",
                exc=exc,
                thread_target_id=thread.thread_target_id,
                execution_id=execution.execution_id,
                backend_thread_id=(
                    runtime_binding.backend_thread_id if runtime_binding else None
                ),
                request_kind=request.kind,
                fresh_conversation_retry_attempted=fresh_conversation_retry_attempted,
                reported_error=detail,
            )
            try:
                return self.thread_store.record_execution_result(
                    thread.thread_target_id,
                    execution.execution_id,
                    status="error",
                    assistant_text="",
                    error=detail,
                    backend_turn_id=None,
                    transcript_turn_id=None,
                )
            except KeyError:
                refreshed = self.get_execution(
                    thread.thread_target_id, execution.execution_id
                )
                if refreshed is not None:
                    return refreshed
                raise

        resolved_conversation_id = getattr(turn, "conversation_id", conversation_id)
        if (
            isinstance(resolved_conversation_id, str)
            and resolved_conversation_id
            and resolved_conversation_id != conversation_id
        ):
            self.thread_store.set_thread_backend_id(
                thread.thread_target_id,
                resolved_conversation_id,
                backend_runtime_instance_id=runtime_instance_id,
            )
        self.thread_store.set_execution_backend_id(
            execution.execution_id, resolved_turn_id
        )
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.runtime_turn_started",
            thread_target_id=thread.thread_target_id,
            execution_id=execution.execution_id,
            backend_thread_id=resolved_conversation_id,
            backend_turn_id=resolved_turn_id,
            request_kind=request.kind,
            reused_conversation=used_existing_conversation,
            stale_session_recovery=fresh_conversation_retry_attempted,
        )
        refreshed = self.get_execution(thread.thread_target_id, execution.execution_id)
        if refreshed is None:
            raise KeyError(
                f"Execution '{execution.execution_id}' is missing after creation"
            )
        return refreshed

    def claimed_execution_start_error_detail(
        self,
        request: MessageRequest,
        exc: BaseException,
    ) -> str:
        configured = str(
            getattr(request, "metadata", {}).get("execution_error_message") or ""
        ).strip()
        if configured:
            return configured
        detail = str(exc).strip()
        if detail:
            return detail
        if isinstance(exc, asyncio.CancelledError):
            return CLAIMED_EXECUTION_START_CANCELLED_ERROR
        return "Runtime thread execution failed"

    def record_claimed_execution_start_failure(
        self,
        claimed: _ClaimedThreadExecutionRequest,
        exc: BaseException,
    ) -> None:
        try:
            current = self.get_execution(
                claimed.thread.thread_target_id, claimed.execution.execution_id
            )
        except Exception:
            current = None
        if current is not None and current.status != "running":
            return
        detail = self.claimed_execution_start_error_detail(claimed.request, exc)
        logged_exc = exc if isinstance(exc, Exception) else None
        log_event(
            logger,
            logging.WARNING,
            "orchestration.thread.claimed_start_failed",
            exc=logged_exc,
            thread_target_id=claimed.thread.thread_target_id,
            execution_id=claimed.execution.execution_id,
            request_kind=claimed.request.kind,
            reported_error=detail,
        )
        try:
            self.thread_store.record_execution_result(
                claimed.thread.thread_target_id,
                claimed.execution.execution_id,
                status="error",
                assistant_text="",
                error=detail,
                backend_turn_id=None,
                transcript_turn_id=None,
            )
        except KeyError:
            return

    async def start_claimed_execution_request(
        self,
        claimed: _ClaimedThreadExecutionRequest,
        *,
        harness: Optional[RuntimeThreadHarness] = None,
        workspace_root: Optional[Path] = None,
    ) -> tuple[ExecutionRecord, RuntimeThreadHarness]:
        resolved_workspace_root = workspace_root
        if resolved_workspace_root is None:
            if not claimed.thread.workspace_root:
                raise RuntimeError("Thread target is missing workspace_root")
            resolved_workspace_root = Path(claimed.thread.workspace_root)
        try:
            resolved_harness = harness or self.harness_for_thread(claimed.thread)
            started = await self.start_execution(
                claimed.thread,
                claimed.request,
                claimed.execution,
                harness=resolved_harness,
                workspace_root=resolved_workspace_root,
                sandbox_policy=claimed.sandbox_policy,
            )
            return started, resolved_harness
        except BaseException as exc:
            self.record_claimed_execution_start_failure(claimed, exc)
            raise


@dataclass
class _ThreadRecoveryHelper:
    """Owns interrupt, stop, restart recovery, and stale-binding validation.

    Ownership contract:
    - This helper is the sole authority for managed-thread recovery decisions.
    - It never synthesizes a successful completion outcome. All recovery paths
      record either ``error`` or ``interrupted`` status.
    - Stale backend bindings (where the stored runtime instance id differs from
      the current one) are detected and cleared here, not in the execution
      lifecycle layer.
    - Callers must not substitute their own stale-binding detection logic.
    """

    thread_store: ThreadExecutionStore
    get_thread_target: Callable[[str], Optional[ThreadTarget]]
    get_running_execution: Callable[[str], Optional[ExecutionRecord]]
    harness_for_thread: Callable[[ThreadTarget], RuntimeThreadHarness]

    def clear_stale_backend_binding(
        self,
        *,
        thread_target_id: str,
        backend_thread_id: Optional[str],
        runtime_instance_id: Optional[str],
    ) -> bool:
        if not backend_thread_id or not runtime_instance_id:
            return False
        runtime_binding = _resolve_thread_runtime_binding(
            self.thread_store, thread_target_id
        )
        if runtime_binding is None:
            return False
        if (
            not runtime_binding.backend_runtime_instance_id
            or runtime_binding.backend_runtime_instance_id == runtime_instance_id
        ):
            return False
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.stale_backend_binding",
            thread_target_id=thread_target_id,
            backend_thread_id=backend_thread_id,
            stored_runtime_instance_id=runtime_binding.backend_runtime_instance_id,
            current_runtime_instance_id=runtime_instance_id,
            action="clear_stale_binding",
        )
        self.thread_store.set_thread_backend_id(
            thread_target_id,
            None,
            backend_runtime_instance_id=None,
        )
        return True

    async def interrupt_thread(self, thread_target_id: str) -> ExecutionRecord:
        thread = self.get_thread_target(thread_target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{thread_target_id}'")
        if not thread.workspace_root:
            raise RuntimeError("Thread target is missing workspace_root")
        runtime_binding = _resolve_thread_runtime_binding(
            self.thread_store, thread_target_id
        )

        execution = self.get_running_execution(thread_target_id)
        if execution is None:
            raise KeyError(
                f"Thread target '{thread_target_id}' has no running execution"
            )
        if runtime_binding is None or not runtime_binding.backend_thread_id:
            return self.thread_store.record_execution_interrupted(
                thread_target_id, execution.execution_id
            )

        harness = self.harness_for_thread(thread)
        if not harness.supports("interrupt"):
            raise RuntimeError(f"Agent '{thread.agent_id}' does not support interrupt")
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.interrupt_requested",
            thread_target_id=thread_target_id,
            execution_id=execution.execution_id,
            backend_thread_id=runtime_binding.backend_thread_id,
            backend_turn_id=execution.backend_id,
            agent_id=thread.agent_id,
        )
        await harness.interrupt(
            Path(thread.workspace_root),
            runtime_binding.backend_thread_id,
            execution.backend_id,
        )
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.interrupt_acknowledged",
            thread_target_id=thread_target_id,
            execution_id=execution.execution_id,
            backend_thread_id=runtime_binding.backend_thread_id,
            backend_turn_id=execution.backend_id,
            agent_id=thread.agent_id,
        )
        interrupted = self.thread_store.record_execution_interrupted(
            thread_target_id, execution.execution_id
        )
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.interrupt_recorded",
            thread_target_id=thread_target_id,
            execution_id=interrupted.execution_id,
            backend_thread_id=runtime_binding.backend_thread_id,
            backend_turn_id=interrupted.backend_id,
            status=interrupted.status,
        )
        return interrupted

    def recover_lost_backend_execution(
        self,
        *,
        thread_target_id: str,
        execution: ExecutionRecord,
        backend_thread_id: Optional[str],
        error_message: str,
        reason: str,
    ) -> ExecutionRecord:
        recovered = self.thread_store.record_execution_result(
            thread_target_id,
            execution.execution_id,
            status="error",
            assistant_text="",
            error=error_message,
            backend_turn_id=execution.backend_id,
            transcript_turn_id=None,
        )
        self.thread_store.set_thread_backend_id(
            thread_target_id,
            None,
            backend_runtime_instance_id=None,
        )
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.recovered_lost_backend",
            thread_target_id=thread_target_id,
            execution_id=execution.execution_id,
            backend_thread_id=backend_thread_id,
            backend_turn_id=execution.backend_id,
            reason=reason,
            error=error_message,
        )
        return recovered

    def interrupt_lost_backend_execution(
        self,
        *,
        thread_target_id: str,
        execution: ExecutionRecord,
        backend_thread_id: Optional[str],
        reason: str,
    ) -> ExecutionRecord:
        interrupted = self.thread_store.record_execution_interrupted(
            thread_target_id, execution.execution_id
        )
        self.thread_store.set_thread_backend_id(
            thread_target_id,
            None,
            backend_runtime_instance_id=None,
        )
        log_event(
            logger,
            logging.INFO,
            "orchestration.thread.recovered_lost_backend",
            thread_target_id=thread_target_id,
            execution_id=execution.execution_id,
            backend_thread_id=backend_thread_id,
            backend_turn_id=execution.backend_id,
            reason=reason,
            error=None,
        )
        return interrupted

    async def stop_thread(
        self,
        thread_target_id: str,
        *,
        cancel_queued: bool = True,
    ) -> ThreadStopOutcome:
        thread = self.get_thread_target(thread_target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{thread_target_id}'")
        runtime_binding = _resolve_thread_runtime_binding(
            self.thread_store, thread_target_id
        )

        cancelled_queued = (
            self.thread_store.cancel_queued_executions(thread_target_id)
            if cancel_queued
            else 0
        )
        execution = self.get_running_execution(thread_target_id)
        if execution is None:
            return ThreadStopOutcome(
                thread_target_id=thread_target_id,
                cancelled_queued=cancelled_queued,
            )

        backend_thread_id = (
            runtime_binding.backend_thread_id if runtime_binding is not None else None
        )
        if not backend_thread_id:
            interrupted = self.interrupt_lost_backend_execution(
                thread_target_id=thread_target_id,
                execution=execution,
                backend_thread_id=None,
                reason="missing_backend_thread_id",
            )
            return ThreadStopOutcome(
                thread_target_id=thread_target_id,
                cancelled_queued=cancelled_queued,
                execution=interrupted,
                interrupted_active=True,
                recovered_lost_backend=True,
            )

        runtime_instance_id: Optional[str] = None
        if thread.workspace_root:
            harness = self.harness_for_thread(thread)
            runtime_instance_id = await _resolve_harness_runtime_instance_id(
                harness, Path(thread.workspace_root)
            )
        if (
            runtime_instance_id
            and runtime_binding
            and runtime_binding.backend_runtime_instance_id
            and runtime_binding.backend_runtime_instance_id != runtime_instance_id
        ):
            log_event(
                logger,
                logging.INFO,
                "orchestration.thread.stop_stale_backend_binding",
                thread_target_id=thread_target_id,
                execution_id=execution.execution_id,
                backend_thread_id=backend_thread_id,
                stored_runtime_instance_id=runtime_binding.backend_runtime_instance_id,
                current_runtime_instance_id=runtime_instance_id,
            )
            interrupted = self.interrupt_lost_backend_execution(
                thread_target_id=thread_target_id,
                execution=execution,
                backend_thread_id=backend_thread_id,
                reason="stale_backend_runtime_instance",
            )
            return ThreadStopOutcome(
                thread_target_id=thread_target_id,
                cancelled_queued=cancelled_queued,
                execution=interrupted,
                interrupted_active=True,
                recovered_lost_backend=True,
            )

        try:
            interrupted = await self.interrupt_thread(thread_target_id)
        except Exception as exc:
            if not _is_recoverable_backend_error(exc):
                raise
            reason = (
                "interrupt_thread_not_found"
                if _is_missing_thread_error(exc)
                else "interrupt_thread_runtime_unavailable"
            )
            log_event(
                logger,
                logging.INFO,
                "orchestration.thread.interrupt_recoverable_backend_error",
                thread_target_id=thread_target_id,
                execution_id=execution.execution_id,
                backend_thread_id=backend_thread_id,
                backend_turn_id=execution.backend_id,
                reason=reason,
                exc=exc,
            )
            interrupted = self.interrupt_lost_backend_execution(
                thread_target_id=thread_target_id,
                execution=execution,
                backend_thread_id=backend_thread_id,
                reason=reason,
            )
            return ThreadStopOutcome(
                thread_target_id=thread_target_id,
                cancelled_queued=cancelled_queued,
                execution=interrupted,
                interrupted_active=True,
                recovered_lost_backend=True,
            )

        return ThreadStopOutcome(
            thread_target_id=thread_target_id,
            cancelled_queued=cancelled_queued,
            execution=interrupted,
            interrupted_active=True,
        )

    def recover_running_execution_after_restart(
        self, thread_target_id: str
    ) -> Optional[ExecutionRecord]:
        thread = self.get_thread_target(thread_target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{thread_target_id}'")

        execution = self.get_running_execution(thread_target_id)
        if execution is None:
            return None

        runtime_binding = _resolve_thread_runtime_binding(
            self.thread_store, thread_target_id
        )
        backend_thread_id = (
            runtime_binding.backend_thread_id if runtime_binding is not None else None
        )
        return self.recover_lost_backend_execution(
            thread_target_id=thread_target_id,
            execution=execution,
            backend_thread_id=backend_thread_id,
            error_message=(
                LOST_BACKEND_THREAD_ERROR
                if backend_thread_id
                else MISSING_BACKEND_THREAD_ERROR
            ),
            reason=(
                "startup_lost_backend_binding"
                if backend_thread_id
                else "startup_missing_backend_thread_id"
            ),
        )


@dataclass
class HarnessBackedOrchestrationService(OrchestrationThreadService):
    """Canonical runtime-thread orchestration service used by PMA and later surfaces.

    Ownership boundary:
    - ``RunnerOrchestrator`` owns repo-process lifecycle (start/stop/reconcile/resume/kill).
    - ``_ThreadExecutionLifecycle`` (via ``_execution_lifecycle``) owns execution start,
      rehydration, and fresh-conversation retries.
    - ``_ThreadRecoveryHelper`` (via ``_recovery_helper``) owns interrupt, stop, restart
      recovery, and stale-backend-binding validation.
    - ``runtime_thread_events`` owns backend-specific event normalization and bounded
      completion-gap recovery.
    These seams must not overlap: recovery code must not start executions, and execution
    start code must not synthesize completion outcomes.
    """

    definition_catalog: AgentDefinitionCatalog
    thread_store: ThreadExecutionStore
    harness_factory: HarnessFactory
    binding_store: Optional[OrchestrationBindingStore] = None
    _runtime_adapter: _ThreadRuntimeAdapter = field(init=False, repr=False)
    _queue_adapter: _ThreadQueueRequestAdapter = field(init=False, repr=False)
    _execution_lifecycle: _ThreadExecutionLifecycle = field(init=False, repr=False)
    _recovery_helper: _ThreadRecoveryHelper = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._runtime_adapter = _ThreadRuntimeAdapter(
            definition_catalog=self.definition_catalog,
            thread_store=self.thread_store,
            harness_factory=self.harness_factory,
        )
        self._queue_adapter = _ThreadQueueRequestAdapter(
            thread_store=self.thread_store,
            get_thread_target=self.get_thread_target,
        )
        self._recovery_helper = _ThreadRecoveryHelper(
            thread_store=self.thread_store,
            get_thread_target=self.get_thread_target,
            get_running_execution=self.get_running_execution,
            harness_for_thread=self._runtime_adapter.harness_for_thread,
        )
        self._execution_lifecycle = _ThreadExecutionLifecycle(
            thread_store=self.thread_store,
            get_execution=self.get_execution,
            harness_for_thread=self._runtime_adapter.harness_for_thread,
            _stale_binding_checker=self._recovery_helper.clear_stale_backend_binding,
        )

    def _harness_for_agent(
        self, agent_id: str, profile: Optional[str] = None
    ) -> RuntimeThreadHarness:
        return self._runtime_adapter.harness_for_agent(agent_id, profile)

    def _harness_for_thread(self, thread: ThreadTarget) -> RuntimeThreadHarness:
        return self._runtime_adapter.harness_for_thread(thread)

    def list_agent_definitions(self) -> list[AgentDefinition]:
        return self.definition_catalog.list_definitions()

    def get_agent_definition(self, agent_id: str) -> Optional[AgentDefinition]:
        return self.definition_catalog.get_definition(agent_id)

    def get_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        return self.thread_store.get_thread_target(thread_target_id)

    def list_thread_targets(
        self,
        *,
        agent_id: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        runtime_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[ThreadTarget]:
        return self.thread_store.list_thread_targets(
            agent_id=agent_id,
            lifecycle_status=lifecycle_status,
            runtime_status=runtime_status,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            limit=limit,
        )

    def get_thread_status(self, thread_target_id: str) -> Optional[str]:
        thread = self.get_thread_target(thread_target_id)
        if thread is None:
            return None
        return thread.status

    def get_thread_runtime_binding(
        self, thread_target_id: str
    ) -> Optional[RuntimeThreadBinding]:
        return self._runtime_adapter.get_thread_runtime_binding(thread_target_id)

    def upsert_binding(
        self,
        *,
        surface_kind: str,
        surface_key: str,
        thread_target_id: str,
        agent_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        mode: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ):
        if self.binding_store is None:
            raise RuntimeError("binding_store is not configured")
        return self.binding_store.upsert_binding(
            surface_kind=surface_kind,
            surface_key=surface_key,
            thread_target_id=thread_target_id,
            agent_id=agent_id,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            mode=mode,
            metadata=metadata,
        )

    def get_binding(
        self,
        *,
        surface_kind: str,
        surface_key: str,
        include_disabled: bool = False,
    ):
        if self.binding_store is None:
            return None
        return self.binding_store.get_binding(
            surface_kind=surface_kind,
            surface_key=surface_key,
            include_disabled=include_disabled,
        )

    def list_bindings(
        self,
        *,
        thread_target_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        surface_kind: Optional[str] = None,
        include_disabled: bool = False,
        limit: int = 200,
    ):
        if self.binding_store is None:
            return []
        return self.binding_store.list_bindings(
            thread_target_id=thread_target_id,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            agent_id=agent_id,
            surface_kind=surface_kind,
            include_disabled=include_disabled,
            limit=limit,
        )

    def get_active_thread_for_binding(
        self, *, surface_kind: str, surface_key: str
    ) -> Optional[str]:
        if self.binding_store is None:
            return None
        return self.binding_store.get_active_thread_for_binding(
            surface_kind=surface_kind,
            surface_key=surface_key,
        )

    def list_active_work_summaries(
        self,
        *,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[ActiveWorkSummary]:
        if self.binding_store is None:
            return []
        return self.binding_store.list_active_work_summaries(
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            agent_id=agent_id,
            limit=limit,
        )

    def create_thread_target(
        self,
        agent_id: str,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        context_profile: Optional[CarContextProfile] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ThreadTarget:
        return self._runtime_adapter.create_thread_target(
            agent_id,
            workspace_root,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            display_name=display_name,
            backend_thread_id=backend_thread_id,
            context_profile=context_profile,
            metadata=metadata,
        )

    def resolve_thread_target(
        self,
        *,
        thread_target_id: Optional[str],
        agent_id: str,
        workspace_root: Path,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        context_profile: Optional[CarContextProfile] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ThreadTarget:
        return self._runtime_adapter.resolve_thread_target(
            thread_target_id=thread_target_id,
            agent_id=agent_id,
            workspace_root=workspace_root,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            display_name=display_name,
            backend_thread_id=backend_thread_id,
            context_profile=context_profile,
            metadata=metadata,
        )

    def resume_thread_target(
        self,
        thread_target_id: str,
        *,
        backend_thread_id: Optional[str] = None,
        backend_runtime_instance_id: Optional[str] = None,
    ) -> ThreadTarget:
        return self._runtime_adapter.resume_thread_target(
            thread_target_id,
            backend_thread_id=backend_thread_id,
            backend_runtime_instance_id=backend_runtime_instance_id,
        )

    async def acquire_workspace_runtime(
        self, agent_id: str, workspace_root: Path
    ) -> WorkspaceRuntimeAcquisition:
        return await self._runtime_adapter.acquire_workspace_runtime(
            agent_id, workspace_root
        )

    async def resolve_backend_runtime_instance_id(
        self, agent_id: str, workspace_root: Path
    ) -> Optional[str]:
        return await self._runtime_adapter.resolve_backend_runtime_instance_id(
            agent_id, workspace_root
        )

    def archive_thread_target(self, thread_target_id: str) -> ThreadTarget:
        return self._runtime_adapter.archive_thread_target(thread_target_id)

    async def _start_execution(
        self,
        thread: ThreadTarget,
        request: MessageRequest,
        execution: ExecutionRecord,
        *,
        harness: RuntimeThreadHarness,
        workspace_root: Path,
        sandbox_policy: Optional[Any],
    ) -> ExecutionRecord:
        return await self._execution_lifecycle.start_execution(
            thread,
            request,
            execution,
            harness=harness,
            workspace_root=workspace_root,
            sandbox_policy=sandbox_policy,
        )

    async def send_message(
        self,
        request: MessageRequest,
        *,
        client_request_id: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        harness: Optional[RuntimeThreadHarness] = None,
    ) -> ExecutionRecord:
        execution, _resolved_harness = await self.send_message_with_started_harness(
            request,
            client_request_id=client_request_id,
            sandbox_policy=sandbox_policy,
            harness=harness,
        )
        return execution

    async def send_message_with_started_harness(
        self,
        request: MessageRequest,
        *,
        client_request_id: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        harness: Optional[RuntimeThreadHarness] = None,
    ) -> tuple[ExecutionRecord, Optional[RuntimeThreadHarness]]:
        if request.target_kind != "thread":
            raise ValueError("Thread orchestration service only handles thread targets")

        thread = self.get_thread_target(request.target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{request.target_id}'")
        if not thread.workspace_root:
            raise RuntimeError("Thread target is missing workspace_root")
        runtime_binding = _resolve_thread_runtime_binding(
            self.thread_store, thread.thread_target_id
        )

        definition = self.get_agent_definition(thread.agent_id)
        if definition is None:
            raise KeyError(f"Unknown agent definition '{thread.agent_id}'")

        workspace_root = Path(thread.workspace_root)
        queue_payload = self._queue_adapter.payload_for_request(
            request,
            client_request_id=client_request_id,
            sandbox_policy=sandbox_policy,
        )
        running = self.get_running_execution(thread.thread_target_id)
        if running is not None and request.busy_policy == "interrupt":
            try:
                await self.stop_thread(thread.thread_target_id)
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                AttributeError,
            ) as exc:
                current_running = self.get_running_execution(thread.thread_target_id)
                raise BusyInterruptFailedError(
                    thread_target_id=thread.thread_target_id,
                    active_execution_id=(
                        current_running.execution_id
                        if current_running is not None
                        else running.execution_id
                    ),
                    backend_thread_id=(
                        runtime_binding.backend_thread_id if runtime_binding else None
                    ),
                ) from exc
            current_running = self.get_running_execution(thread.thread_target_id)
            if current_running is not None:
                raise BusyInterruptFailedError(
                    thread_target_id=thread.thread_target_id,
                    active_execution_id=current_running.execution_id,
                    backend_thread_id=(
                        runtime_binding.backend_thread_id if runtime_binding else None
                    ),
                )
            thread = self.get_thread_target(thread.thread_target_id) or thread

        execution = self.thread_store.create_execution(
            thread.thread_target_id,
            prompt=request.message_text,
            request_kind=request.kind,
            busy_policy=request.busy_policy,
            model=request.model,
            reasoning=request.reasoning,
            client_request_id=client_request_id,
            queue_payload=queue_payload,
        )
        self.thread_store.record_thread_activity(
            thread.thread_target_id,
            execution_id=execution.execution_id,
            message_preview=_truncate_text(request.message_text, MessagePreviewLimit),
        )
        if execution.status != "running":
            return execution, None
        resolved_harness = harness or self._harness_for_agent(
            definition.agent_id,
            request.agent_profile,
        )
        started = await self._start_execution(
            thread,
            request,
            execution,
            harness=resolved_harness,
            workspace_root=workspace_root,
            sandbox_policy=sandbox_policy,
        )
        return started, resolved_harness

    def claim_next_queued_execution_context(
        self, thread_target_id: str
    ) -> Optional[_ClaimedThreadExecutionRequest]:
        return self._queue_adapter.claim_next_queued_execution(thread_target_id)

    def _claimed_execution_start_error_detail(
        self,
        request: MessageRequest,
        exc: BaseException,
    ) -> str:
        return self._execution_lifecycle.claimed_execution_start_error_detail(
            request, exc
        )

    def _record_claimed_execution_start_failure(
        self,
        thread: ThreadTarget,
        execution: ExecutionRecord,
        request: MessageRequest,
        exc: BaseException,
    ) -> None:
        self._execution_lifecycle.record_claimed_execution_start_failure(
            _ClaimedThreadExecutionRequest(
                thread=thread,
                execution=execution,
                queued_request=QueuedExecutionRequest(
                    request=request,
                    sandbox_policy=None,
                ),
            ),
            exc,
        )

    async def _start_claimed_execution_request(
        self,
        thread: ThreadTarget,
        request: MessageRequest,
        execution: ExecutionRecord,
        *,
        harness: Optional[RuntimeThreadHarness] = None,
        workspace_root: Optional[Path] = None,
        sandbox_policy: Optional[Any] = None,
    ) -> tuple[ExecutionRecord, RuntimeThreadHarness]:
        return await self._execution_lifecycle.start_claimed_execution_request(
            _ClaimedThreadExecutionRequest(
                thread=thread,
                execution=execution,
                queued_request=QueuedExecutionRequest(
                    request=request,
                    sandbox_policy=sandbox_policy,
                ),
            ),
            harness=harness,
            workspace_root=workspace_root,
        )

    def claim_next_queued_execution_request(
        self, thread_target_id: str
    ) -> Optional[
        tuple[ThreadTarget, ExecutionRecord, MessageRequest, Optional[str], Any]
    ]:
        claimed = self.claim_next_queued_execution_context(thread_target_id)
        return None if claimed is None else claimed.as_legacy_tuple()

    async def start_next_queued_execution(
        self,
        thread_target_id: str,
        *,
        harness: Optional[RuntimeThreadHarness] = None,
    ) -> Optional[ExecutionRecord]:
        claimed = self.claim_next_queued_execution_context(thread_target_id)
        if claimed is None:
            return None
        (
            started,
            _resolved_harness,
        ) = await self._execution_lifecycle.start_claimed_execution_request(
            claimed,
            harness=harness,
            workspace_root=(
                Path(claimed.thread.workspace_root)
                if claimed.thread.workspace_root
                else None
            ),
        )
        return started

    async def interrupt_thread(self, thread_target_id: str) -> ExecutionRecord:
        return await self._recovery_helper.interrupt_thread(thread_target_id)

    async def stop_thread(
        self,
        thread_target_id: str,
        *,
        cancel_queued: bool = True,
    ) -> ThreadStopOutcome:
        return await self._recovery_helper.stop_thread(
            thread_target_id,
            cancel_queued=cancel_queued,
        )

    def recover_running_execution_after_restart(
        self, thread_target_id: str
    ) -> Optional[ExecutionRecord]:
        return self._recovery_helper.recover_running_execution_after_restart(
            thread_target_id
        )

    def get_execution(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[ExecutionRecord]:
        return self.thread_store.get_execution(thread_target_id, execution_id)

    def get_running_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        return self.thread_store.get_running_execution(thread_target_id)

    def get_latest_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        return self.thread_store.get_latest_execution(thread_target_id)

    def list_queued_executions(
        self, thread_target_id: str, *, limit: int = 200
    ) -> list[ExecutionRecord]:
        return self.thread_store.list_queued_executions(thread_target_id, limit=limit)

    def get_queue_depth(self, thread_target_id: str) -> int:
        return self.thread_store.get_queue_depth(thread_target_id)

    def cancel_queued_execution(self, thread_target_id: str, execution_id: str) -> bool:
        return self.thread_store.cancel_queued_execution(
            thread_target_id,
            execution_id,
        )

    def promote_queued_execution(
        self, thread_target_id: str, execution_id: str
    ) -> bool:
        return self.thread_store.promote_queued_execution(
            thread_target_id,
            execution_id,
        )

    def record_execution_result(
        self,
        thread_target_id: str,
        execution_id: str,
        *,
        status: str,
        assistant_text: Optional[str] = None,
        error: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
        transcript_turn_id: Optional[str] = None,
    ) -> ExecutionRecord:
        return self.thread_store.record_execution_result(
            thread_target_id,
            execution_id,
            status=status,
            assistant_text=assistant_text,
            error=error,
            backend_turn_id=backend_turn_id,
            transcript_turn_id=transcript_turn_id,
        )

    def record_execution_interrupted(
        self, thread_target_id: str, execution_id: str
    ) -> ExecutionRecord:
        return self.thread_store.record_execution_interrupted(
            thread_target_id, execution_id
        )

    def cancel_queued_executions(self, thread_target_id: str) -> int:
        return self.thread_store.cancel_queued_executions(thread_target_id)


@dataclass
class FlowBackedOrchestrationService(OrchestrationFlowService):
    """Canonical orchestration service boundary for CAR-native flow targets."""

    flow_wrappers: Mapping[str, TicketFlowTargetWrapper]

    def list_flow_targets(self) -> list[FlowTarget]:
        return [wrapper.flow_target for wrapper in self.flow_wrappers.values()]

    def get_flow_target(self, flow_target_id: str) -> Optional[FlowTarget]:
        wrapper = self.flow_wrappers.get(flow_target_id)
        if wrapper is None:
            return None
        return wrapper.flow_target

    def _require_wrapper(self, flow_target_id: str) -> TicketFlowTargetWrapper:
        wrapper = self.flow_wrappers.get(flow_target_id)
        if wrapper is None:
            raise KeyError(f"Unknown flow target '{flow_target_id}'")
        return wrapper

    def _find_wrapper_for_run(
        self, run_id: str
    ) -> tuple[Optional[TicketFlowTargetWrapper], Optional[FlowRunTarget]]:
        for wrapper in self.flow_wrappers.values():
            run = wrapper.get_run(run_id)
            if run is not None:
                return wrapper, run
        return None, None

    async def start_flow_run(
        self,
        flow_target_id: str,
        *,
        input_data: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> FlowRunTarget:
        return await self._require_wrapper(flow_target_id).start_run(
            input_data=input_data,
            metadata=metadata,
            run_id=run_id,
        )

    async def resume_flow_run(
        self, run_id: str, *, force: bool = False
    ) -> FlowRunTarget:
        wrapper, existing = self._find_wrapper_for_run(run_id)
        if wrapper is None or existing is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        return await wrapper.resume_run(existing.run_id, force=force)

    async def stop_flow_run(self, run_id: str) -> FlowRunTarget:
        wrapper, existing = self._find_wrapper_for_run(run_id)
        if wrapper is None or existing is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        return await wrapper.stop_run(existing.run_id)

    def ensure_flow_run_worker(self, run_id: str, *, is_terminal: bool = False) -> None:
        wrapper, existing = self._find_wrapper_for_run(run_id)
        if wrapper is None or existing is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        wrapper.ensure_run_worker(existing.run_id, is_terminal=is_terminal)

    def reconcile_flow_run(self, run_id: str) -> tuple[FlowRunTarget, bool, bool]:
        wrapper, existing = self._find_wrapper_for_run(run_id)
        if wrapper is None or existing is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        return wrapper.reconcile_run(existing.run_id)

    async def wait_for_flow_run_terminal(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.25,
    ) -> Optional[FlowRunTarget]:
        wrapper, existing = self._find_wrapper_for_run(run_id)
        if wrapper is None or existing is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        return await wrapper.wait_for_terminal(
            existing.run_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def archive_flow_run(
        self,
        run_id: str,
        *,
        force: bool = False,
        delete_run: bool = True,
    ) -> dict[str, Any]:
        wrapper, existing = self._find_wrapper_for_run(run_id)
        if wrapper is None or existing is None:
            raise KeyError(f"Unknown flow run '{run_id}'")
        return wrapper.archive_run(
            existing.run_id,
            force=force,
            delete_run=delete_run,
        )

    def get_flow_run(self, run_id: str) -> Optional[FlowRunTarget]:
        _, run = self._find_wrapper_for_run(run_id)
        return run

    def list_flow_runs(
        self, *, flow_target_id: Optional[str] = None
    ) -> list[FlowRunTarget]:
        if flow_target_id is not None:
            wrapper = self.flow_wrappers.get(flow_target_id)
            return [] if wrapper is None else wrapper.list_runs()

        runs: list[FlowRunTarget] = []
        for wrapper in self.flow_wrappers.values():
            runs.extend(wrapper.list_runs())
        return runs

    def list_active_flow_runs(
        self, *, flow_target_id: Optional[str] = None
    ) -> list[FlowRunTarget]:
        if flow_target_id is not None:
            wrapper = self.flow_wrappers.get(flow_target_id)
            return [] if wrapper is None else wrapper.list_active_runs()

        active_runs: list[FlowRunTarget] = []
        for wrapper in self.flow_wrappers.values():
            active_runs.extend(wrapper.list_active_runs())
        return active_runs


ResolvePausedFlowTarget = Callable[
    [SurfaceThreadMessageRequest],
    Awaitable[Optional[PausedFlowTarget]],
]
SubmitFlowReply = Callable[
    [SurfaceThreadMessageRequest, PausedFlowTarget],
    Awaitable[Any],
]
SubmitThreadMessage = Callable[[SurfaceThreadMessageRequest], Awaitable[Any]]
RunThreadControl = Callable[[ThreadControlRequest], Awaitable[Any]]


@dataclass(frozen=True)
class SurfaceIngressResult:
    """Result of routing one surface request through orchestration ingress."""

    route: str
    events: tuple[OrchestrationEvent, ...] = ()
    thread_result: Any = None
    flow_result: Any = None
    flow_target: Optional[PausedFlowTarget] = None
    control_result: Any = None


@dataclass
class SurfaceOrchestrationIngress:
    """Shared ingress for surfaces that need thread-versus-flow routing."""

    event_sink: Optional[Callable[[OrchestrationEvent], None]] = None

    def _emit(
        self,
        events: list[OrchestrationEvent],
        *,
        event_type: str,
        target_kind: str,
        surface_kind: str,
        target_id: Optional[str] = None,
        status: Optional[str] = None,
        detail: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        event = OrchestrationEvent(
            event_type=event_type,  # type: ignore[arg-type]
            target_kind=target_kind,  # type: ignore[arg-type]
            surface_kind=surface_kind,
            target_id=target_id,
            status=status,
            detail=detail,
            metadata=dict(metadata or {}),
        )
        events.append(event)
        if self.event_sink is not None:
            self.event_sink(event)

    async def submit_message(
        self,
        request: SurfaceThreadMessageRequest,
        *,
        resolve_paused_flow_target: ResolvePausedFlowTarget,
        submit_flow_reply: SubmitFlowReply,
        submit_thread_message: SubmitThreadMessage,
    ) -> SurfaceIngressResult:
        events: list[OrchestrationEvent] = []
        self._emit(
            events,
            event_type="ingress.received",
            target_kind="thread",
            surface_kind=request.surface_kind,
            metadata={"pma_enabled": request.pma_enabled},
        )
        flow_target = None
        if not request.pma_enabled:
            flow_target = await resolve_paused_flow_target(request)
        if flow_target is not None:
            self._emit(
                events,
                event_type="ingress.target_resolved",
                target_kind="flow",
                surface_kind=request.surface_kind,
                target_id=flow_target.flow_target.flow_target_id,
                status=flow_target.status,
                metadata={"run_id": flow_target.run_id},
            )
            flow_result = await submit_flow_reply(request, flow_target)
            self._emit(
                events,
                event_type="ingress.flow_resumed",
                target_kind="flow",
                surface_kind=request.surface_kind,
                target_id=flow_target.flow_target.flow_target_id,
                status=flow_target.status,
                metadata={"run_id": flow_target.run_id},
            )
            return SurfaceIngressResult(
                route="flow",
                events=tuple(events),
                flow_result=flow_result,
                flow_target=flow_target,
            )

        self._emit(
            events,
            event_type="ingress.target_resolved",
            target_kind="thread",
            surface_kind=request.surface_kind,
            target_id=request.agent_id,
            metadata={"workspace_root": str(request.workspace_root)},
        )
        thread_result = await submit_thread_message(request)
        self._emit(
            events,
            event_type="ingress.thread_submitted",
            target_kind="thread",
            surface_kind=request.surface_kind,
            target_id=request.agent_id,
            metadata={"workspace_root": str(request.workspace_root)},
        )
        return SurfaceIngressResult(
            route="thread",
            events=tuple(events),
            thread_result=thread_result,
        )

    async def run_thread_control(
        self,
        request: ThreadControlRequest,
        *,
        control_runner: RunThreadControl,
    ) -> SurfaceIngressResult:
        events: list[OrchestrationEvent] = []
        self._emit(
            events,
            event_type="ingress.control_requested",
            target_kind="thread",
            surface_kind=request.surface_kind,
            target_id=request.target_id,
            status=request.action,
        )
        control_result = await control_runner(request)
        self._emit(
            events,
            event_type="ingress.control_completed",
            target_kind="thread",
            surface_kind=request.surface_kind,
            target_id=request.target_id,
            status=request.action,
        )
        return SurfaceIngressResult(
            route="thread_control",
            events=tuple(events),
            control_result=control_result,
        )


def build_surface_orchestration_ingress(
    *, event_sink: Optional[Callable[[OrchestrationEvent], None]] = None
) -> SurfaceOrchestrationIngress:
    """Build the shared ingress facade used by chat surfaces."""

    return SurfaceOrchestrationIngress(event_sink=event_sink)


def get_surface_orchestration_ingress(owner: Any) -> SurfaceOrchestrationIngress:
    """Return the lazily-initialized ingress facade for a surface/service object."""

    existing = getattr(owner, "_surface_orchestration_ingress", None)
    if isinstance(existing, SurfaceOrchestrationIngress):
        return existing
    created = build_surface_orchestration_ingress()
    owner._surface_orchestration_ingress = created
    return created


def build_harness_backed_orchestration_service(
    *,
    descriptors: Mapping[str, RuntimeAgentDescriptor],
    harness_factory: HarnessFactory,
    thread_store: Optional[ThreadExecutionStore] = None,
    pma_thread_store: Optional[PmaThreadStore] = None,
    definition_catalog: Optional[AgentDefinitionCatalog] = None,
    binding_store: Optional[OrchestrationBindingStore] = None,
) -> HarnessBackedOrchestrationService:
    """Build the default runtime-thread orchestration service for current PMA state."""

    if thread_store is None:
        if pma_thread_store is None:
            raise ValueError("thread_store or pma_thread_store is required")
        thread_store = PmaThreadExecutionStore(pma_thread_store)
    if definition_catalog is None:
        definition_catalog = MappingAgentDefinitionCatalog(descriptors)
    if binding_store is None and pma_thread_store is not None:
        hub_root = getattr(pma_thread_store, "_hub_root", None)
        if isinstance(hub_root, Path):
            binding_store = OrchestrationBindingStore(hub_root)
    return HarnessBackedOrchestrationService(
        definition_catalog=definition_catalog,
        thread_store=thread_store,
        harness_factory=harness_factory,
        binding_store=binding_store,
    )


def build_ticket_flow_orchestration_service(
    *,
    workspace_root: Path,
    repo_id: Optional[str] = None,
) -> FlowBackedOrchestrationService:
    """Build the orchestration wrapper that exposes `ticket_flow` as a flow target."""

    wrapper = build_ticket_flow_target_wrapper(workspace_root, repo_id=repo_id)
    return FlowBackedOrchestrationService(
        flow_wrappers={wrapper.flow_target.flow_target_id: wrapper}
    )


__all__ = [
    "FlowBackedOrchestrationService",
    "HarnessBackedOrchestrationService",
    "MessagePreviewLimit",
    "PmaThreadExecutionStore",
    "SurfaceIngressResult",
    "SurfaceOrchestrationIngress",
    "build_harness_backed_orchestration_service",
    "build_surface_orchestration_ingress",
    "build_ticket_flow_orchestration_service",
    "get_surface_orchestration_ingress",
]
