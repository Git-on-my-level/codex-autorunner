from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from ....core.feedback_reports import FeedbackReportStore


def _require_hub_root(request: Request) -> Path:
    config = getattr(request.app.state, "config", None)
    hub_root = getattr(config, "root", None)
    if hub_root is None:
        raise HTTPException(status_code=503, detail="Hub config unavailable")
    return Path(hub_root)


def _serialize_reports(items: list[Any]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for item in items:
        to_dict = getattr(item, "to_dict", None)
        if callable(to_dict):
            serialized.append(to_dict())
    return serialized


def build_feedback_report_routes() -> APIRouter:
    router = APIRouter(prefix="/hub/feedback-reports", tags=["feedback"])

    @router.post("", status_code=201)
    def create_feedback_report(
        request: Request,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        hub_root = _require_hub_root(request)
        try:
            report = FeedbackReportStore(hub_root).create_report(**payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return report.to_dict()

    @router.get("")
    def list_feedback_reports(
        request: Request,
        repo_id: Optional[str] = None,
        thread_target_id: Optional[str] = None,
        report_kind: Optional[str] = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        hub_root = _require_hub_root(request)
        reports = FeedbackReportStore(hub_root).list_reports(
            repo_id=repo_id,
            thread_target_id=thread_target_id,
            report_kind=report_kind,
            limit=limit,
        )
        return {
            "reports": _serialize_reports(reports),
            "limit": max(0, int(limit)),
        }

    return router


__all__ = ["build_feedback_report_routes"]
