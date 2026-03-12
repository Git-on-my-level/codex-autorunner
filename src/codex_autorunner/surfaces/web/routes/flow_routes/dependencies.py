from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from .....core.flows import FlowController, FlowRunRecord, FlowStore
    from .....core.flows.ux_helpers import BootstrapCheckResult


@dataclass
class FlowRouteDependencies:
    find_repo_root: Callable[[], Optional[Path]]
    require_flow_store: Callable[[Path], "FlowStore"]
    safe_list_flow_runs: Callable[..., list["FlowRunRecord"]]
    build_flow_status_response: Any
    get_flow_record: Any
    get_flow_controller: Any
    start_flow_worker: Any
    recover_flow_store_if_possible: Any
    bootstrap_check: Callable[..., "BootstrapCheckResult"]
    seed_issue: Any


def build_default_flow_route_dependencies() -> FlowRouteDependencies:
    from .....core.flows.ux_helpers import bootstrap_check as ux_bootstrap_check
    from .....core.flows.ux_helpers import (
        build_flow_status_snapshot,
        seed_issue_from_github,
        seed_issue_from_text,
    )
    from .....core.utils import find_repo_root
    from .definitions import get_flow_record
    from .run_routes import start_flow_worker as _start_flow_worker
    from .runtime_service import (
        recover_flow_store_if_possible as _recover_flow_store_if_possible,
    )
    from .ticket_bootstrap import safe_list_flow_runs

    def require_flow_store(repo_root: Path) -> "FlowStore":
        from .....core.flows import FlowStore
        from .runtime_service import flow_paths

        db_path, _ = flow_paths(repo_root)
        store = FlowStore(db_path)
        store.initialize()
        return store

    def get_flow_controller(
        repo_root: Path, flow_type: str, state: Any
    ) -> "FlowController":
        from .....core.flows import FlowController
        from .....flows.ticket_flow import build_ticket_flow_definition

        key = (repo_root.resolve(), flow_type)
        with state.lock:
            controller = state.controller_cache.get(key)
            if controller:
                return controller
        definition = build_ticket_flow_definition(flow_type)
        controller = FlowController(repo_root, definition)
        with state.lock:
            state.controller_cache[key] = controller
        return controller

    def seed_issueispatch(
        repo_root: Path,
        run_id: str,
        issue_url: Optional[str] = None,
        issue_text: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if issue_url:
            return seed_issue_from_github(repo_root, run_id, issue_url)
        return seed_issue_from_text(repo_root, run_id, issue_text or "", **kwargs)

    return FlowRouteDependencies(
        find_repo_root=find_repo_root,
        require_flow_store=require_flow_store,
        safe_list_flow_runs=safe_list_flow_runs,
        build_flow_status_response=build_flow_status_snapshot,
        get_flow_record=get_flow_record,
        get_flow_controller=get_flow_controller,
        start_flow_worker=_start_flow_worker,
        recover_flow_store_if_possible=_recover_flow_store_if_possible,
        bootstrap_check=ux_bootstrap_check,
        seed_issue=seed_issueispatch,
    )
