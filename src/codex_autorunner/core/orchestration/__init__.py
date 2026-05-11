from typing import TYPE_CHECKING

from . import execution_history_maintenance as maintenance_module
from . import migrations as migrations_module
from .bindings import (
    ActiveWorkSummary,
    OrchestrationBindingStore,
)
from .catalog import (
    MappingAgentDefinitionCatalog,
    build_agent_definition,
    get_agent_definition,
    list_agent_definitions,
    map_agent_capabilities,
    merge_agent_capabilities,
)
from .chat_operation_duplicates import (
    ChatOperationDuplicateAction,
    plan_chat_operation_duplicate,
)
from .chat_operation_ledger import (
    ChatOperationRegistration,
    SQLiteChatOperationLedger,
)
from .chat_operation_recovery import (
    ChatOperationRecoveryAction,
    plan_chat_operation_recovery,
)
from .chat_operation_scheduler_projection import (
    discord_execution_status_to_chat_operation_state,
    discord_interaction_has_pending_delivery,
    discord_scheduler_state_to_chat_operation_state,
    discord_scheduler_terminal_outcome,
)
from .chat_operation_state import (
    ChatOperationSnapshot,
    ChatOperationState,
)
from .chat_surface_events import (
    CHAT_SURFACE_EVENT_TYPES,
    ChatSurfaceEvent,
    ChatSurfaceEventAppendResult,
    ChatSurfaceEventType,
    SQLiteChatSurfaceEventJournal,
    normalize_chat_surface_event_type,
)
from .chat_surface_models import (
    ChatSurface,
    ChatSurfaceDisplayMetadata,
    ChatSurfaceExternalConversationId,
    ChatSurfaceIdentity,
    ChatSurfaceLifecycle,
    ChatSurfaceResourceOwner,
    chat_surface_identity_dict,
    normalize_chat_surface_identity,
    normalize_chat_surface_key,
    normalize_chat_surface_kind,
    normalize_chat_surface_lifecycle,
)
from .cold_trace_store import ColdTraceStore
from .execution_history_maintenance import (
    audit_execution_history,
    backfill_legacy_execution_history,
    compact_completed_execution_history,
    export_execution_history_bundle,
    resolve_execution_history_maintenance_policy,
    vacuum_execution_history,
)
from .flows import PausedFlowTarget
from .interfaces import (
    AgentDefinitionCatalog,
    FreshConversationRequiredError,
    OrchestrationFlowService,
    OrchestrationThreadService,
    RuntimeThreadHarness,
    ThreadExecutionStore,
    WorkspaceRuntimeAcquisition,
)
from .managed_thread_delivery import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryEnvelope,
    ManagedThreadDeliveryIntent,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryRecord,
    ManagedThreadDeliveryRecoveryAction,
    ManagedThreadDeliveryRecoverySweepResult,
    ManagedThreadDeliveryState,
    ManagedThreadDeliveryTarget,
    build_managed_thread_delivery_id,
    build_managed_thread_delivery_idempotency_key,
    is_valid_managed_thread_delivery_transition,
    plan_managed_thread_delivery_recovery,
    record_from_intent,
)
from .managed_thread_delivery_ledger import (
    SQLiteManagedThreadDeliveryEngine,
    SQLiteManagedThreadDeliveryLedger,
)
from .migrations import (
    ORCHESTRATION_SCHEMA_VERSION,
    apply_orchestration_migrations,
    current_orchestration_schema_version,
    list_orchestration_table_definitions,
)
from .models import (
    AgentDefinition,
    BackendBinding,
    Binding,
    ExecutionRecord,
    FlowTarget,
    MessageRequest,
    MessageRequestKind,
    Thread,
    ThreadStopOutcome,
    ThreadTarget,
)
from .sqlite import (
    ORCHESTRATION_DB_FILENAME,
    initialize_orchestration_sqlite,
    resolve_orchestration_sqlite_path,
)
from .threads import SurfaceThreadMessageRequest

if TYPE_CHECKING:
    from . import runtime_threads as runtime_threads_module
    from .service import (
        HarnessBackedOrchestrationService,
        ManagedThreadExecutionStore,
        build_harness_backed_orchestration_service,
        build_surface_orchestration_ingress,
        build_ticket_flow_orchestration_service,
    )

