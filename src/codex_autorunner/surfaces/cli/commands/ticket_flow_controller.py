"""Ticket-flow CLI controller decisions."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, cast

from ....core.flows import FlowStore
from ....core.orchestration.models import FlowRunTarget
from ....core.runtime import RuntimeContext
from ....core.state_roots import resolve_repo_flows_db_path, resolve_repo_state_root
from ....core.ticket_flow_operator import (
    PreflightReport,
    RunReuseResult,
    resolve_run_reuse_policy,
)
from ....flows.ticket_flow.runtime_helpers import (
    ensure_ticket_flow_worker,
    flow_run_record_from_target,
    seed_bootstrap_ticket_if_needed,
    stop_ticket_flow_worker,
)


@dataclass(frozen=True)
class TicketFlowPaths:
    db_path: Path
    artifacts_root: Path
    ticket_dir: Path


@dataclass(frozen=True)
class TicketFlowRunAction:
    message: str
    exit_message: Optional[str] = None
    preflight_report: Optional[PreflightReport] = None

    @property
    def should_exit(self) -> bool:
        return self.exit_message is not None


class TicketFlowCliController:
    def __init__(self, engine: RuntimeContext, service: Any) -> None:
        self.engine = engine
        self.service = service
        self.paths = resolve_ticket_flow_paths(engine)

    def open_flow_store(self) -> FlowStore:
        store = FlowStore(self.paths.db_path, durable=self.engine.config.durable_writes)
        store.initialize()
        return store

    def bootstrap(
        self,
        *,
        force_new: bool,
        max_total_turns: Optional[int],
    ) -> TicketFlowRunAction:
        self.paths.ticket_dir.mkdir(parents=True, exist_ok=True)
        reuse = self._resolve_reuse(force_new=force_new)
        terminal_action = self._terminal_reuse_action(reuse)
        if terminal_action is not None:
            return terminal_action
        warning = self._stale_terminal_warning(reuse)

        seeded = seed_bootstrap_ticket_if_needed(self.paths.ticket_dir)
        run_id = str(uuid.uuid4())
        input_data: dict[str, object] = {}
        if max_total_turns is not None:
            input_data["max_total_turns"] = max_total_turns
        asyncio.run(
            self.service.start_flow_run(
                "ticket_flow",
                input_data=input_data,
                run_id=run_id,
                metadata={"seeded_ticket": seeded},
            )
        )
        message = f"run={run_id} | car ticket-flow status --run-id {run_id}"
        if warning is not None:
            message = f"{warning}\n{message}"
        return TicketFlowRunAction(message=message)

    def start(
        self,
        *,
        force_new: bool,
        max_total_turns: Optional[int],
        preflight: Callable[[], PreflightReport],
    ) -> TicketFlowRunAction:
        self.paths.ticket_dir.mkdir(parents=True, exist_ok=True)
        reuse = self._resolve_reuse(force_new=force_new)
        terminal_action = self._terminal_reuse_action(
            reuse, require_preflight_for_active=True, preflight=preflight
        )
        if terminal_action is not None:
            return terminal_action
        warning = self._stale_terminal_warning(reuse)

        report = preflight()
        if report.has_errors():
            return TicketFlowRunAction(
                message="Ticket flow preflight failed:",
                exit_message="Fix the above errors before starting the ticket flow.",
                preflight_report=report,
            )

        run_id = str(uuid.uuid4())
        input_data: dict[str, object] = {"workspace_root": str(self.engine.repo_root)}
        if max_total_turns is not None:
            input_data["max_total_turns"] = max_total_turns
        asyncio.run(
            self.service.start_flow_run(
                "ticket_flow",
                input_data=input_data,
                run_id=run_id,
            )
        )
        message = f"run={run_id} | car ticket-flow status --run-id {run_id}"
        if warning is not None:
            message = f"{warning}\n{message}"
        return TicketFlowRunAction(message=message)

    def resolve_target_run(self, run_id: Optional[str]) -> Optional[FlowRunTarget]:
        if run_id:
            return cast(Optional[FlowRunTarget], self.service.get_flow_run(run_id))
        runs = self.service.list_flow_runs()
        return runs[0] if runs else None

    def stop(self, run_id: Optional[str]) -> Optional[FlowRunTarget]:
        target = self.resolve_target_run(run_id)
        if target is None:
            return None
        stop_ticket_flow_worker(self.engine.repo_root, target.run_id)
        return cast(
            Optional[FlowRunTarget],
            asyncio.run(self.service.stop_flow_run(target.run_id)),
        )

    def retire(
        self,
        *,
        run_id: Optional[str],
        force: bool,
        force_attestation: Optional[str],
        delete_run: bool,
        vacuum: bool,
        dry_run: bool,
        archive_flow_run_artifacts: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        if not run_id:
            raise ValueError("--run-id is required")
        store = self.open_flow_store()
        try:
            record = store.get_flow_run(run_id)
            if record is None:
                raise ValueError(f"Flow run not found: {run_id}")
            archive_kwargs: dict[str, Any] = {
                "repo_root": self.engine.repo_root,
                "store": store,
                "record": record,
                "force": force,
                "delete_run": delete_run,
                "vacuum": vacuum,
                "dry_run": dry_run,
            }
            if force:
                archive_kwargs["force_attestation"] = build_force_attestation_payload(
                    force_attestation,
                    target_scope=f"flow.ticket_flow.retire:{run_id}",
                )
            return archive_flow_run_artifacts(**archive_kwargs)
        finally:
            store.close()

    def _resolve_reuse(self, *, force_new: bool) -> RunReuseResult:
        records = [
            flow_run_record_from_target(target)
            for target in self.service.list_flow_runs()
        ]
        return resolve_run_reuse_policy(
            records, force_new=force_new, ticket_dir=self.paths.ticket_dir
        )

    def _terminal_reuse_action(
        self,
        reuse: RunReuseResult,
        *,
        require_preflight_for_active: bool = False,
        preflight: Optional[Callable[[], PreflightReport]] = None,
    ) -> Optional[TicketFlowRunAction]:
        if reuse.action == "reuse_active":
            assert reuse.run is not None
            if require_preflight_for_active:
                assert preflight is not None
                report = preflight()
                if report.has_errors():
                    return TicketFlowRunAction(
                        message="Ticket flow preflight failed:",
                        exit_message=(
                            "Fix the above errors before starting the ticket flow."
                        ),
                        preflight_report=report,
                    )
            ensure_ticket_flow_worker(
                self.engine.repo_root, reuse.run.id, is_terminal=False
            )
            return TicketFlowRunAction(
                message=(
                    f"reused run={reuse.run.id} | "
                    f"car ticket-flow status --run-id {reuse.run.id}"
                )
            )
        if reuse.action == "completed_pending" and reuse.pending_ticket_count > 0:
            assert reuse.run is not None
            return TicketFlowRunAction(
                message=(
                    f"run {reuse.run.id} completed with "
                    f"{reuse.pending_ticket_count} pending tickets. "
                    f"use --force-new to reset dispatch history."
                ),
                exit_message="Add --force-new to create a new run.",
            )
        return None

    def _stale_terminal_warning(self, reuse: RunReuseResult) -> Optional[str]:
        if reuse.stale_terminal_runs:
            stale_id = reuse.stale_terminal_runs[0].id
            return (
                f"warning: {len(reuse.stale_terminal_runs)} stale runs "
                f"(FAILED/STOPPED). inspect: car ticket-flow status "
                f"--run-id {stale_id}. retire: car ticket-flow retire "
                f"--run-id {stale_id} --force. use --force-new to suppress."
            )
        return None


def normalize_flow_run_id(run_id: Optional[str]) -> Optional[str]:
    if run_id is None:
        return None
    return str(uuid.UUID(str(run_id)))


def resolve_ticket_flow_paths(engine: RuntimeContext) -> TicketFlowPaths:
    state_root = resolve_repo_state_root(engine.repo_root)
    return TicketFlowPaths(
        db_path=resolve_repo_flows_db_path(engine.repo_root),
        artifacts_root=state_root / "flows",
        ticket_dir=state_root / "tickets",
    )


def build_force_attestation_payload(
    force_attestation: Optional[str], *, target_scope: str
) -> Optional[dict[str, str]]:
    if force_attestation is None:
        return None
    from ....core.force_attestation import FORCE_ATTESTATION_REQUIRED_PHRASE

    return {
        "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
        "user_request": force_attestation,
        "target_scope": target_scope,
    }
