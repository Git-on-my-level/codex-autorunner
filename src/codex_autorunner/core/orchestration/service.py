from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..pma_thread_store import PmaThreadStore
from .catalog import MappingAgentDefinitionCatalog, RuntimeAgentDescriptor
from .interfaces import (
    AgentDefinitionCatalog,
    OrchestrationThreadService,
    RuntimeThreadHarness,
    ThreadExecutionStore,
)
from .models import AgentDefinition, ExecutionRecord, MessageRequest, ThreadTarget

MessagePreviewLimit = 120


def _truncate_text(value: str, limit: int = MessagePreviewLimit) -> str:
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3] + "..."


def _thread_target_from_store_row(record: Mapping[str, Any]) -> ThreadTarget:
    return ThreadTarget.from_mapping(record)


def _execution_record_from_store_row(record: Mapping[str, Any]) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=str(record.get("managed_turn_id") or ""),
        target_id=str(record.get("managed_thread_id") or ""),
        target_kind="thread",
        status=str(record.get("status") or ""),
        backend_id=(
            str(record["backend_turn_id"])
            if record.get("backend_turn_id") is not None
            else None
        ),
        started_at=(
            str(record["started_at"]) if record.get("started_at") is not None else None
        ),
        finished_at=(
            str(record["finished_at"])
            if record.get("finished_at") is not None
            else None
        ),
        error=str(record["error"]) if record.get("error") is not None else None,
        output_text=(
            str(record["assistant_text"])
            if record.get("assistant_text") is not None
            else None
        ),
    )


