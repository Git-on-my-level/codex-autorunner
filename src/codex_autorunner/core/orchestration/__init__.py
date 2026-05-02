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
from .chat_operation_state import (
    ChatOperationSnapshot,
    ChatOperationState,
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
    Binding,
    ExecutionRecord,
    FlowTarget,
    MessageRequest,
    MessageRequestKind,
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
        PmaThreadExecutionStore,
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
    "PmaThreadExecutionStore": (".service", "PmaThreadExecutionStore"),
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
    "Binding",
    "ChatOperationDuplicateAction",
    "ChatOperationRecoveryAction",
    "ChatOperationRegistration",
    "ChatOperationSnapshot",
    "ChatOperationState",
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
    "PmaThreadExecutionStore",
    "RuntimeThreadHarness",
    "SQLiteChatOperationLedger",
    "SQLiteManagedThreadDeliveryEngine",
    "SQLiteManagedThreadDeliveryLedger",
    "SurfaceThreadMessageRequest",
    "ThreadExecutionStore",
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
    "compact_completed_execution_history",
    "current_orchestration_schema_version",
    "export_execution_history_bundle",
    "get_agent_definition",
    "initialize_orchestration_sqlite",
    "is_valid_managed_thread_delivery_transition",
    "list_agent_definitions",
    "list_orchestration_table_definitions",
    "map_agent_capabilities",
    "merge_agent_capabilities",
    "plan_chat_operation_duplicate",
    "plan_chat_operation_recovery",
    "plan_managed_thread_delivery_recovery",
    "record_from_intent",
    "resolve_execution_history_maintenance_policy",
    "resolve_orchestration_sqlite_path",
    "vacuum_execution_history",
]
