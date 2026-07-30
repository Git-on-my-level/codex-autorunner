from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, cast

if TYPE_CHECKING:
    from .....core.flows import FlowController, FlowDefinition, FlowRunRecord
    from .....flows.controller_provider import FlowControllerProvider
    from . import FlowRoutesState

_logger = logging.getLogger(__name__)


def _flow_run_record_payload(record: "FlowRunRecord") -> dict[str, Any]:
    return {
        "id": record.id,
        "flow_type": record.flow_type,
        "status": (
            record.status.value
            if hasattr(record.status, "value")
            else str(record.status)
        ),
        "input_data": dict(record.input_data or {}),
        "state": dict(record.state or {}),
        "current_step": record.current_step,
        "stop_requested": record.stop_requested,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "error_message": record.error_message,
        "metadata": dict(record.metadata or {}),
    }


def build_flow_definition(
    repo_root: Path, flow_type: str, state: "FlowRoutesState"
) -> FlowDefinition:
    from .....core.flows import FlowDefinition

    repo_root = repo_root.resolve()
    key = (repo_root, flow_type)
    with state.lock:
        cached_definition = cast(
            Optional[FlowDefinition], state.definition_cache.get(key)
        )
        if cached_definition is not None:
            return cached_definition

    from .....adapters.agents.build_agent_pool import build_agent_pool
    from .....core.config import load_repo_config
    from .....core.runtime import RuntimeContext
    from .....core.state import load_state
    from .....flows.ticket_flow import build_ticket_flow_definition
    from .....tickets import DEFAULT_MAX_TOTAL_TURNS

    if flow_type == "ticket_flow":
        config = load_repo_config(repo_root)
        engine = RuntimeContext(
            repo_root=repo_root,
            config=config,
        )
        agent_pool = build_agent_pool(engine.config)
        definition = build_ticket_flow_definition(
            agent_pool=agent_pool,
            auto_commit_default=engine.config.git_auto_commit,
            require_commit_default=load_state(
                engine.state_path
            ).ticket_flow_require_commit,
            include_previous_ticket_context_default=(
                engine.config.ticket_flow.include_previous_ticket_context
            ),
            max_total_turns_default=(
                engine.config.ticket_flow.max_total_turns
                if engine.config.ticket_flow.max_total_turns is not None
                else DEFAULT_MAX_TOTAL_TURNS
            ),
        )
    else:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"Unknown flow type: {flow_type}")

    # Validate before caching. This module and routes/flows.py used to build
    # definitions separately and only the latter validated, so which of the two
    # lazily-constructed providers won a race decided whether definitions were
    # validated at all. There is now one builder, and it always validates.
    definition.validate()
    with state.lock:
        state.definition_cache[key] = definition
    return definition


def controller_provider_for(state: "FlowRoutesState") -> "FlowControllerProvider":
    """Return this app's controller provider, bound to the shared state cache.

    Built on first use because the definition/path factories live here rather
    than on FlowRoutesState. Racing constructions are harmless: every provider
    shares the same cache mapping and lock.
    """
    from .....flows.controller_provider import FlowControllerProvider
    from ...services import flow_store as flow_store_service

    existing = state.controller_provider
    if isinstance(existing, FlowControllerProvider):
        return existing

    built = FlowControllerProvider(
        definition_factory=lambda root, ft: build_flow_definition(root, ft, state),
        paths_factory=flow_store_service.flow_paths,
        cache=state.controller_cache,
        lock=state.lock,
    )
    with state.lock:
        current = state.controller_provider
        if isinstance(current, FlowControllerProvider):
            return current
        state.controller_provider = built
    return built


def get_flow_controller(
    repo_root: Path, flow_type: str, state: "FlowRoutesState"
) -> FlowController:
    """Resolve an initialized controller through the shared provider.

    Construction, caching, and corrupt-store recovery live in
    ``flows.controller_provider``; this is a lookup, not an owner.
    """
    return controller_provider_for(state).get(repo_root, flow_type)


def get_flow_record(
    repo_root: Path, run_id: str, state: "FlowRoutesState"
) -> Optional[Dict[str, Any]]:
    from ...services import flow_store as flow_store_service
    from .runtime_service import recover_flow_store_if_possible

    try:
        store = flow_store_service.require_flow_store(repo_root, logger=_logger)
        if store is None:
            return None
        record = store.get_flow_run(run_id)
        if record is None:
            return None
        return _flow_run_record_payload(record)
    except (
        Exception
    ) as exc:  # intentional: store access may fail in many ways, triggers recovery
        recovered = recover_flow_store_if_possible(repo_root, "ticket_flow", state, exc)
        if not recovered:
            return None
        try:
            store = flow_store_service.require_flow_store(repo_root, logger=_logger)
            if store is None:
                return None
            record = store.get_flow_run(run_id)
            if record is None:
                return None
            return _flow_run_record_payload(record)
        except Exception:  # intentional: post-recovery attempt, silent fallback
            return None
