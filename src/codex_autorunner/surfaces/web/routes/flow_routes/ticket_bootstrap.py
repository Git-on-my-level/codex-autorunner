from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

if TYPE_CHECKING:
    from . import FlowRouteDependencies, FlowRoutesState

_logger = logging.getLogger(__name__)


def require_flow_store(repo_root: Path):
    from ...services import flow_store as flow_store_service

    return flow_store_service.require_flow_store(repo_root, logger=_logger)


def safe_list_flow_runs(
    repo_root: Path, flow_type: Optional[str] = None, *, recover_stuck: bool = False
):
    from ...services import flow_store as flow_store_service

    return flow_store_service.safe_list_flow_runs(
        repo_root,
        flow_type=flow_type,
        recover_stuck=recover_stuck,
        logger=_logger,
    )


def start_flow_worker(
    repo_root: Path,
    flow_type: str,
    force_new: bool,
    state: "FlowRoutesState",
):
    from .runtime_service import evict_cached_controller

    key = (repo_root.resolve(), flow_type)
    with state.lock:
        if not force_new and key in state.active_workers:
            worker_handle = state.active_workers[key]
            if worker_handle is not None:
                proc, _, _ = worker_handle
                if proc is not None and proc.poll() is None:
                    return worker_handle
        old_handle = state.active_workers.pop(key, None)
        if old_handle is not None:
            try:
                proc, _, _ = old_handle
                if proc is not None:
                    proc.terminate()
            except Exception:
                pass

    evict_cached_controller(repo_root, flow_type, state)

    from .run_routes import start_flow_worker as start_worker_impl

    return start_worker_impl(repo_root, flow_type, state)


def bootstrap_check(
    repo_root: Path,
    state: "FlowRoutesState",
) -> Dict[str, Any]:
    from .....core.flows.ux_helpers import bootstrap_check as ux_bootstrap_check

    return ux_bootstrap_check(
        repo_root,
        github_service_factory=None,
    )


def seed_issue(
    repo_root: Path,
    state: "FlowRoutesState",
    body: Dict[str, Any],
) -> Dict[str, Any]:
    from fastapi import HTTPException

    from .....core.flows.ux_helpers import seed_issue_from_github, seed_issue_from_text

    source = (body.get("source") or "text").strip().lower()
    if source not in ("text", "github"):
        raise HTTPException(status_code=400, detail="invalid source")

    if source == "github":
        gh_issue = body.get("github_issue")
        if not gh_issue:
            raise HTTPException(status_code=400, detail="github_issue is required")
        issue_url = (gh_issue.get("url") or "").strip()
        issue_number = gh_issue.get("number")
        if not issue_url and not issue_number:
            raise HTTPException(
                status_code=400, detail="github_issue.url or number required"
            )
        return seed_issue_from_github(repo_root, issue_url, issue_number)

    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    body_text = body.get("body") or ""
    labels = body.get("labels") or []
    assignees = body.get("assignees") or []
    return seed_issue_from_text(repo_root, title, body_text, labels, assignees)


