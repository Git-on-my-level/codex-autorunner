from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from ....core.orchestration.cold_trace_store import ColdTraceStore

_COLD_TRACE_DEFAULT_LIMIT = 50
_COLD_TRACE_MAX_LIMIT = 500
_PREVIEW_FAMILY_WHITELIST = frozenset(
    {"tool_call", "tool_result", "output_delta", "run_notice", "terminal"}
)


def _cold_trace_store(request: Request) -> ColdTraceStore:
    return ColdTraceStore(request.app.state.config.root)


def _annotate_truncation_flags(event: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(event)
    payload = annotated.get("payload")
    if not isinstance(payload, dict):
        return annotated
    details_in_cold = payload.get("details_in_cold_trace")
    if details_in_cold:
        annotated["detail_available_in_cold_trace"] = True
    for key in (
        "tool_input_truncated",
        "result_truncated",
        "error_truncated",
        "message_truncated",
        "data_truncated",
        "content_truncated",
        "final_message_truncated",
        "error_message_truncated",
    ):
        if payload.get(key):
            annotated["preview_truncated"] = True
            break
    return annotated


def build_trace_inspection_routes(router: APIRouter) -> None:

    @router.get("/traces/manifests/{execution_id}")
    def get_trace_manifest(
        execution_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store = _cold_trace_store(request)
        manifest = store.get_manifest(execution_id)
        if manifest is None:
            raise HTTPException(
                status_code=404,
                detail="No cold trace manifest found for this execution",
            )
        return {
            "manifest": manifest.to_dict(),
            "execution_id": execution_id,
            "storage_layer": "cold_trace",
        }

    @router.get("/traces/manifests")
    def list_trace_manifests(
        request: Request,
        status: Optional[str] = None,
        limit: int = _COLD_TRACE_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        limit = min(limit, _COLD_TRACE_MAX_LIMIT)
        store = _cold_trace_store(request)
        manifests = store.list_manifests(status=status, limit=limit)
        return {
            "manifests": [m.to_dict() for m in manifests],
            "count": len(manifests),
            "storage_layer": "cold_trace",
        }

    @router.get("/traces/events/{execution_id}")
    def get_trace_events(
        execution_id: str,
        request: Request,
        offset: int = 0,
        limit: int = _COLD_TRACE_DEFAULT_LIMIT,
        family: Optional[str] = None,
    ) -> dict[str, Any]:
        if offset < 0:
            raise HTTPException(status_code=400, detail="offset must be >= 0")
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        limit = min(limit, _COLD_TRACE_MAX_LIMIT)
        store = _cold_trace_store(request)
        manifest = store.get_manifest(execution_id)
        if manifest is None:
            return {
                "execution_id": execution_id,
                "events": [],
                "offset": offset,
                "limit": limit,
                "total_count": 0,
                "has_more": False,
                "storage_layer": "cold_trace",
                "manifest_available": False,
            }
        all_events = store.read_events(execution_id, offset=0, limit=None)
        if family is not None:
            all_events = [
                e
                for e in all_events
                if isinstance(e, dict) and e.get("event_family") == family
            ]
        total_count = len(all_events)
        page = all_events[offset : offset + limit]
        annotated = [_annotate_truncation_flags(e) for e in page]
        return {
            "execution_id": execution_id,
            "trace_id": manifest.trace_id,
            "events": annotated,
            "offset": offset,
            "limit": limit,
            "total_count": total_count,
            "has_more": (offset + limit) < total_count,
            "storage_layer": "cold_trace",
            "manifest_available": True,
            "trace_status": manifest.status,
            "includes_families": list(manifest.includes_families),
        }

    @router.get("/traces/checkpoint/{execution_id}")
    def get_trace_checkpoint(
        execution_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store = _cold_trace_store(request)
        checkpoint = store.load_checkpoint(execution_id)
        if checkpoint is None:
            raise HTTPException(
                status_code=404,
                detail="No checkpoint found for this execution",
            )
        manifest = store.get_manifest(execution_id)
        result: dict[str, Any] = {
            "checkpoint": checkpoint.to_dict(),
            "execution_id": execution_id,
            "storage_layer": "compact_checkpoint",
        }
        if manifest is not None:
            result["trace_manifest"] = {
                "trace_id": manifest.trace_id,
                "status": manifest.status,
                "event_count": manifest.event_count,
            }
            result["cold_trace_available"] = True
        else:
            result["cold_trace_available"] = False
        return result


__all__ = ["build_trace_inspection_routes"]
