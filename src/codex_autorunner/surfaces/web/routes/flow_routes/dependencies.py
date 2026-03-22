from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .....core.flows import FlowRunRecord, FlowStore
    from .....core.flows.controller import FlowController
    from .....core.flows.ux_helpers import BootstrapCheckResult


@dataclass
class FlowRouteDependencies:
    find_repo_root: Callable[[], Optional[Path]]
    build_flow_orchestration_service: Any
    require_flow_store: Callable[[Path], Optional["FlowStore"]]
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
    from ...services import flow_store as _flow_store_service
    from ...services.flow_store import get_flow_record
    from .definitions import get_flow_controller
    from .runtime_service import (
        build_flow_orchestration_service as _build_flow_orchestration_service,
    )
    from .runtime_service import (
        recover_flow_store_if_possible as _recover_flow_store_if_possible,
    )

    def require_flow_store(repo_root: Path) -> "FlowStore":
        from .....core.flows import FlowStore
        from .runtime_service import flow_paths

        db_path, _ = flow_paths(repo_root)
        store = FlowStore(db_path)
        store.initialize()
        return store

    def get_flow_controller_adapter(
        repo_root: Path, flow_type: str, state: Any
    ) -> "FlowController":
        from .....core.flows.controller import FlowController as _FlowController

        controller: _FlowController = get_flow_controller(repo_root, flow_type, state)
        return controller

    def safe_list_flow_runs_wrapper(
        repo_root: Path,
        flow_type: Optional[str] = None,
        *,
        recover_stuck: bool = False,
    ):
        return _flow_store_service.safe_list_flow_runs(
            repo_root,
            flow_type=flow_type,
            recover_stuck=recover_stuck,
            logger=_logger,
        )

    def seed_issue_dispatch(
        repo_root: Path,
        run_id: str,
        issue_url: Optional[str] = None,
        issue_text: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        github_service_factory = kwargs.get("github_service_factory")
        if issue_url:
            return seed_issue_from_github(
                repo_root,
                issue_url,
                github_service_factory=github_service_factory,
            )
        return seed_issue_from_text(issue_text or "")

    return FlowRouteDependencies(
        find_repo_root=find_repo_root,
        build_flow_orchestration_service=_build_flow_orchestration_service,
        require_flow_store=require_flow_store,
        safe_list_flow_runs=safe_list_flow_runs_wrapper,
        build_flow_status_response=build_flow_status_snapshot,
        get_flow_record=get_flow_record,
        get_flow_controller=get_flow_controller_adapter,
        start_flow_worker=None,
        recover_flow_store_if_possible=_recover_flow_store_if_possible,
        bootstrap_check=ux_bootstrap_check,
        seed_issue=seed_issue_dispatch,
    )
