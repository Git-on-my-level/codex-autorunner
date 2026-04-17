"""Inbox endpoints for agent dispatches and human replies.

These endpoints provide a thin wrapper over the durable on-disk ticket_flow
dispatch history (agent -> human) and reply history (human -> agent).

Domain terminology:
- Dispatch: Agent-to-human communication (mode: "notify" for FYI, "pause" for handoff)
- Reply: Human-to-agent response
- Handoff: A dispatch with mode="pause" that requires human action

The UI contract is intentionally filesystem-backed:
* Dispatches come from `.codex-autorunner/runs/<run_id>/dispatch_history/<seq>/`.
* Human replies are written to USER_REPLY.md + reply/* and immediately archived
  into `.codex-autorunner/runs/<run_id>/reply_history/<seq>/`.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from ....core.config import ConfigError, load_repo_config
from ....core.filebox import ensure_structure, save_file
from ....core.flows.failure_diagnostics import (
    format_failure_summary,
    get_failure_payload,
)
from ....core.flows.models import FlowRunStatus
from ....core.flows.store import FlowStore
from ....core.flows.workspace_root import resolve_ticket_flow_workspace_root
from ....core.utils import find_repo_root
from ....tickets.files import safe_relpath
from ....tickets.outbox import parse_dispatch, resolve_outbox_paths
from ....tickets.replies import (
    dispatch_reply,
    ensure_reply_dirs,
    next_reply_seq,
    parse_user_reply,
    resolve_reply_paths,
)
from .messages_helpers import (
    collect_entry_files,
    iter_seq_dirs,
    safe_attachment_name,
    ticket_state_snapshot,
)
from .messages_helpers import (
    timestamp as _timestamp,
)

_logger = logging.getLogger(__name__)


def _flows_db_path(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "flows.db"


def _resolve_workspace_root(record_input: dict[str, Any], repo_root: Path) -> Path:
    try:
        return resolve_ticket_flow_workspace_root(record_input, repo_root)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _get_durable_writes(repo_root: Path) -> bool:
    try:
        return load_repo_config(repo_root).durable_writes
    except ConfigError:
        return False


def _collect_dispatch_history(
    *, repo_root: Path, run_id: str, record_input: dict[str, Any]
) -> list[dict[str, Any]]:
    workspace_root = _resolve_workspace_root(record_input, repo_root)
    outbox_paths = resolve_outbox_paths(workspace_root=workspace_root, run_id=run_id)
    history: list[dict[str, Any]] = []
    for seq, entry_dir in reversed(iter_seq_dirs(outbox_paths.dispatch_history_dir)):
        dispatch_path = entry_dir / "DISPATCH.md"
        dispatch, errors = parse_dispatch(dispatch_path)
        files = collect_entry_files(
            entry_dir,
            skip_name="DISPATCH.md",
            url_prefix=f"api/flows/{run_id}/dispatch_history/{seq:04d}",
        )
        created_at = _timestamp(dispatch_path) or _timestamp(entry_dir)
        history.append(
            {
                "seq": seq,
                "dir": safe_relpath(entry_dir, workspace_root),
                "created_at": created_at,
                "dispatch": (
                    {
                        "mode": dispatch.mode,
                        "title": dispatch.title,
                        "body": dispatch.body,
                        "extra": dispatch.extra,
                        "is_handoff": dispatch.is_handoff,
                    }
                    if dispatch
                    else None
                ),
                "errors": errors,
                "files": files,
            }
        )
    return history


def _collect_reply_history(
    *, repo_root: Path, run_id: str, record_input: dict[str, Any]
):
    workspace_root = _resolve_workspace_root(record_input, repo_root)
    reply_paths = resolve_reply_paths(workspace_root=workspace_root, run_id=run_id)
    history: list[dict[str, Any]] = []
    for seq, entry_dir in reversed(iter_seq_dirs(reply_paths.reply_history_dir)):
        reply_path = entry_dir / "USER_REPLY.md"
        reply, errors = (
            parse_user_reply(reply_path)
            if reply_path.exists()
            else (None, ["USER_REPLY.md missing"])
        )
        files = collect_entry_files(
            entry_dir,
            skip_name="USER_REPLY.md",
            url_prefix=f"api/flows/{run_id}/reply_history/{seq:04d}",
        )
        created_at = _timestamp(reply_path) or _timestamp(entry_dir)
        history.append(
            {
                "seq": seq,
                "dir": safe_relpath(entry_dir, workspace_root),
                "created_at": created_at,
                "reply": (
                    {"title": reply.title, "body": reply.body, "extra": reply.extra}
                    if reply
                    else None
                ),
                "errors": errors,
                "files": files,
            }
        )
    return history


def build_messages_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/messages/active")
    def get_active_message(request: Request):
        repo_root = find_repo_root()
        db_path = _flows_db_path(repo_root)
        if not db_path.exists():
            return {"active": False}
        try:
            with FlowStore(db_path, durable=_get_durable_writes(repo_root)) as store:
                paused = store.list_flow_runs(
                    flow_type="ticket_flow", status=FlowRunStatus.PAUSED
                )
        except (sqlite3.Error, OSError, ValueError, KeyError):
            return {"active": False}
        if not paused:
            return {"active": False}

        for record in paused:
            history = _collect_dispatch_history(
                repo_root=repo_root,
                run_id=str(record.id),
                record_input=dict(record.input_data or {}),
            )
            if not history:
                continue
            latest = history[0]
            return {
                "active": True,
                "run_id": record.id,
                "flow_type": record.flow_type,
                "status": record.status.value,
                "seq": latest.get("seq"),
                "dispatch": latest.get("dispatch"),
                "files": latest.get("files"),
                "open_url": f"?tab=inbox&run_id={record.id}",
            }

        return {"active": False}

    @router.get("/api/messages/threads")
    def list_threads():
        repo_root = find_repo_root()
        db_path = _flows_db_path(repo_root)
        if not db_path.exists():
            return {"conversations": []}
        try:
            with FlowStore(db_path, durable=_get_durable_writes(repo_root)) as store:
                runs = store.list_flow_runs(flow_type="ticket_flow")
        except (sqlite3.Error, OSError, ValueError, KeyError):
            return {"conversations": []}

        conversations: list[dict[str, Any]] = []
        for record in runs:
            record_input = dict(record.input_data or {})
            dispatch_history = _collect_dispatch_history(
                repo_root=repo_root,
                run_id=str(record.id),
                record_input=record_input,
            )
            if not dispatch_history:
                continue
            latest = dispatch_history[0]
            reply_history = _collect_reply_history(
                repo_root=repo_root,
                run_id=str(record.id),
                record_input=record_input,
            )
            failure_payload = get_failure_payload(record)
            failure_summary = (
                format_failure_summary(failure_payload) if failure_payload else None
            )
            conversations.append(
                {
                    "run_id": record.id,
                    "flow_type": record.flow_type,
                    "status": record.status.value,
                    "created_at": record.created_at,
                    "started_at": record.started_at,
                    "finished_at": record.finished_at,
                    "current_step": record.current_step,
                    "latest": latest,
                    "dispatch_count": len(dispatch_history),
                    "reply_count": len(reply_history),
                    "ticket_state": ticket_state_snapshot(record.state),
                    "failure": failure_payload,
                    "failure_summary": failure_summary,
                    "open_url": f"?tab=inbox&run_id={record.id}",
                }
            )
        return {"conversations": conversations}

    @router.get("/api/messages/threads/{run_id}")
    def get_thread(run_id: str):
        repo_root = find_repo_root()
        db_path = _flows_db_path(repo_root)
        empty_response = {
            "dispatch_history": [],
            "reply_history": [],
            "dispatch_count": 0,
            "reply_count": 0,
        }
        if not db_path.exists():
            return empty_response
        try:
            with FlowStore(db_path, durable=_get_durable_writes(repo_root)) as store:
                record = store.get_flow_run(run_id)
        except (sqlite3.Error, OSError, ValueError, KeyError):
            raise HTTPException(
                status_code=404, detail="Flows database unavailable"
            ) from None
        if not record:
            return empty_response
        input_data = dict(record.input_data or {})
        dispatch_history = _collect_dispatch_history(
            repo_root=repo_root, run_id=run_id, record_input=input_data
        )
        reply_history = _collect_reply_history(
            repo_root=repo_root, run_id=run_id, record_input=input_data
        )
        failure_payload = get_failure_payload(record)
        failure_summary = (
            format_failure_summary(failure_payload) if failure_payload else None
        )
        return {
            "run": {
                "id": record.id,
                "flow_type": record.flow_type,
                "status": record.status.value,
                "created_at": record.created_at,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
                "current_step": record.current_step,
                "error_message": record.error_message,
                "failure": failure_payload,
                "failure_summary": failure_summary,
            },
            "dispatch_history": dispatch_history,
            "reply_history": reply_history,
            "dispatch_count": len(dispatch_history),
            "reply_count": len(reply_history),
            "ticket_state": ticket_state_snapshot(record.state),
        }

    @router.post("/api/messages/{run_id}/reply")
    async def post_reply(
        run_id: str,
        body: str = Form(""),
        title: Optional[str] = Form(None),
        files: list[UploadFile] = File(default=[]),  # noqa: B006,B008
    ):
        repo_root = find_repo_root()
        db_path = _flows_db_path(repo_root)
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="No flows database")
        try:
            with FlowStore(db_path, durable=_get_durable_writes(repo_root)) as store:
                record = store.get_flow_run(run_id)
        except (sqlite3.Error, OSError, ValueError, KeyError):
            raise HTTPException(
                status_code=404, detail="Flows database unavailable"
            ) from None
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")

        input_data = dict(record.input_data or {})
        workspace_root = _resolve_workspace_root(input_data, repo_root)
        reply_paths = resolve_reply_paths(workspace_root=workspace_root, run_id=run_id)
        ensure_reply_dirs(reply_paths)

        cleaned_title = (
            title.strip() if isinstance(title, str) and title.strip() else None
        )
        cleaned_body = body or ""

        if cleaned_title:
            fm = yaml.safe_dump({"title": cleaned_title}, sort_keys=False).strip()
            raw = f"---\n{fm}\n---\n\n{cleaned_body}\n"
        else:
            raw = cleaned_body
            if raw and not raw.endswith("\n"):
                raw += "\n"

        try:
            reply_paths.user_reply_path.parent.mkdir(parents=True, exist_ok=True)
            reply_paths.user_reply_path.write_text(raw, encoding="utf-8")
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to write USER_REPLY.md: {exc}"
            ) from exc

        for upload in files:
            try:
                filename = safe_attachment_name(upload.filename or "")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            dest = reply_paths.reply_dir / filename
            data = await upload.read()
            try:
                dest.write_bytes(data)
                try:
                    ensure_structure(repo_root)
                    save_file(repo_root, "inbox", filename, data)
                except (OSError, ValueError):
                    _logger.debug(
                        "Failed to mirror attachment into FileBox", exc_info=True
                    )
            except OSError as exc:
                raise HTTPException(
                    status_code=500, detail=f"Failed to write attachment: {exc}"
                ) from exc

        seq = next_reply_seq(reply_paths.reply_history_dir)
        dispatch, errors = dispatch_reply(reply_paths, next_seq=seq)
        if errors:
            raise HTTPException(status_code=400, detail=errors)
        if dispatch is None:
            raise HTTPException(status_code=500, detail="Failed to archive reply")
        return {
            "status": "ok",
            "seq": dispatch.seq,
            "reply": {"title": dispatch.reply.title, "body": dispatch.reply.body},
        }

    return router


__all__ = ["build_messages_routes"]
