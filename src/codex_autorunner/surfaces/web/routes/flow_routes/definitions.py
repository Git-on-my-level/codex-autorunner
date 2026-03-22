from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, cast

if TYPE_CHECKING:
    from ....core.flows import FlowController, FlowDefinition, FlowRunRecord
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
    from ....core.flows import FlowDefinition

    repo_root = repo_root.resolve()
    key = (repo_root, flow_type)
    with state.lock:
        cached_definition = cast(
            Optional[FlowDefinition], state.definition_cache.get(key)
        )
        if cached_definition is not None:
            return cached_definition

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
        definition = build_ticket_flow_definition(
            agent_pool=agent_pool,
            auto_commit_default=engine.config.git_auto_commit,
            include_previous_ticket_context_default=(
                engine.config.ticket_flow.include_previous_ticket_context
            ),
        )
    else:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"Unknown flow type: {flow_type}")

    with state.lock:
        state.definition_cache[key] = definition
    return definition


def get_flow_controller(
    repo_root: Path, flow_type: str, state: "FlowRoutesState"
) -> FlowController:
    from ....core.flows import FlowController
    from ...services import flow_store as flow_store_service

    repo_root = repo_root.resolve()
    key = (repo_root, flow_type)
    with state.lock:
        controller = cast(Optional[FlowController], state.controller_cache.get(key))
        if controller is not None:
            try:
                controller.initialize()
            except Exception:
                pass
            return controller

    definition = build_flow_definition(repo_root, flow_type, state)
    db_path, artifacts_root = flow_store_service.flow_paths(repo_root)
    controller = FlowController(
        definition=definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
    )
    controller.initialize()
    with state.lock:
        state.controller_cache[key] = controller
    return controller


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
    except Exception as exc:
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
        except Exception:
            return None
