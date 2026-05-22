from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional

from ..car_context import CarContextProfile
from ..domain.refs import ScopeRef
from ..logging_utils import log_event
from ..text_utils import _truncate_text
from .bindings import ActiveWorkSummary, OrchestrationBindingStore
from .execution_lifecycle import (
    _ClaimedThreadExecutionRequest,
    _message_request_from_turn_request,
    _resolve_harness_runtime_instance_id,
    _resolve_thread_runtime_binding,
    _ThreadExecutionLifecycle,
)
from .interfaces import (
    AgentDefinitionCatalog,
    OrchestrationThreadService,
    RuntimeThreadHarness,
    ThreadExecutionStore,
    WorkspaceRuntimeAcquisition,
)
from .models import (
    AgentDefinition,
    ExecutionRecord,
    MessageRequest,
    QueuedExecutionRequest,
    ThreadStopOutcome,
    ThreadTarget,
)
from .recovery_lifecycle import (
    BusyInterruptFailedError,
    ManagedTurnRecoveryScanResult,
    RecoveryScanner,
    _ThreadRecoveryHelper,
)
from .runtime_bindings import RuntimeThreadBinding
from .turn_context import turn_assembly_from_request_metadata
from .turn_execution_contract import (
    TurnExecutionOrigin,
    TurnExecutionRecord,
    TurnExecutionRequest,
)
from .turn_execution_storage import build_turn_execution_record_from_storage

MessagePreviewLimit = 120
logger = logging.getLogger("codex_autorunner.core.orchestration.service")
HarnessFactory = Callable[..., RuntimeThreadHarness]


@dataclass(frozen=True)
class PreparedThreadExecution:
    """Execution row plus enough context to start the runtime turn later."""

    thread: ThreadTarget
    request: MessageRequest
    turn_request: TurnExecutionRequest
    turn_record: TurnExecutionRecord
    execution: ExecutionRecord
    workspace_root: Path
    sandbox_policy: Optional[Any]
    harness: Optional[RuntimeThreadHarness] = None

    def to_claimed_request(self) -> _ClaimedThreadExecutionRequest:
        return _ClaimedThreadExecutionRequest(
            thread=self.thread,
            execution=self.execution,
            queued_request=QueuedExecutionRequest(
                request=self.request,
                sandbox_policy=self.sandbox_policy,
            ),
            turn_request=self.turn_request,
            turn_record=self.turn_record,
        )


def _normalize_recovered_execution_status(
    status: Any,
    *,
    assistant_text: str,
    errors: list[str],
) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"ok", "completed", "complete", "success", "succeeded"}:
        return "ok"
    if normalized in {"interrupted", "cancelled", "canceled", "aborted"}:
        return "interrupted"
    if assistant_text and not errors and not normalized:
        return "ok"
    return "error"