_LAZY_EXPORTS = {
    "runtime_threads_module": (".runtime_threads", None),
    "HarnessBackedOrchestrationService": (
        ".service",
        "HarnessBackedOrchestrationService",
    ),
    "ManagedThreadExecutionStore": (".service", "ManagedThreadExecutionStore"),
    "build_harness_backed_orchestration_service": (
        ".service",
        "build_harness_backed_orchestration_service",
    ),
    "build_surface_orchestration_ingress": (
        ".service",
        "build_surface_orchestration_ingress",
    ),
    "build_ticket_flow_orchestration_service": (
        ".service",
        "build_ticket_flow_orchestration_service",
    ),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    from importlib import import_module

    module = import_module(module_name, __name__)
    value = module if attribute_name is None else getattr(module, attribute_name)
    globals()[name] = value
    return value


__all__ = [
    "maintenance_module",
    "migrations_module",
    "runtime_threads_module",
    "ActiveWorkSummary",
    "AgentDefinition",
    "AgentDefinitionCatalog",
    "BackendBinding",
    "Binding",
    "ChatOperationDuplicateAction",
    "ChatOperationRecoveryAction",
    "ChatOperationRegistration",
    "ChatOperationSnapshot",
    "ChatOperationState",
    "ChatSurface",
    "ChatSurfaceDisplayMetadata",
    "ChatSurfaceEvent",
    "ChatSurfaceEventAppendResult",
    "ChatSurfaceEventType",
    "ChatSurfaceExternalConversationId",
    "ChatSurfaceIdentity",
    "ChatSurfaceLifecycle",
    "ChatSurfaceResourceOwner",
    "CHAT_SURFACE_EVENT_TYPES",
    "ColdTraceStore",
    "ExecutionRecord",
    "FlowTarget",
    "FreshConversationRequiredError",
    "HarnessBackedOrchestrationService",
    "ManagedThreadDeliveryAttemptResult",
    "ManagedThreadDeliveryEnvelope",
    "ManagedThreadDeliveryIntent",
    "ManagedThreadDeliveryOutcome",
    "ManagedThreadDeliveryRecord",
    "ManagedThreadDeliveryRecoveryAction",
    "ManagedThreadDeliveryRecoverySweepResult",
    "ManagedThreadDeliveryState",
    "ManagedThreadDeliveryTarget",
    "MappingAgentDefinitionCatalog",
    "MessageRequest",
    "MessageRequestKind",
    "ORCHESTRATION_DB_FILENAME",
    "ORCHESTRATION_SCHEMA_VERSION",
    "OrchestrationBindingStore",
    "OrchestrationFlowService",
    "OrchestrationThreadService",
    "PausedFlowTarget",
    "ManagedThreadExecutionStore",
    "RuntimeThreadHarness",
    "SQLiteChatOperationLedger",
    "SQLiteChatSurfaceEventJournal",
    "SQLiteManagedThreadDeliveryEngine",
    "SQLiteManagedThreadDeliveryLedger",
    "SurfaceThreadMessageRequest",
    "ThreadExecutionStore",
    "Thread",
    "ThreadStopOutcome",
    "ThreadTarget",
    "WorkspaceRuntimeAcquisition",
    "apply_orchestration_migrations",
    "audit_execution_history",
    "backfill_legacy_execution_history",
    "build_agent_definition",
    "build_harness_backed_orchestration_service",
    "build_managed_thread_delivery_id",
    "build_managed_thread_delivery_idempotency_key",
    "build_surface_orchestration_ingress",
    "build_ticket_flow_orchestration_service",
    "chat_surface_identity_dict",
    "compact_completed_execution_history",
    "current_orchestration_schema_version",
    "discord_execution_status_to_chat_operation_state",
    "discord_interaction_has_pending_delivery",
    "discord_scheduler_state_to_chat_operation_state",
    "discord_scheduler_terminal_outcome",
    "export_execution_history_bundle",
    "get_agent_definition",
    "initialize_orchestration_sqlite",
    "is_valid_managed_thread_delivery_transition",
    "list_agent_definitions",
    "list_orchestration_table_definitions",
    "map_agent_capabilities",
    "merge_agent_capabilities",
    "normalize_chat_surface_event_type",
    "normalize_chat_surface_identity",
    "normalize_chat_surface_key",
    "normalize_chat_surface_kind",
    "normalize_chat_surface_lifecycle",
    "plan_chat_operation_duplicate",
    "plan_chat_operation_recovery",
    "plan_managed_thread_delivery_recovery",
    "record_from_intent",
    "resolve_execution_history_maintenance_policy",
    "resolve_orchestration_sqlite_path",
    "vacuum_execution_history",
]
