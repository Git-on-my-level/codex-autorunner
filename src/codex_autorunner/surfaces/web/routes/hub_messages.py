from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from ....core.capability_hints import (
    CAPABILITY_HINT_ITEM_TYPE,
    HUB_HINT_REPO_ID,
    capability_hint_id_from_run_id,
    is_hub_scoped_hint_id,
)
from ....core.freshness import (
    iso_now,
    resolve_stale_threshold_seconds,
    summarize_section_freshness,
)
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
    async def hub_messages(limit: int = 100, scope_key: Optional[str] = None):
        """Return paused ticket_flow dispatches across all repos.

        The hub inbox is intentionally simple: it surfaces the latest archived
        dispatch for each paused ticket_flow run.
        """
        items = await asyncio.to_thread(
            hub_gather_service.gather_hub_messages,
            context,
            limit=limit,
            scope_key=scope_key,
        )
        generated_at = iso_now()
        stale_threshold_seconds = resolve_stale_threshold_seconds(
            getattr(context.config.pma, "freshness_stale_threshold_seconds", None)
        )
        return {
            "generated_at": generated_at,
            "freshness": {
                "schema_version": 1,
                "generated_at": generated_at,
                "stale_threshold_seconds": stale_threshold_seconds,
                "sections": {
                    "inbox": summarize_section_freshness(
                        items,
                        generated_at=generated_at,
                        stale_threshold_seconds=stale_threshold_seconds,
                        extractor=lambda item: (
                            (item.get("canonical_state_v1") or {}).get("freshness")
                            if isinstance(item, dict)
                            else None
                        ),
                    )
                },
            },
            "items": items,
        }

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
        hint_id = str(payload.get("hint_id") or "").strip() or None
        scope_key = str(payload.get("scope_key") or "").strip() or None
        action = str(payload.get("action") or "dismiss").strip() or "dismiss"
        reason_raw = payload.get("reason")
        reason = str(reason_raw).strip() if isinstance(reason_raw, str) else ""
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
            if item_type != CAPABILITY_HINT_ITEM_TYPE or not is_hub_scoped_hint_id(
                hint_id
            ):
                raise HTTPException(status_code=400, detail="Missing repo_id")
        if not run_id:
            raise HTTPException(status_code=400, detail="Missing run_id")
        if action not in {"dismiss"}:
            raise HTTPException(status_code=400, detail="Unsupported action")

        if not item_type:
            hub_payload = await hub_messages(limit=2000, scope_key=scope_key)
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
                candidate_hint_id = str(item.get("hint_id") or "").strip() or None
                if hint_id is not None and candidate_hint_id != hint_id:
                    continue
                candidate_seq = item.get("seq")
                if seq is not None and candidate_seq != seq:
                    continue
                matched = item
                break
            if matched is None:
                raise HTTPException(status_code=404, detail="Hub message not found")
            item_type = str(matched.get("item_type") or "").strip() or "run_dispatch"
            if hint_id is None:
                matched_hint_id = str(matched.get("hint_id") or "").strip()
                hint_id = matched_hint_id or None
            if seq is None:
                matched_seq = matched.get("seq")
                if isinstance(matched_seq, int) and matched_seq > 0:
                    seq = matched_seq
        elif item_type == "run_dispatch" and seq is None:
            raise HTTPException(status_code=400, detail="Missing seq for run_dispatch")
        elif item_type == CAPABILITY_HINT_ITEM_TYPE:
            if hint_id is None:
                hint_id = capability_hint_id_from_run_id(run_id)
            if hint_id is None:
                raise HTTPException(status_code=400, detail="Missing hint_id")
            if scope_key is None:
                raise HTTPException(status_code=400, detail="Missing scope_key")

        resolution_root: Optional[Path] = None
        resolved_repo_id = repo_id
        if item_type == CAPABILITY_HINT_ITEM_TYPE and is_hub_scoped_hint_id(hint_id):
            resolution_root = context.config.root
            resolved_repo_id = HUB_HINT_REPO_ID
        else:
            snapshots = await asyncio.to_thread(context.supervisor.list_repos)
            snapshot = next((s for s in snapshots if s.id == repo_id), None)
            if snapshot is None or not snapshot.exists_on_disk:
                raise HTTPException(status_code=404, detail="Repo not found")
            resolution_root = snapshot.path

        repo_lock = await _repo_dismissal_lock(resolution_root)
        async with repo_lock:
            resolved = _record_message_resolution(
                repo_root=resolution_root,
                repo_id=resolved_repo_id,
                run_id=run_id,
                item_type=item_type,
                seq=seq,
                action=action,
                reason=reason or None,
                actor="hub_messages_resolve",
                hint_id=hint_id,
                scope_key=scope_key,
            )
        return {"status": "ok", "resolved": resolved}

    return router


__all__ = ["build_hub_messages_routes"]
