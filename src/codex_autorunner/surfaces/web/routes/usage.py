"""
Usage routes: token usage summaries for repo/hub.

Moved out of the legacy docs routes during the workspace + file chat cutover.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ...core.usage import (
    UsageError,
    default_codex_home,
    get_repo_usage_series_cached,
    get_repo_usage_summary_cached,
    parse_iso_datetime,
)
from .schemas import RepoUsageResponse, UsageSeriesResponse


def build_usage_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["usage"])

    @router.get("/usage", response_model=RepoUsageResponse)
    def get_usage(
        request: Request, since: Optional[str] = None, until: Optional[str] = None
    ):
        engine = request.app.state.engine
        try:
            since_dt = parse_iso_datetime(since)
            until_dt = parse_iso_datetime(until)
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        summary, status = get_repo_usage_summary_cached(
            engine.repo_root,
            default_codex_home(),
            config=engine.config,
            since=since_dt,
            until=until_dt,
        )
        return {
            "mode": "repo",
            "repo": str(engine.repo_root),
            "codex_home": str(default_codex_home()),
            "since": since,
            "until": until,
            "status": status,
            **summary.to_dict(),
        }

    @router.get("/usage/series", response_model=UsageSeriesResponse)
    def get_usage_series(
        request: Request,
        since: Optional[str] = None,
        until: Optional[str] = None,
        bucket: str = "day",
        segment: str = "none",
    ):
        engine = request.app.state.engine
        try:
            since_dt = parse_iso_datetime(since)
            until_dt = parse_iso_datetime(until)
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            series, status = get_repo_usage_series_cached(
                engine.repo_root,
                default_codex_home(),
                config=engine.config,
                since=since_dt,
                until=until_dt,
                bucket=bucket,
                segment=segment,
            )
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "mode": "repo",
            "repo": str(engine.repo_root),
            "codex_home": str(default_codex_home()),
            "since": since,
            "until": until,
            "status": status,
            **series,
        }

    return router
