from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from ....core.flows import FlowController, FlowDefinition

_logger = logging.getLogger(__name__)


def build_flow_definition(
    repo_root: Path, flow_type: str, state: "FlowRoutesState"
) -> FlowDefinition:
    from . import FlowRoutesState

    repo_root = repo_root.resolve()
    key = (repo_root, flow_type)
    with state.lock:
        if key in state.definition_cache:
            return state.definition_cache[key]

    from ....core.config import load_repo_config
    from ....core.runtime import RuntimeContext
    from ....flows.ticket_flow import build_ticket_flow_definition
    from ....integrations.agents.build_agent_pool import build_agent_pool

    if flow_type == "ticket_flow":
        config = load_repo_config(repo_root)
        engine = RuntimeContext(
            repo_root=repo_root,
            config=config,
        )
        agent_pool = build_agent_pool(engine.config)
        definition = build_ticket_flow_definition(agent_pool=agent_pool)
    else:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"Unknown flow type: {flow_type}")

    with state.lock:
        state.definition_cache[key] = definition
    return definition


def get_flow_controller(
    repo_root: Path, flow_type: str, state: "FlowRoutesState"
) -> FlowController:
    repo_root = repo_root.resolve()
    key = (repo_root, flow_type)
    with state.lock:
        if key in state.controller_cache:
            controller = state.controller_cache[key]
            try:
                controller.initialize()
            except Exception:
                pass
            return controller

    definition = build_flow_definition(repo_root, flow_type, state)
    controller = definition.build_controller(repo_root)
    with state.lock:
        state.controller_cache[key] = controller
    return controller


def get_flow_record(
    repo_root: Path, run_id: str, state: "FlowRoutesState"
) -> Optional[Dict[str, Any]]:
    from .runtime_service import recover_flow_store_if_possible
    from ...services import flow_store as flow_store_service

    try:
        store = flow_store_service.require_flow_store(repo_root, logger=_logger)
        if store is None:
            return None
        record = store.get_run(run_id)
        if record is None:
            return None
        return {
            "id": record.id,
            "flow_type": record.flow_type,
            "status": record.status.value
            if hasattr(record.status, "value")
            else str(record.status),
            "current_ticket_index": record.current_ticket_index,
            "current_ticket_path": str(record.current_ticket_path)
            if record.current_ticket_path
            else None,
            "created_at": record.created_at.isoformat()
            if hasattr(record.created_at, "isoformat")
            else str(record.created_at),
            "updated_at": record.updated_at.isoformat()
            if hasattr(record.updated_at, "isoformat")
            else str(record.updated_at),
        }
    except Exception as exc:
        recovered = recover_flow_store_if_possible(repo_root, "ticket_flow", state, exc)
        if not recovered:
            return None
        try:
            store = flow_store_service.require_flow_store(repo_root, logger=_logger)
            if store is None:
                return None
            record = store.get_run(run_id)
            if record is None:
                return None
            return {
                "id": record.id,
                "flow_type": record.flow_type,
                "status": record.status.value
                if hasattr(record.status, "value")
                else str(record.status),
                "current_ticket_index": record.current_ticket_index,
                "current_ticket_path": str(record.current_ticket_path)
                if record.current_ticket_path
                else None,
                "created_at": record.created_at.isoformat()
                if hasattr(record.created_at, "isoformat")
                else str(record.created_at),
                "updated_at": record.updated_at.isoformat()
                if hasattr(record.updated_at, "isoformat")
                else str(record.updated_at),
            }
        except Exception:
            return None
