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
from .chat_architecture_goal import (
    CHAT_ARCHITECTURE_GOAL_CRITERIA,
    CURRENT_CHAT_ARCHITECTURE_SIGNALS,
    ChatArchitectureCriterion,
    ChatArchitectureDimension,
    ChatArchitectureFinding,
    ChatArchitectureGoalEvaluation,
    ChatArchitectureSignal,
    chat_architecture_goal_summary,
    current_chat_architecture_goal_evaluation,
    evaluate_chat_architecture_goal,
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
from .chat_surface_emitters import (
    chat_surface_key,
    emit_binding_event,
    emit_chat_surface_event,
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
from .chat_surface_read_model import (
    CHAT_SURFACE_READ_CONTRACT_VERSION,
    ChatSurfaceProjection,
    ChatSurfaceReadService,
    parse_chat_surface_cursor,
    serialize_chat_surface_event,
)
from .cold_trace_store import ColdTraceStore
from .context_capsule_ledger import SQLiteContextCapsuleLedger
from .discord_interaction_lifecycle import (
    DiscordInteractionExecutionStatus,
    DiscordInteractionSchedulerState,
    is_discord_interaction_execution_terminal,
    is_discord_interaction_scheduler_terminal,
    is_valid_discord_interaction_execution_transition,
    is_valid_discord_interaction_scheduler_transition,
    normalize_discord_interaction_execution_status,
    normalize_discord_interaction_scheduler_state,
    validate_discord_interaction_execution_transition,
    validate_discord_interaction_scheduler_transition,
)
from .execution_history_maintenance import (
    audit_execution_history,
    backfill_legacy_execution_history,
    collect_orchestration_storage_maintenance_read_model,
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
    ManagedThreadFailureRecoverySummary,
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
from .managed_thread_side_effects import (
    MANAGED_THREAD_SIDE_EFFECT_ALLOWED_TRANSITIONS,
    MANAGED_THREAD_SIDE_EFFECT_TERMINAL_STATES,
    ManagedThreadSideEffectAttemptResult,
    ManagedThreadSideEffectIntent,
    ManagedThreadSideEffectOutcome,
    ManagedThreadSideEffectRecord,
    ManagedThreadSideEffectState,
    SQLiteManagedThreadSideEffectEngine,
    SQLiteManagedThreadSideEffectLedger,
    build_managed_thread_side_effect_id,
    build_managed_thread_side_effect_idempotency_key,
    is_valid_managed_thread_side_effect_transition,
)
from .migrations import (
    ORCHESTRATION_SCHEMA_VERSION,
    apply_orchestration_migrations,
    collect_orchestration_migration_status,
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
from .ticket_flow_chat_ledger_contract import (
    TICKET_FLOW_CHAT_LEDGER_CONTRACT_VERSION,
    TICKET_FLOW_FLOW_TYPE,
    TICKET_FLOW_LEDGER_LIFECYCLE,
    TICKET_FLOW_LEDGER_RECORDS,
    TICKET_FLOW_THREAD_KIND,
    TicketFlowLedgerRecord,
    TicketFlowThreadLink,
    ticket_flow_chat_ledger_contract,
    ticket_flow_thread_link_key,
    ticket_flow_thread_metadata,
    validate_ticket_flow_thread_metadata,
)
from .turn_context import ChatTurnDeliveryTarget, ChatTurnEnvelope, ChatTurnSource

if TYPE_CHECKING:
    from . import runtime_threads as runtime_threads_module
    from .flow_service import FlowBackedOrchestrationService
    from .service_factories import (
        build_harness_backed_orchestration_service,
        build_ticket_flow_orchestration_service,
    )
    from .surface_ingress import (
        SurfaceIngressResult,
        SurfaceOrchestrationIngress,
        build_surface_orchestration_ingress,
        get_surface_orchestration_ingress,
    )
    from .thread_service import HarnessBackedOrchestrationService
    from .thread_store_adapter import (
        ManagedThreadExecutionStore,
    )

_LAZY_EXPORTS = {
    "runtime_threads_module": (".runtime_threads", None),
    "FlowBackedOrchestrationService": (
        ".flow_service",
        "FlowBackedOrchestrationService",
    ),
    "HarnessBackedOrchestrationService": (
        ".thread_service",
        "HarnessBackedOrchestrationService",
    ),
    "ManagedThreadExecutionStore": (
        ".thread_store_adapter",
        "ManagedThreadExecutionStore",
    ),
    "build_harness_backed_orchestration_service": (
        ".service_factories",
        "build_harness_backed_orchestration_service",
    ),
    "build_surface_orchestration_ingress": (
        ".surface_ingress",
        "build_surface_orchestration_ingress",
    ),
    "get_surface_orchestration_ingress": (
        ".surface_ingress",
        "get_surface_orchestration_ingress",
    ),
    "SurfaceIngressResult": (".surface_ingress", "SurfaceIngressResult"),
    "SurfaceOrchestrationIngress": (
        ".surface_ingress",
        "SurfaceOrchestrationIngress",
    ),
    "build_ticket_flow_orchestration_service": (
        ".service_factories",
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
    "ChatTurnDeliveryTarget",
    "ChatTurnEnvelope",
    "ChatTurnSource",
    "ChatOperationDuplicateAction",
    "ChatOperationRecoveryAction",
    "ChatOperationRegistration",
    "ChatOperationSnapshot",
    "ChatOperationState",
    "ChatArchitectureCriterion",
    "ChatArchitectureDimension",
    "ChatArchitectureFinding",
    "ChatArchitectureGoalEvaluation",
    "ChatArchitectureSignal",
    "ChatSurface",
    "ChatSurfaceDisplayMetadata",
    "ChatSurfaceEvent",
    "ChatSurfaceEventAppendResult",
    "ChatSurfaceEventType",
    "ChatSurfaceExternalConversationId",
    "ChatSurfaceIdentity",
    "ChatSurfaceLifecycle",
    "ChatSurfaceProjection",
    "ChatSurfaceReadService",
    "ChatSurfaceResourceOwner",
    "CHAT_ARCHITECTURE_GOAL_CRITERIA",
    "CURRENT_CHAT_ARCHITECTURE_SIGNALS",
    "CHAT_SURFACE_EVENT_TYPES",
    "CHAT_SURFACE_READ_CONTRACT_VERSION",
    "ColdTraceStore",
    "DiscordInteractionExecutionStatus",
    "DiscordInteractionSchedulerState",
    "ExecutionRecord",
    "FlowBackedOrchestrationService",
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
    "ManagedThreadFailureRecoverySummary",
    "ManagedThreadSideEffectAttemptResult",
    "ManagedThreadSideEffectIntent",
    "ManagedThreadSideEffectOutcome",
    "ManagedThreadSideEffectRecord",
    "ManagedThreadSideEffectState",
    "MANAGED_THREAD_SIDE_EFFECT_ALLOWED_TRANSITIONS",
    "MANAGED_THREAD_SIDE_EFFECT_TERMINAL_STATES",
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
    "SQLiteContextCapsuleLedger",
    "SQLiteManagedThreadDeliveryEngine",
    "SQLiteManagedThreadDeliveryLedger",
    "SQLiteManagedThreadSideEffectEngine",
    "SQLiteManagedThreadSideEffectLedger",
    "is_valid_managed_thread_side_effect_transition",
    "SurfaceIngressResult",
    "SurfaceOrchestrationIngress",
    "SurfaceThreadMessageRequest",
    "TICKET_FLOW_CHAT_LEDGER_CONTRACT_VERSION",
    "TICKET_FLOW_FLOW_TYPE",
    "TICKET_FLOW_LEDGER_LIFECYCLE",
    "TICKET_FLOW_LEDGER_RECORDS",
    "TICKET_FLOW_THREAD_KIND",
    "ThreadExecutionStore",
    "Thread",
    "ThreadStopOutcome",
    "ThreadTarget",
    "TicketFlowLedgerRecord",
    "TicketFlowThreadLink",
    "WorkspaceRuntimeAcquisition",
    "apply_orchestration_migrations",
    "audit_execution_history",
    "backfill_legacy_execution_history",
    "build_agent_definition",
    "build_harness_backed_orchestration_service",
    "build_managed_thread_delivery_id",
    "build_managed_thread_delivery_idempotency_key",
    "build_managed_thread_side_effect_id",
    "build_managed_thread_side_effect_idempotency_key",
    "build_surface_orchestration_ingress",
    "build_ticket_flow_orchestration_service",
    "chat_architecture_goal_summary",
    "chat_surface_identity_dict",
    "compact_completed_execution_history",
    "current_orchestration_schema_version",
    "current_chat_architecture_goal_evaluation",
    "discord_execution_status_to_chat_operation_state",
    "discord_interaction_has_pending_delivery",
    "discord_scheduler_state_to_chat_operation_state",
    "discord_scheduler_terminal_outcome",
    "export_execution_history_bundle",
    "emit_binding_event",
    "emit_chat_surface_event",
    "evaluate_chat_architecture_goal",
    "get_agent_definition",
    "get_surface_orchestration_ingress",
    "initialize_orchestration_sqlite",
    "is_discord_interaction_execution_terminal",
    "is_discord_interaction_scheduler_terminal",
    "is_valid_discord_interaction_execution_transition",
    "is_valid_discord_interaction_scheduler_transition",
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
    "normalize_discord_interaction_execution_status",
    "normalize_discord_interaction_scheduler_state",
    "parse_chat_surface_cursor",
    "plan_chat_operation_duplicate",
    "plan_chat_operation_recovery",
    "plan_managed_thread_delivery_recovery",
    "chat_surface_key",
    "collect_orchestration_migration_status",
    "collect_orchestration_storage_maintenance_read_model",
    "record_from_intent",
    "resolve_execution_history_maintenance_policy",
    "resolve_orchestration_sqlite_path",
    "serialize_chat_surface_event",
    "ticket_flow_chat_ledger_contract",
    "ticket_flow_thread_link_key",
    "ticket_flow_thread_metadata",
    "validate_ticket_flow_thread_metadata",
    "validate_discord_interaction_execution_transition",
    "validate_discord_interaction_scheduler_transition",
    "vacuum_execution_history",
]