class PmaThreadExecutionStore(ThreadExecutionStore):
    """Adapter that hides PMA thread-store details behind orchestration nouns."""

    def __init__(self, store: PmaThreadStore) -> None:
        self._store = store

    def create_thread_target(
        self,
        agent_id: str,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
    ) -> ThreadTarget:
        created = self._store.create_thread(
            agent_id,
            workspace_root,
            repo_id=repo_id,
            name=display_name,
            backend_thread_id=backend_thread_id,
        )
        return _thread_target_from_store_row(created)

    def get_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        record = self._store.get_thread(thread_target_id)
        if record is None:
            return None
        return _thread_target_from_store_row(record)

    def list_thread_targets(
        self,
        *,
        agent_id: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        runtime_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[ThreadTarget]:
        return [
            _thread_target_from_store_row(record)
            for record in self._store.list_threads(
                agent=agent_id,
                status=lifecycle_status,
                normalized_status=runtime_status,
                repo_id=repo_id,
                limit=limit,
            )
        ]

    def resume_thread_target(
        self, thread_target_id: str, *, backend_thread_id: str
    ) -> Optional[ThreadTarget]:
        record = self._store.get_thread(thread_target_id)
        if record is None:
            return None
        self._store.set_thread_backend_id(thread_target_id, backend_thread_id)
        self._store.activate_thread(thread_target_id)
        updated = self._store.get_thread(thread_target_id)
        if updated is None:
            return None
        return _thread_target_from_store_row(updated)

    def set_thread_backend_id(
        self, thread_target_id: str, backend_thread_id: Optional[str]
    ) -> None:
        self._store.set_thread_backend_id(thread_target_id, backend_thread_id)

    def create_execution(
        self,
        thread_target_id: str,
        *,
        prompt: str,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        client_request_id: Optional[str] = None,
    ) -> ExecutionRecord:
        created = self._store.create_turn(
            thread_target_id,
            prompt=prompt,
            model=model,
            reasoning=reasoning,
            client_turn_id=client_request_id,
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

    def set_execution_backend_id(
        self, execution_id: str, backend_turn_id: Optional[str]
    ) -> None:
        self._store.set_turn_backend_turn_id(execution_id, backend_turn_id)

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
        return execution

    def record_execution_interrupted(
        self, thread_target_id: str, execution_id: str
    ) -> ExecutionRecord:
        updated = self._store.mark_turn_interrupted(execution_id)
        if not updated:
            raise KeyError(f"Execution '{execution_id}' was not running")
        execution = self.get_execution(thread_target_id, execution_id)
        if execution is None:
            raise KeyError(
                f"Execution '{execution_id}' is missing after interrupt recording"
            )
        return execution

    def record_thread_activity(
        self,
        thread_target_id: str,
        *,
        execution_id: Optional[str],
        message_preview: Optional[str],
    ) -> None:
        _ = thread_target_id, execution_id, message_preview


@dataclass
class HarnessBackedOrchestrationService(OrchestrationThreadService):
    """Canonical runtime-thread orchestration service used by PMA and later surfaces."""

    definition_catalog: AgentDefinitionCatalog
    thread_store: ThreadExecutionStore
    harness_factory: Callable[[str], RuntimeThreadHarness]

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
        limit: int = 200,
    ) -> list[ThreadTarget]:
        return self.thread_store.list_thread_targets(
            agent_id=agent_id,
            lifecycle_status=lifecycle_status,
            runtime_status=runtime_status,
            repo_id=repo_id,
            limit=limit,
        )

    def get_thread_status(self, thread_target_id: str) -> Optional[str]:
        thread = self.get_thread_target(thread_target_id)
        if thread is None:
            return None
        return thread.status

    def create_thread_target(
        self,
        agent_id: str,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
    ) -> ThreadTarget:
        if self.get_agent_definition(agent_id) is None:
            raise KeyError(f"Unknown agent definition '{agent_id}'")
        return self.thread_store.create_thread_target(
            agent_id,
            workspace_root,
            repo_id=repo_id,
            display_name=display_name,
            backend_thread_id=backend_thread_id,
        )

    def resolve_thread_target(
        self,
        *,
        thread_target_id: Optional[str],
        agent_id: str,
        workspace_root: Path,
        repo_id: Optional[str] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
    ) -> ThreadTarget:
        if thread_target_id:
            thread = self.get_thread_target(thread_target_id)
            if thread is None:
                raise KeyError(f"Unknown thread target '{thread_target_id}'")
            return thread
        return self.create_thread_target(
            agent_id,
            workspace_root,
            repo_id=repo_id,
            display_name=display_name,
            backend_thread_id=backend_thread_id,
        )

    def resume_thread_target(
        self, thread_target_id: str, *, backend_thread_id: str
    ) -> ThreadTarget:
        thread = self.thread_store.resume_thread_target(
            thread_target_id, backend_thread_id=backend_thread_id
        )
        if thread is None:
            raise KeyError(f"Unknown thread target '{thread_target_id}'")
        return thread

    async def send_message(
        self,
        request: MessageRequest,
        *,
        client_request_id: Optional[str] = None,
        sandbox_policy: Optional[Any] = None,
        harness: Optional[RuntimeThreadHarness] = None,
    ) -> ExecutionRecord:
        if request.target_kind != "thread":
            raise ValueError("Thread orchestration service only handles thread targets")

        thread = self.get_thread_target(request.target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{request.target_id}'")
        if not thread.workspace_root:
            raise RuntimeError("Thread target is missing workspace_root")

        definition = self.get_agent_definition(thread.agent_id)
        if definition is None:
            raise KeyError(f"Unknown agent definition '{thread.agent_id}'")

        harness = harness or self.harness_factory(definition.agent_id)
        workspace_root = Path(thread.workspace_root)
        await harness.ensure_ready(workspace_root)
        runtime_prompt = request.message_text
        raw_runtime_prompt = request.metadata.get("runtime_prompt")
        if isinstance(raw_runtime_prompt, str) and raw_runtime_prompt.strip():
            runtime_prompt = raw_runtime_prompt

        conversation_id = thread.backend_thread_id
        if conversation_id:
            conversation = await harness.resume_conversation(
                workspace_root, conversation_id
            )
        else:
            conversation = await harness.new_conversation(
                workspace_root,
                title=thread.display_name,
            )
            conversation_id = conversation.id
            self.thread_store.set_thread_backend_id(
                thread.thread_target_id, conversation_id
            )

        execution = self.thread_store.create_execution(
            thread.thread_target_id,
            prompt=request.message_text,
            model=request.model,
            reasoning=request.reasoning,
            client_request_id=client_request_id,
        )
        self.thread_store.record_thread_activity(
            thread.thread_target_id,
            execution_id=execution.execution_id,
            message_preview=_truncate_text(request.message_text),
        )

        try:
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
                )
        except Exception as exc:
            detail = (
                str(request.metadata.get("execution_error_message") or "").strip()
                or str(exc).strip()
                or "Runtime thread execution failed"
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
            conversation_id = resolved_conversation_id
            self.thread_store.set_thread_backend_id(
                thread.thread_target_id, conversation_id
            )
        self.thread_store.set_execution_backend_id(execution.execution_id, turn.turn_id)
        refreshed = self.get_execution(thread.thread_target_id, execution.execution_id)
        if refreshed is None:
            raise KeyError(
                f"Execution '{execution.execution_id}' is missing after creation"
            )
        return refreshed

    async def interrupt_thread(self, thread_target_id: str) -> ExecutionRecord:
        thread = self.get_thread_target(thread_target_id)
        if thread is None:
            raise KeyError(f"Unknown thread target '{thread_target_id}'")
        if not thread.workspace_root:
            raise RuntimeError("Thread target is missing workspace_root")
        if not thread.backend_thread_id:
            raise RuntimeError("Thread target has no backend thread id to interrupt")

        execution = self.get_running_execution(thread_target_id)
        if execution is None:
            raise KeyError(
                f"Thread target '{thread_target_id}' has no running execution"
            )

        harness = self.harness_factory(thread.agent_id)
        if not harness.supports("interrupt"):
            raise RuntimeError(
                f"Agent '{thread.agent_id}' does not support interrupt"
            )
        await harness.interrupt(
            Path(thread.workspace_root),
            thread.backend_thread_id,
            execution.backend_id,
        )
        return self.thread_store.record_execution_interrupted(
            thread_target_id, execution.execution_id
        )

    def get_execution(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[ExecutionRecord]:
        return self.thread_store.get_execution(thread_target_id, execution_id)

    def get_running_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        return self.thread_store.get_running_execution(thread_target_id)

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


def build_harness_backed_orchestration_service(
    *,
    descriptors: Mapping[str, RuntimeAgentDescriptor],
    harness_factory: Callable[[str], RuntimeThreadHarness],
    thread_store: Optional[ThreadExecutionStore] = None,
    pma_thread_store: Optional[PmaThreadStore] = None,
    definition_catalog: Optional[AgentDefinitionCatalog] = None,
) -> HarnessBackedOrchestrationService:
    """Build the default runtime-thread orchestration service for current PMA state."""

    if thread_store is None:
        if pma_thread_store is None:
            raise ValueError("thread_store or pma_thread_store is required")
        thread_store = PmaThreadExecutionStore(pma_thread_store)
    if definition_catalog is None:
        definition_catalog = MappingAgentDefinitionCatalog(descriptors)
    return HarnessBackedOrchestrationService(
        definition_catalog=definition_catalog,
        thread_store=thread_store,
        harness_factory=harness_factory,
    )


__all__ = [
    "HarnessBackedOrchestrationService",
    "MessagePreviewLimit",
    "PmaThreadExecutionStore",
    "build_harness_backed_orchestration_service",
]
