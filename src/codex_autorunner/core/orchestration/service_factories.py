from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

from ..managed_thread_store import ManagedThreadStore
from .bindings import OrchestrationBindingStore
from .catalog import MappingAgentDefinitionCatalog, RuntimeAgentDescriptor
from .flow_service import FlowBackedOrchestrationService
from .flows import build_ticket_flow_target_wrapper
from .interfaces import (
    AgentDefinitionCatalog,
    ThreadExecutionStore,
)
from .thread_service import HarnessBackedOrchestrationService, HarnessFactory
from .thread_store_adapter import ManagedThreadExecutionStore


def build_harness_backed_orchestration_service(
    *,
    descriptors: Mapping[str, RuntimeAgentDescriptor],
    harness_factory: HarnessFactory,
    thread_store: Optional[ThreadExecutionStore] = None,
    managed_thread_store: Optional[ManagedThreadStore] = None,
    definition_catalog: Optional[AgentDefinitionCatalog] = None,
    binding_store: Optional[OrchestrationBindingStore] = None,
) -> HarnessBackedOrchestrationService:
    """Build the default runtime-thread orchestration service for current PMA state."""

    if thread_store is None:
        if managed_thread_store is None:
            raise ValueError("thread_store or managed_thread_store is required")
        thread_store = ManagedThreadExecutionStore(managed_thread_store)
    if definition_catalog is None:
        definition_catalog = MappingAgentDefinitionCatalog(descriptors)
    if binding_store is None and managed_thread_store is not None:
        hub_root = getattr(managed_thread_store, "_hub_root", None)
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
