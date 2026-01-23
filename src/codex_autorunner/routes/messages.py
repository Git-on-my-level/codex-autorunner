"""Message / inbox endpoints.

These endpoints provide a thin wrapper over the durable on-disk ticket_flow
handoff history (agent -> human) and reply history (human -> agent).

The UI contract is intentionally filesystem-backed:
* Agent messages come from `.codex-autorunner/runs/<run_id>/handoff_history/<seq>/`.
* Human replies are written to USER_REPLY.md + reply/* and immediately archived
  into `.codex-autorunner/runs/<run_id>/reply_history/<seq>/`.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import yaml
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from ..core.flows.models import FlowRunStatus
from ..core.flows.store import FlowStore
from ..core.utils import find_repo_root
from ..tickets.files import safe_relpath
from ..tickets.outbox import parse_user_message, resolve_outbox_paths
from ..tickets.replies import (
    dispatch_reply,
    ensure_reply_dirs,
    next_reply_seq,
    parse_user_reply,
    resolve_reply_paths,
)

_logger = logging.getLogger(__name__)


def _flows_db_path(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "flows.db"


def _safe_attachment_name(name: str) -> str:
    base = os.path.basename(name or "").strip()
    if not base:
        raise ValueError("Missing attachment filename")
    if base.lower() == "user_reply.md":
        raise ValueError("Attachment filename reserved: USER_REPLY.md")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", base):
        raise ValueError(
            "Invalid attachment filename; use only letters, digits, dot, underscore, dash"
        )
    return base


def _iter_seq_dirs(history_dir: Path) -> list[tuple[int, Path]]:
    if not history_dir.exists() or not history_dir.is_dir():
        return []
    out: list[tuple[int, Path]] = []
    try:
        for child in history_dir.iterdir():
            try:
                if not child.is_dir():
                    continue
                name = child.name
                if not (len(name) == 4 and name.isdigit()):
                    continue
                out.append((int(name), child))
            except OSError:
                continue
    except OSError:
        return []
    out.sort(key=lambda x: x[0])
    return out


def _collect_handoff_history(
    *, repo_root: Path, run_id: str, record_input: dict[str, Any]
) -> list[dict[str, Any]]:
    workspace_root = Path(record_input.get("workspace_root") or repo_root)
    runs_dir = Path(record_input.get("runs_dir") or ".codex-autorunner/runs")
    outbox_paths = resolve_outbox_paths(
        workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_id
    )
    history: list[dict[str, Any]] = []
    for seq, entry_dir in reversed(_iter_seq_dirs(outbox_paths.handoff_history_dir)):
        msg_path = entry_dir / "USER_MESSAGE.md"
        msg, errors = parse_user_message(msg_path)
        files: list[dict[str, str]] = []
        try:
            for child in sorted(entry_dir.iterdir(), key=lambda p: p.name):
                try:
                    if child.name.startswith("."):
                        continue
                    if child.name == "USER_MESSAGE.md":
                        continue
                    if child.is_dir():
                        continue
                    rel = child.name
                    url = f"/api/flows/{run_id}/handoff_history/{seq:04d}/{quote(rel)}"
                    files.append({"name": child.name, "url": url})
                except OSError:
                    continue
        except OSError:
            files = []
        history.append(
            {
                "seq": seq,
                "dir": safe_relpath(entry_dir, workspace_root),
                "message": (
                    {
                        "mode": msg.mode,
                        "title": msg.title,
                        "body": msg.body,
                        "extra": msg.extra,
                    }
                    if msg
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
    workspace_root = Path(record_input.get("workspace_root") or repo_root)
    runs_dir = Path(record_input.get("runs_dir") or ".codex-autorunner/runs")
    reply_paths = resolve_reply_paths(
        workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_id
    )
    history: list[dict[str, Any]] = []
    for seq, entry_dir in reversed(_iter_seq_dirs(reply_paths.reply_history_dir)):
        reply_path = entry_dir / "USER_REPLY.md"
        reply, errors = (
            parse_user_reply(reply_path)
            if reply_path.exists()
            else (None, ["USER_REPLY.md missing"])
        )
        files: list[dict[str, str]] = []
        try:
            for child in sorted(entry_dir.iterdir(), key=lambda p: p.name):
                try:
                    if child.name.startswith("."):
                        continue
                    if child.name == "USER_REPLY.md":
                        continue
                    if child.is_dir():
                        continue
                    rel = child.name
                    url = f"/api/messages/{run_id}/reply_history/{seq:04d}/{quote(rel)}"
                    files.append({"name": child.name, "url": url})
                except OSError:
                    continue
        except OSError:
            files = []
        history.append(
            {
                "seq": seq,
                "dir": safe_relpath(entry_dir, workspace_root),
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
        store = FlowStore(db_path)
        try:
            store.initialize()
        except Exception:
            # Corrupt flows db should not 500 the UI.
            return {"active": False}

        paused = store.list_flow_runs(
            flow_type="ticket_flow", status=FlowRunStatus.PAUSED
        )
        if not paused:
            return {"active": False}

        # Walk paused runs (newest first as returned by FlowStore) until we find
        # one with at least one archived handoff message. This avoids hiding
        # older paused runs that do have history when the newest paused run
        # hasn't yet written USER_MESSAGE.md.
        for record in paused:
            history = _collect_handoff_history(
                repo_root=repo_root,
                run_id=str(record.id),
                record_input=dict(record.input_data or {}),
            )
            if not history:
                continue
            latest = history[0]
            return {
                "active": True,
                "repo_id": request.app.state.repo_id,
                "run_id": record.id,
                "flow_type": record.flow_type,
                "status": record.status.value,
                "seq": latest.get("seq"),
                "message": latest.get("message"),
                "files": latest.get("files"),
                "open_url": f"/?tab=messages&run_id={record.id}",
            }

        return {"active": False}

    @router.get("/api/messages/threads")
    def list_threads():
        repo_root = find_repo_root()
        db_path = _flows_db_path(repo_root)
        if not db_path.exists():
            return {"threads": []}
        store = FlowStore(db_path)
        try:
            store.initialize()
        except Exception:
            return {"threads": []}
        runs = store.list_flow_runs(flow_type="ticket_flow")
        threads: list[dict[str, Any]] = []
        for record in runs:
            history = _collect_handoff_history(
                repo_root=repo_root,
                run_id=str(record.id),
                record_input=dict(record.input_data or {}),
            )
            if not history:
                continue
            latest = history[0]
            threads.append(
                {
                    "run_id": record.id,
                    "flow_type": record.flow_type,
                    "status": record.status.value,
                    "created_at": record.created_at,
                    "started_at": record.started_at,
                    "finished_at": record.finished_at,
                    "current_step": record.current_step,
                    "latest": latest,
                    "open_url": f"/?tab=messages&run_id={record.id}",
                }
            )
        return {"threads": threads}

    @router.get("/api/messages/threads/{run_id}")
    def get_thread(run_id: str):
        repo_root = find_repo_root()
        db_path = _flows_db_path(repo_root)
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="No flows database")
        store = FlowStore(db_path)
        try:
            store.initialize()
        except Exception as exc:
            raise HTTPException(
                status_code=404, detail="Flows database unavailable"
            ) from exc

        record = store.get_flow_run(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")
        input_data = dict(record.input_data or {})
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
            },
            "handoff_history": _collect_handoff_history(
                repo_root=repo_root, run_id=run_id, record_input=input_data
            ),
            "reply_history": _collect_reply_history(
                repo_root=repo_root, run_id=run_id, record_input=input_data
            ),
        }

    @router.get("/api/messages/{run_id}/reply_history/{seq}/{file_path:path}")
    def get_reply_history_file(run_id: str, seq: str, file_path: str):
        repo_root = find_repo_root()
        db_path = _flows_db_path(repo_root)
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="No flows database")
        store = FlowStore(db_path)
        try:
            store.initialize()
        except Exception as exc:
            raise HTTPException(
                status_code=404, detail="Flows database unavailable"
            ) from exc
        record = store.get_flow_run(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")

        if not (len(seq) == 4 and seq.isdigit()):
            raise HTTPException(status_code=400, detail="Invalid seq")
        if ".." in file_path or file_path.startswith("/"):
            raise HTTPException(status_code=400, detail="Invalid file path")
        filename = os.path.basename(file_path)
        if filename != file_path:
            raise HTTPException(status_code=400, detail="Invalid file path")

        input_data = dict(record.input_data or {})
        workspace_root = Path(input_data.get("workspace_root") or repo_root)
        runs_dir = Path(input_data.get("runs_dir") or ".codex-autorunner/runs")
        reply_paths = resolve_reply_paths(
            workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_id
        )
        target = reply_paths.reply_history_dir / seq / filename
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(path=str(target), filename=filename)

    @router.post("/api/messages/{run_id}/reply")
    async def post_reply(
        run_id: str,
        body: str = Form(""),
        title: Optional[str] = Form(None),
        # NOTE: FastAPI/starlette will supply either a single UploadFile or a list
        # depending on how the multipart form is encoded. Declaring this as a
        # concrete list avoids a common 422 where a single file upload is treated
        # as a non-list value.
        files: list[UploadFile] = File(default=[]),  # noqa: B006,B008
    ):
        repo_root = find_repo_root()
        db_path = _flows_db_path(repo_root)
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="No flows database")
        store = FlowStore(db_path)
        store.initialize()
        record = store.get_flow_run(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")

        input_data = dict(record.input_data or {})
        workspace_root = Path(input_data.get("workspace_root") or repo_root)
        runs_dir = Path(input_data.get("runs_dir") or ".codex-autorunner/runs")
        reply_paths = resolve_reply_paths(
            workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_id
        )
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
                filename = _safe_attachment_name(upload.filename or "")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            dest = reply_paths.reply_dir / filename
            data = await upload.read()
            try:
                dest.write_bytes(data)
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
