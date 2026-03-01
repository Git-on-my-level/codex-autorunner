from typing import TYPE_CHECKING, Optional

from fastapi import FastAPI, HTTPException

from ...core.usage import (
    UsageError,
    default_codex_home,
    get_hub_usage_series_cached,
    get_hub_usage_summary_cached,
    parse_iso_datetime,
)
from ...manifest import load_manifest
from .schemas import HubJobResponse

if TYPE_CHECKING:
    from .app_state import HubAppContext


def register_simple_hub_routes(app: FastAPI, context: "HubAppContext") -> None:
    @app.get("/hub/usage")
    def hub_usage(since: Optional[str] = None, until: Optional[str] = None):
        try:
            since_dt = parse_iso_datetime(since)
            until_dt = parse_iso_datetime(until)
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        manifest = load_manifest(context.config.manifest_path, context.config.root)
        repo_map = [
            (repo.id, (context.config.root / repo.path)) for repo in manifest.repos
        ]
        per_repo, unmatched, status = get_hub_usage_summary_cached(
            repo_map,
            default_codex_home(),
            config=context.config,
            since=since_dt,
            until=until_dt,
        )
        return {
            "mode": "hub",
            "hub_root": str(context.config.root),
            "codex_home": str(default_codex_home()),
            "since": since,
            "until": until,
            "status": status,
            "repos": [
                {
                    "id": repo_id,
                    "events": summary.events,
                    "totals": summary.totals.to_dict(),
                    "latest_rate_limits": summary.latest_rate_limits,
                    "source_confidence": summary.source_confidence,
                }
                for repo_id, summary in per_repo.items()
            ],
            "unmatched": unmatched.to_dict(),
        }

    @app.get("/hub/usage/series")
    def hub_usage_series(
        since: Optional[str] = None,
        until: Optional[str] = None,
        bucket: str = "day",
        segment: str = "none",
    ):
        try:
            since_dt = parse_iso_datetime(since)
            until_dt = parse_iso_datetime(until)
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        manifest = load_manifest(context.config.manifest_path, context.config.root)
        repo_map = [
            (repo.id, (context.config.root / repo.path)) for repo in manifest.repos
        ]
        try:
            series, status = get_hub_usage_series_cached(
                repo_map,
                default_codex_home(),
                config=context.config,
                since=since_dt,
                until=until_dt,
                bucket=bucket,
                segment=segment,
            )
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "mode": "hub",
            "hub_root": str(context.config.root),
            "codex_home": str(default_codex_home()),
            "since": since,
            "until": until,
            "status": status,
            **series,
        }

    @app.get("/hub/version")
    def hub_version():
        return {"asset_version": app.state.asset_version}

    @app.get("/hub/jobs/{job_id}", response_model=HubJobResponse)
    async def get_hub_job(job_id: str):
        job = await context.job_manager.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job.to_dict()