def run_bootstrap(
    repo_root: Path,
    flow_type: str,
    state: "FlowRoutesState",
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from .runtime_service import recover_flow_store_if_possible

    body = body or {}
    force_new = bool(body.get("force_new", False))

    try:
        store = require_flow_store(repo_root)
        if store is None:
            raise RuntimeError("Flow store unavailable")
        runs = store.list_runs(flow_type=flow_type)
    except Exception as exc:
        recovered = recover_flow_store_if_possible(repo_root, flow_type, state, exc)
        if not recovered:
            raise
        store = require_flow_store(repo_root)
        if store is None:
            raise RuntimeError("Flow store unavailable after recovery") from exc
        runs = store.list_runs(flow_type=flow_type)

    active_runs = [r for r in runs if r.status.value == "active"]
    paused_runs = [r for r in runs if r.status.value == "paused"]

    if active_runs:
        if force_new:
            pass
        else:
            run = active_runs[0]
            return {
                "status": "ok",
                "run_id": run.id,
                "action": "resumed",
                "current_ticket_index": run.current_ticket_index,
            }

    if paused_runs:
        if force_new:
            pass
        else:
            run = paused_runs[0]
            controller = get_flow_controller(repo_root, flow_type, state)
            controller.resume(run.id)
            return {
                "status": "ok",
                "run_id": run.id,
                "action": "resumed",
                "current_ticket_index": run.current_ticket_index,
            }

    worker_handle = start_flow_worker(repo_root, flow_type, force_new, state)
    with state.lock:
        state.active_workers[(repo_root.resolve(), flow_type)] = worker_handle

    if worker_handle is None:
        raise RuntimeError("Failed to start flow worker")

    proc, run_id, _ = worker_handle
    if proc is None or run_id is None:
        raise RuntimeError("Flow worker handle invalid")

    return {
        "status": "ok",
        "run_id": run_id,
        "action": "started",
    }


def get_flow_controller(repo_root: Path, flow_type: str, state: "FlowRoutesState"):
    from .definitions import get_flow_controller as impl

    return impl(repo_root, flow_type, state)


def build_ticket_bootstrap_routes(
    deps: "FlowRouteDependencies",
) -> tuple[APIRouter, list[str]]:
    router = APIRouter(prefix="/api/flows", tags=["flows"])

    def _ensure_state_in_app(request: Request) -> "FlowRoutesState":
        from typing import cast

        if not hasattr(request.app.state, "flow_routes_state"):
            from . import FlowRoutesState

            request.app.state.flow_routes_state = FlowRoutesState()
        return cast("FlowRoutesState", request.app.state.flow_routes_state)

    @router.get("/ticket_flow/bootstrap-check")
    async def bootstrap_check():
        """
        Determine whether ISSUE.md already exists and whether GitHub is available
        for fetching an issue before bootstrapping the ticket flow.
        """
        repo_root = deps.find_repo_root()
        if repo_root is None:
            return {"status": "error", "github_available": False, "repo": None}

        result = deps.bootstrap_check(repo_root, github_service_factory=None)
        if result.status == "ready":
            return {"status": "ready"}
        return {
            "status": result.status,
            "github_available": result.github_available,
            "repo": result.repo_slug,
        }

    @router.post("/ticket_flow/bootstrap")
    async def bootstrap_ticket_flow(
        http_request: Request,
    ):
        _ensure_state_in_app(http_request)
        repo_root = deps.find_repo_root()
        if repo_root is None:
            raise HTTPException(status_code=400, detail="Repository not found")

        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        from .....tickets.files import list_ticket_paths

        existing_tickets = list_ticket_paths(ticket_dir)
        tickets_exist = bool(existing_tickets)

        if not tickets_exist and not ticket_path.exists():
            bootstrap_ticket_id = f"tkt_{uuid.uuid4().hex}"
            template = f"""---
agent: codex
done: false
ticket_id: "{bootstrap_ticket_id}"
title: Bootstrap ticket plan
goal: Capture scope and seed follow-up tickets
---

You are the first ticket in a new ticket_flow run.

- Read `.codex-autorunner/ISSUE.md`. If it is missing:
  - If GitHub is available, ask the user for the issue/PR URL or number and create `.codex-autorunner/ISSUE.md` from it.
  - If GitHub is not available, write `DISPATCH.md` with `mode: pause` asking the user to describe the work (or share a doc). After the reply, create `.codex-autorunner/ISSUE.md` with their input.
- If helpful, create or update contextspace docs under `.codex-autorunner/contextspace/`:
  - `active_context.md` for current context and links
  - `decisions.md` for decisions/rationale
  - `spec.md` for requirements and constraints
- Break the work into additional `TICKET-00X.md` files with clear owners/goals; keep this ticket open until they exist.
- Place any supporting artifacts in `.codex-autorunner/runs/<run_id>/dispatch/` if needed.
- Write `DISPATCH.md` to dispatch a message to the user:
  - Use `mode: pause` (handoff) to wait for user response. This pauses execution.
  - Use `mode: notify` (informational) to message the user but keep running.
"""
            ticket_path.write_text(template, encoding="utf-8")

        records = deps.safe_list_flow_runs(
            repo_root, flow_type="ticket_flow", recover_stuck=True
        )
        active = None
        for record in records:
            if record.status.value in ("running", "active"):
                active = record
                break

        if active:
            record = active
            return deps.build_flow_status_response(record, repo_root, store=None)

        raise HTTPException(
            status_code=400,
            detail="Bootstrap not fully implemented in extraction slice - use flows.py for now",
        )

    return router, ["ticket_bootstrap"]


_TICKET_BOOTSTRAP_ROUTE_API = run_bootstrap
