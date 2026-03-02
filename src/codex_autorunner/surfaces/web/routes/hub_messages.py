from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from ..app_state import (
    HubAppContext,
    _record_message_resolution,
)
from ..services import hub_gather as hub_gather_service


def build_hub_messages_routes(context: HubAppContext) -> APIRouter:
    router = APIRouter()
    hub_dismissal_locks: dict[str, asyncio.Lock] = {}
    hub_dismissal_locks_guard = asyncio.Lock()

    async def _repo_dismissal_lock(repo_root: Path) -> asyncio.Lock:
        key = str(repo_root.resolve())
        async with hub_dismissal_locks_guard:
            lock = hub_dismissal_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                hub_dismissal_locks[key] = lock
            return lock

    @router.get("/hub/messages")
    async def hub_messages(limit: int = 100):
        """Return paused ticket_flow dispatches across all repos.

        The hub inbox is intentionally simple: it surfaces the latest archived
        dispatch for each paused ticket_flow run.
        """
        items = await asyncio.to_thread(
            hub_gather_service.gather_hub_messages, context, limit=limit
        )
        return {"items": items}

    @router.post("/hub/messages/dismiss")
    async def dismiss_hub_message(payload: dict[str, Any]):
        repo_id = str(payload.get("repo_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        seq_raw = payload.get("seq")
        reason_raw = payload.get("reason")
        reason = str(reason_raw).strip() if isinstance(reason_raw, str) else ""
        if not repo_id:
            raise HTTPException(status_code=400, detail="Missing repo_id")
        if not run_id:
            raise HTTPException(status_code=400, detail="Missing run_id")
        if seq_raw is None:
            raise HTTPException(status_code=400, detail="Invalid seq")
        try:
            seq = int(seq_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid seq") from None
        if seq <= 0:
            raise HTTPException(status_code=400, detail="Invalid seq")

        snapshots = await asyncio.to_thread(context.supervisor.list_repos)
        snapshot = next((s for s in snapshots if s.id == repo_id), None)
        if snapshot is None or not snapshot.exists_on_disk:
            raise HTTPException(status_code=404, detail="Repo not found")

        repo_lock = await _repo_dismissal_lock(snapshot.path)
        async with repo_lock:
            dismissed = _record_message_resolution(
                repo_root=snapshot.path,
                repo_id=repo_id,
                run_id=run_id,
                item_type="run_dispatch",
                seq=seq,
                action="dismiss",
                reason=reason or None,
                actor="hub_messages_dismiss",
            )
            dismissed_at = str(dismissed.get("resolved_at") or "")
            dismissed["dismissed_at"] = dismissed_at
        return {
            "status": "ok",
            "dismissed": dismissed,
        }

    @router.post("/hub/messages/resolve")
    async def resolve_hub_message(payload: dict[str, Any]):
        repo_id = str(payload.get("repo_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        item_type = str(payload.get("item_type") or "").strip()
        action = str(payload.get("action") or "dismiss").strip() or "dismiss"
        reason_raw = payload.get("reason")
        actor_raw = payload.get("actor")
        reason = str(reason_raw).strip() if isinstance(reason_raw, str) else ""
        actor = str(actor_raw).strip() if isinstance(actor_raw, str) else ""
        seq_raw = payload.get("seq")
        seq: Optional[int] = None
        if seq_raw is not None and seq_raw != "":
            try:
                parsed = int(seq_raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Invalid seq") from None
            if parsed <= 0:
                raise HTTPException(status_code=400, detail="Invalid seq")
            seq = parsed

        if not repo_id:
            raise HTTPException(status_code=400, detail="Missing repo_id")
        if not run_id:
            raise HTTPException(status_code=400, detail="Missing run_id")
        if action not in {"dismiss"}:
            raise HTTPException(status_code=400, detail="Unsupported action")

        snapshots = await asyncio.to_thread(context.supervisor.list_repos)
        snapshot = next((s for s in snapshots if s.id == repo_id), None)
        if snapshot is None or not snapshot.exists_on_disk:
            raise HTTPException(status_code=404, detail="Repo not found")

        if not item_type:
            hub_payload = await hub_messages(limit=2000)
            items_raw = (
                hub_payload.get("items", []) if isinstance(hub_payload, dict) else []
            )
            matched = None
            for item in items_raw if isinstance(items_raw, list) else []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("repo_id") or "") != repo_id:
                    continue
                if str(item.get("run_id") or "") != run_id:
                    continue
                candidate_seq = item.get("seq")
                if seq is not None and candidate_seq != seq:
                    continue
                matched = item
                break
            if matched is None:
                raise HTTPException(status_code=404, detail="Hub message not found")
            item_type = str(matched.get("item_type") or "").strip() or "run_dispatch"
            if seq is None:
                matched_seq = matched.get("seq")
                if isinstance(matched_seq, int) and matched_seq > 0:
                    seq = matched_seq
        elif item_type == "run_dispatch" and seq is None:
            raise HTTPException(status_code=400, detail="Missing seq for run_dispatch")

        repo_lock = await _repo_dismissal_lock(snapshot.path)
        async with repo_lock:
            resolved = _record_message_resolution(
                repo_root=snapshot.path,
                repo_id=repo_id,
                run_id=run_id,
                item_type=item_type,
                seq=seq,
                action=action,
                reason=reason or None,
                actor=actor or "hub_messages_resolve",
            )
        return {"status": "ok", "resolved": resolved}

    return router


__all__ = ["build_hub_messages_routes"]