def _record_thread_activity_best_effort(
    thread_store: ThreadExecutionStore,
    thread_target_id: str,
    *,
    execution_id: Optional[str],
    message_preview: Optional[str],
) -> None:
    try:
        thread_store.record_thread_activity(
            thread_target_id,
            execution_id=execution_id,
            message_preview=message_preview,
        )
    except RuntimeError as exc:
        from ..hub_control_plane.errors import is_retryable_hub_control_plane_failure

        if not is_retryable_hub_control_plane_failure(exc):
            raise
        error_code = str(getattr(exc, "code", "") or "").strip()
        retryable = bool(getattr(exc, "retryable", False))
        log_event(
            logger,
            logging.WARNING,
            "orchestration.thread_activity.record_degraded",
            thread_target_id=thread_target_id,
            execution_id=execution_id,
            retryable=retryable,
            error_code=error_code or None,
            detail=str(exc),
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
        scope: Optional[ScopeRef] = None,
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
            scope=scope,
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
        scope: Optional[ScopeRef] = None,
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
            scope=scope,
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
        binding_state: Optional[str] = None,
        state_reason: Optional[str] = None,
    ) -> ThreadTarget:
        thread = self.thread_store.resume_thread_target(
            thread_target_id,
            backend_thread_id=backend_thread_id,
            backend_runtime_instance_id=backend_runtime_instance_id,
            binding_state=binding_state,
            state_reason=state_reason,
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
        request: TurnExecutionRequest,
        *,
        client_request_id: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> dict[str, Any]:
        return {
            "turn_request": request.to_dict(),
            "client_request_id": client_request_id,
            "sandbox_policy": sandbox_policy,
        }

    def queued_request_from_turn_request(
        self, request: TurnExecutionRequest
    ) -> QueuedExecutionRequest:
        return QueuedExecutionRequest(
            request=_message_request_from_turn_request(request),
            client_request_id=request.client_request_id,
            sandbox_policy=(
                None
                if request.sandbox_policy == "dangerFullAccess"
                else request.sandbox_policy
            ),
        )

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
        request_loader = getattr(self.thread_store, "get_turn_execution_request", None)
        record_loader = getattr(self.thread_store, "get_turn_execution_record", None)
        if not callable(request_loader) or not callable(record_loader):
            raise RuntimeError("Thread store cannot load canonical turn requests")
        turn_request = request_loader(thread_target_id, execution.execution_id)
        if not isinstance(turn_request, TurnExecutionRequest):
            request_data = payload.get("turn_request")
            if not isinstance(request_data, Mapping):
                raise RuntimeError(
                    f"Execution '{execution.execution_id}' is missing canonical turn request"
                )
            turn_request = TurnExecutionRequest.from_mapping(request_data)
            if turn_request.target_id != thread_target_id:
                raise RuntimeError(
                    "Queued execution turn_request target_id does not match thread_target_id"
                )
        turn_record = record_loader(thread_target_id, execution.execution_id)
        if not isinstance(turn_record, TurnExecutionRecord):
            record_data = payload.get("turn_record")
            if isinstance(record_data, Mapping):
                turn_record = TurnExecutionRecord.from_mapping(record_data)
            else:
                turn_record = build_turn_execution_record_from_storage(
                    execution=execution.to_dict(),
                    thread=thread.to_dict(),
                    request=turn_request,
                    queue_item=payload,
                )
        queued_request = self.queued_request_from_turn_request(turn_request)
        return _ClaimedThreadExecutionRequest(
            thread=thread,
            execution=execution,
            queued_request=queued_request,
            turn_request=turn_request,
            turn_record=turn_record,
        )


def _canonical_request_from_message_request(
    request: MessageRequest,
    *,
    request_id: str,
    thread: ThreadTarget,
    workspace_root: Path,
    client_request_id: Optional[str],
    sandbox_policy: Any,
) -> TurnExecutionRequest:
    model_payload = (
        _opencode_model_payload(request.model) if thread.agent_id == "opencode" else {}
    )
    approval_policy = request.approval_mode or "never"
    return TurnExecutionRequest(
        request_id=request_id,
        target_id=request.target_id,
        target_kind=request.target_kind,
        workspace_root=str(workspace_root),
        request_kind=request.kind,
        busy_policy=request.busy_policy,
        prompt_text=request.message_text,
        input_items=tuple(dict(item) for item in request.input_items or ()),
        context_profile=request.context_profile,
        agent=thread.agent_id,
        profile=request.agent_profile,
        model=request.model,
        model_payload=model_payload,
        reasoning=request.reasoning,
        approval_policy=approval_policy,
        approval_mode=request.approval_mode,
        sandbox_policy=sandbox_policy,
        client_request_id=client_request_id,
        idempotency_key=client_request_id or request_id,
        correlation_id=client_request_id or request_id,
        origin=TurnExecutionOrigin(
            kind="system",
            source_id="orchestration-thread-service",
        ),
        metadata=dict(request.metadata),
    )


def _opencode_model_payload(model: Optional[str]) -> dict[str, str]:
    if model is None or "/" not in model:
        return {}
    provider_id, model_id = (part.strip() for part in model.split("/", 1))
    if not provider_id or not model_id:
        return {}
    return {"providerID": provider_id, "modelID": model_id}


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
            _stale_binding_checker=self._recovery_helper.hint_stale_backend_binding_for_resume,
            _logger=logger,
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
        scope: Optional[ScopeRef] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        context_profile: Optional[CarContextProfile] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ThreadTarget:
        return self._runtime_adapter.create_thread_target(
            agent_id,
            workspace_root,
            scope=scope,
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
        binding_state: Optional[str] = None,
        state_reason: Optional[str] = None,
    ) -> ThreadTarget:
        return self._runtime_adapter.resume_thread_target(
            thread_target_id,
            backend_thread_id=backend_thread_id,
            backend_runtime_instance_id=backend_runtime_instance_id,
            binding_state=binding_state,
            state_reason=state_reason,
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
            turn_request=(
                self.thread_store.get_turn_execution_request(
                    thread.thread_target_id, execution.execution_id
                )
                if hasattr(self.thread_store, "get_turn_execution_request")
                else None
            ),
        )

    async def send_message(
        self,
        request: MessageRequest | TurnExecutionRequest,
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
        request: MessageRequest | TurnExecutionRequest,
        *,
        client_request_id: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        harness: Optional[RuntimeThreadHarness] = None,
    ) -> tuple[ExecutionRecord, Optional[RuntimeThreadHarness]]:
        prepared = await self.prepare_thread_execution(
            request,
            client_request_id=client_request_id,
            sandbox_policy=sandbox_policy,
            harness=harness,
        )
        if prepared.execution.status != "running":
            return prepared.execution, None
        started, resolved_harness = await self.start_prepared_thread_execution(prepared)
        return started, resolved_harness

    async def prepare_thread_execution(
        self,
        request: MessageRequest | TurnExecutionRequest,
        *,
        client_request_id: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        harness: Optional[RuntimeThreadHarness] = None,
    ) -> PreparedThreadExecution:
        canonical_request: Optional[TurnExecutionRequest]
        message_request: MessageRequest
        if isinstance(request, TurnExecutionRequest):
            canonical_request = request
            message_request = _message_request_from_turn_request(request)
        else:
            canonical_request = None
            message_request = request

        if message_request.target_kind != "thread":
            raise ValueError("Thread orchestration service only handles thread targets")

        thread = self.get_thread_target(message_request.target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{message_request.target_id}'")
        if not thread.workspace_root:
            raise RuntimeError("Thread target is missing workspace_root")
        runtime_binding = _resolve_thread_runtime_binding(
            self.thread_store, thread.thread_target_id
        )

        definition = self.get_agent_definition(thread.agent_id)
        if definition is None:
            raise KeyError(f"Unknown agent definition '{thread.agent_id}'")

        workspace_root = Path(thread.workspace_root)
        if canonical_request is not None:
            client_request_id = client_request_id or canonical_request.client_request_id
            sandbox_policy = (
                sandbox_policy
                if sandbox_policy is not None
                else canonical_request.sandbox_policy
            )
        else:
            sandbox_policy = (
                sandbox_policy if sandbox_policy is not None else "dangerFullAccess"
            )
            canonical_request = _canonical_request_from_message_request(
                message_request,
                request_id=client_request_id or str(uuid.uuid4()),
                thread=thread,
                workspace_root=workspace_root,
                client_request_id=client_request_id,
                sandbox_policy=sandbox_policy,
            )
        turn_assembly = turn_assembly_from_request_metadata(
            message_text=message_request.message_text,
            metadata=message_request.metadata,
        )
        request_metadata = {
            **message_request.metadata,
            **turn_assembly.metadata_patch(),
            "runtime_prompt": turn_assembly.raw_model_prompt,
        }
        message_request.metadata.clear()
        message_request.metadata.update(request_metadata)
        queue_payload = self._queue_adapter.payload_for_request(
            canonical_request,
            client_request_id=client_request_id,
            sandbox_policy=sandbox_policy,
        )
        running = self.get_running_execution(thread.thread_target_id)
        if running is not None and message_request.busy_policy == "interrupt":
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

        turn_request = canonical_request
        if turn_request is not None and dict(turn_request.metadata) != request_metadata:
            turn_request = replace(turn_request, metadata=request_metadata)
        execution = self.thread_store.create_execution(
            thread.thread_target_id,
            prompt=turn_assembly.user_visible_text,
            request_kind=message_request.kind,
            busy_policy=message_request.busy_policy,
            model=message_request.model,
            reasoning=message_request.reasoning,
            client_request_id=client_request_id,
            metadata=request_metadata,
            queue_payload=queue_payload,
            turn_request=turn_request,
        )
        _record_thread_activity_best_effort(
            self.thread_store,
            thread.thread_target_id,
            execution_id=execution.execution_id,
            message_preview=_truncate_text(
                turn_assembly.title_seed, MessagePreviewLimit
            ),
        )
        request_loader = getattr(self.thread_store, "get_turn_execution_request", None)
        record_loader = getattr(self.thread_store, "get_turn_execution_record", None)
        if not callable(request_loader) or not callable(record_loader):
            raise RuntimeError("Thread store cannot load canonical turn execution data")
        turn_request = request_loader(thread.thread_target_id, execution.execution_id)
        if not isinstance(turn_request, TurnExecutionRequest):
            raise RuntimeError(
                f"Execution '{execution.execution_id}' is missing canonical turn request"
            )
        turn_record = record_loader(thread.thread_target_id, execution.execution_id)
        if not isinstance(turn_record, TurnExecutionRecord):
            raise RuntimeError(
                f"Execution '{execution.execution_id}' is missing canonical turn record"
            )
        resolved_harness = harness if execution.status == "running" else None
        if resolved_harness is None and execution.status == "running":
            resolved_harness = self._harness_for_agent(
                definition.agent_id,
                message_request.agent_profile,
            )
        return PreparedThreadExecution(
            thread=thread,
            request=message_request,
            turn_request=turn_request,
            turn_record=turn_record,
            execution=execution,
            workspace_root=workspace_root,
            sandbox_policy=sandbox_policy,
            harness=resolved_harness,
        )

    async def start_prepared_thread_execution(
        self,
        prepared: PreparedThreadExecution,
    ) -> tuple[ExecutionRecord, RuntimeThreadHarness]:
        if prepared.execution.status != "running":
            raise ValueError("Only running executions can be started")
        return await self._execution_lifecycle.start_claimed_execution_request(
            prepared.to_claimed_request(),
            harness=prepared.harness,
            workspace_root=prepared.workspace_root,
        )

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
                turn_request=(
                    self.thread_store.get_turn_execution_request(
                        thread.thread_target_id, execution.execution_id
                    )
                    if hasattr(self.thread_store, "get_turn_execution_request")
                    else None
                ),
            ),
            harness=harness,
            workspace_root=workspace_root,
        )

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

    async def recover_running_execution_from_harness(
        self,
        thread_target_id: str,
        *,
        default_error: Optional[str] = None,
    ) -> Optional[ExecutionRecord]:
        thread = self.get_thread_target(thread_target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{thread_target_id}'")

        execution = self.get_running_execution(thread_target_id)
        if execution is None:
            return None

        workspace_root = (
            Path(thread.workspace_root)
            if isinstance(thread.workspace_root, str) and thread.workspace_root.strip()
            else None
        )
        if workspace_root is None:
            return None

        runtime_binding = _resolve_thread_runtime_binding(
            self.thread_store, thread_target_id
        )
        backend_thread_id = (
            runtime_binding.backend_thread_id
            if runtime_binding is not None and runtime_binding.backend_thread_id
            else (
                thread.backend_thread_id.strip()
                if isinstance(thread.backend_thread_id, str)
                and thread.backend_thread_id.strip()
                else None
            )
        )
        backend_turn_id = (
            execution.backend_id.strip()
            if isinstance(execution.backend_id, str) and execution.backend_id.strip()
            else None
        )
        if backend_thread_id is None or backend_turn_id is None:
            return None

        harness = self._harness_for_thread(thread)
        recover = getattr(harness, "recover_stalled_turn", None)
        if not callable(recover):
            return None

        try:
            recovered = await recover(
                workspace_root, backend_thread_id, backend_turn_id
            )
        except asyncio.CancelledError:
            raise
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            ConnectionError,
        ):
            return None
        if recovered is None:
            return None

        assistant_text = str(getattr(recovered, "assistant_text", "") or "")
        raw_errors = getattr(recovered, "errors", None)
        errors = []
        if isinstance(raw_errors, list):
            errors = [
                normalized
                for item in raw_errors
                if (normalized := str(item or "").strip())
            ]
        status = _normalize_recovered_execution_status(
            getattr(recovered, "status", None),
            assistant_text=assistant_text,
            errors=errors,
        )
        error_text: Optional[str] = None
        if status == "error":
            error_text = errors[0] if errors else (default_error or execution.error)
        elif status == "interrupted":
            error_text = errors[0] if errors else execution.error

        log_event(
            logger,
            logging.WARNING,
            "orchestration.thread.recovered_from_harness",
            thread_target_id=thread_target_id,
            execution_id=execution.execution_id,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
            recovered_status=status,
            recovered_output_chars=len(assistant_text),
            error_text=error_text,
            agent_id=thread.agent_id,
            agent_profile=thread.agent_profile,
        )
        try:
            return self.thread_store.record_execution_result(
                thread_target_id,
                execution.execution_id,
                status=status,
                assistant_text=assistant_text if status == "ok" else "",
                error=error_text,
                backend_turn_id=backend_turn_id,
                transcript_turn_id=None,
            )
        except KeyError:
            refreshed = self.get_execution(thread_target_id, execution.execution_id)
            if refreshed is not None:
                return refreshed
            raise

    def record_stale_running_execution_error(
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
            runtime_binding.backend_thread_id
            if runtime_binding is not None and runtime_binding.backend_thread_id
            else (
                thread.backend_thread_id.strip()
                if isinstance(thread.backend_thread_id, str)
                and thread.backend_thread_id.strip()
                else None
            )
        )
        return self._recovery_helper.recover_lost_backend_execution(
            thread_target_id=thread_target_id,
            execution=execution,
            backend_thread_id=backend_thread_id,
            error_message="Running execution could not be reattached after restart",
            reason="stale_managed_turn_recovery_scanner",
        )

    async def recover_stale_managed_thread_turns(
        self,
        *,
        stale_after_seconds: float,
    ) -> ManagedTurnRecoveryScanResult:
        """Scan stale managed-thread turns and record terminal state.

        This recovery path uses durable orchestration rows as the source of truth.
        If a live backend can provide a terminal result, that result is recorded;
        otherwise the stale execution is terminal-recorded as an error so queued
        work can advance.
        """
        list_running = getattr(
            self.thread_store, "list_thread_ids_with_running_executions", None
        )
        if not callable(list_running):
            return ManagedTurnRecoveryScanResult(
                scanned=0,
                changed=0,
                decisions=(),
            )
        scanner = RecoveryScanner(
            recover_from_harness=self.recover_running_execution_from_harness,
            record_lost_execution=self.record_stale_running_execution_error,
            get_running_execution=self.get_running_execution,
            list_thread_ids_with_running_executions=list_running,
            get_queue_depth=self.get_queue_depth,
            stale_after_seconds=stale_after_seconds,
            logger=logger,
        )
        return await scanner.scan()

    def get_execution(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[ExecutionRecord]:
        return self.thread_store.get_execution(thread_target_id, execution_id)

    def get_previous_completed_execution(
        self,
        thread_target_id: str,
        *,
        exclude_execution_id: Optional[str] = None,
    ) -> Optional[ExecutionRecord]:
        return self.thread_store.get_previous_completed_execution(
            thread_target_id,
            exclude_execution_id=exclude_execution_id,
        )

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
        assistant_output: Optional[Any] = None,
        error: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
        transcript_turn_id: Optional[str] = None,
    ) -> ExecutionRecord:
        return self.thread_store.record_execution_result(
            thread_target_id,
            execution_id,
            status=status,
            assistant_text=assistant_text,
            assistant_output=assistant_output,
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
