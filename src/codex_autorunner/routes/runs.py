"""
Run telemetry routes.
"""

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from ..core.utils import is_within
from .shared import SSE_HEADERS, jsonl_event_stream


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _token_total(entry: dict[str, Any]) -> Optional[float]:
    token_usage = entry.get("token_usage")
    if not isinstance(token_usage, dict):
        return None
    delta = token_usage.get("delta")
    if isinstance(delta, dict):
        for key in ("total_tokens", "totalTokens", "total"):
            value = delta.get(key)
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _extract_total_from_dict(token_dict: Optional[dict[str, Any]]) -> Optional[float]:
    if token_dict is None:
        return None
    if not isinstance(token_dict, dict):
        return None
    for key in ("total_tokens", "totalTokens", "total"):
        value = token_dict.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _completed_todo_count(entry: dict[str, Any]) -> int:
    todo = entry.get("todo")
    if not isinstance(todo, dict):
        return 0
    counts = todo.get("counts")
    if isinstance(counts, dict):
        value = counts.get("completed")
        if isinstance(value, int):
            return value
    completed = todo.get("completed")
    return len(completed) if isinstance(completed, list) else 0


def build_runs_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/runs")
    def list_runs(request: Request, limit: int = 200):
        engine = request.app.state.engine
        engine.reconcile_run_index()
        index = engine._load_run_index()
        entries: list[dict[str, Any]] = []
        for key, entry in index.items():
            try:
                run_id = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            started = _parse_iso(entry.get("started_at"))
            finished = _parse_iso(entry.get("finished_at"))
            duration = None
            if started and finished:
                duration = (finished - started).total_seconds()
            enriched = dict(entry)
            enriched["run_id"] = run_id
            enriched["duration_seconds"] = duration
            enriched["token_total"] = _token_total(entry)
            enriched["completed_todo_count"] = _completed_todo_count(entry)
            entries.append(enriched)
        entries.sort(key=lambda item: item.get("run_id", 0), reverse=True)
        capped = entries[: max(1, min(int(limit), 1000))]
        return {"runs": capped}

    @router.get("/api/runs/{run_id}/plan")
    def fetch_run_plan(request: Request, run_id: int):
        engine = request.app.state.engine
        entry = engine._load_run_index().get(str(run_id))
        if not isinstance(entry, dict):
            raise HTTPException(status_code=404, detail="Run not found")
        artifacts = entry.get("artifacts")
        if not isinstance(artifacts, dict):
            raise HTTPException(status_code=404, detail="Plan not found")
        plan_path = artifacts.get("plan_path")
        if not isinstance(plan_path, str) or not plan_path:
            raise HTTPException(status_code=404, detail="Plan not found")
        path = Path(plan_path)
        if not is_within(engine.repo_root, path):
            raise HTTPException(status_code=400, detail="Invalid plan path")
        if not path.exists():
            raise HTTPException(status_code=404, detail="Plan not found")
        return FileResponse(path, media_type="application/json")

    @router.get("/api/runs/{run_id}/diff")
    def fetch_run_diff(request: Request, run_id: int):
        engine = request.app.state.engine
        entry = engine._load_run_index().get(str(run_id))
        if not isinstance(entry, dict):
            raise HTTPException(status_code=404, detail="Run not found")
        artifacts = entry.get("artifacts")
        if not isinstance(artifacts, dict):
            raise HTTPException(status_code=404, detail="Diff not found")
        diff_path = artifacts.get("diff_path")
        if not isinstance(diff_path, str) or not diff_path:
            raise HTTPException(status_code=404, detail="Diff not found")
        path = Path(diff_path)
        if not is_within(engine.repo_root, path):
            raise HTTPException(status_code=400, detail="Invalid diff path")
        if not path.exists():
            raise HTTPException(status_code=404, detail="Diff not found")
        return FileResponse(path, media_type="text/plain")

    @router.get("/api/runs/{run_id}/output")
    def fetch_run_output(request: Request, run_id: int):
        engine = request.app.state.engine
        entry = engine._load_run_index().get(str(run_id))
        if not isinstance(entry, dict):
            raise HTTPException(status_code=404, detail="Run not found")
        artifacts = entry.get("artifacts")
        if not isinstance(artifacts, dict):
            raise HTTPException(status_code=404, detail="Output not found")
        output_path = artifacts.get("output_path")
        if not isinstance(output_path, str) or not output_path:
            raise HTTPException(status_code=404, detail="Output not found")
        path = Path(output_path)
        if not is_within(engine.repo_root, path):
            raise HTTPException(status_code=400, detail="Invalid output path")
        if not path.exists():
            raise HTTPException(status_code=404, detail="Output not found")
        return FileResponse(path, media_type="text/plain")

    @router.get("/api/runs/{run_id}/telemetry")
    def fetch_run_telemetry(request: Request, run_id: int):
        engine = request.app.state.engine
        telemetry = engine._snapshot_run_telemetry(run_id)
        if telemetry is None:
            entry = engine._load_run_index().get(str(run_id))
            if not isinstance(entry, dict):
                raise HTTPException(status_code=404, detail="Run not found")
            token_usage = entry.get("token_usage")
            if isinstance(token_usage, dict):
                delta = token_usage.get("delta")
                thread_total = token_usage.get("thread_total_after")
            else:
                delta = None
                thread_total = None
            return {
                "run_id": run_id,
                "status": "completed",
                "thread_id": None,
                "turn_id": None,
                "token_delta": delta,
                "token_total": thread_total,
                "total_tokens": _extract_total_from_dict(delta),
                "updated_at": None,
            }
        token_total = telemetry.token_total
        total_tokens = _extract_total_from_dict(token_total)
        return {
            "run_id": run_id,
            "status": "active",
            "thread_id": telemetry.thread_id,
            "turn_id": telemetry.turn_id,
            "token_delta": None,
            "token_total": token_total,
            "total_tokens": total_tokens,
            "updated_at": time.time(),
        }

    @router.get("/api/runs/{run_id}/events/stream")
    async def stream_run_events(request: Request, run_id: int):
        engine = request.app.state.engine
        entry = engine._load_run_index().get(str(run_id))
        if not isinstance(entry, dict):
            raise HTTPException(status_code=404, detail="Run not found")
        events_path = engine._events_log_path(run_id)
        shutdown_event = getattr(request.app.state, "shutdown_event", None)
        return StreamingResponse(
            jsonl_event_stream(
                events_path,
                event_name="event",
                shutdown_event=shutdown_event,
            ),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    @router.get("/api/runs/{run_id}/final_review")
    def fetch_final_review(request: Request, run_id: int):
        engine = request.app.state.engine
        entry = engine._load_run_index().get(str(run_id))
        if not isinstance(entry, dict):
            raise HTTPException(status_code=404, detail="Run not found")
        artifacts = entry.get("artifacts")
        if not isinstance(artifacts, dict):
            raise HTTPException(status_code=404, detail="Review not found")
        report_path = artifacts.get("final_review_report_path")
        if not isinstance(report_path, str) or not report_path:
            raise HTTPException(status_code=404, detail="Review not found")
        path = Path(report_path)
        if not is_within(engine.repo_root, path):
            raise HTTPException(status_code=400, detail="Invalid review path")
        if not path.exists():
            raise HTTPException(status_code=404, detail="Review not found")
        media_type = "text/markdown" if path.suffix == ".md" else "text/plain"
        return FileResponse(path, media_type=media_type)

    @router.get("/api/runs/{run_id}/final_review_scratchpad")
    def fetch_final_review_scratchpad(request: Request, run_id: int):
        engine = request.app.state.engine
        entry = engine._load_run_index().get(str(run_id))
        if not isinstance(entry, dict):
            raise HTTPException(status_code=404, detail="Run not found")
        artifacts = entry.get("artifacts")
        if not isinstance(artifacts, dict):
            raise HTTPException(status_code=404, detail="Review scratchpad not found")
        bundle_path = artifacts.get("final_review_scratchpad_bundle_path")
        if not isinstance(bundle_path, str) or not bundle_path:
            raise HTTPException(status_code=404, detail="Review scratchpad not found")
        path = Path(bundle_path)
        if not is_within(engine.repo_root, path):
            raise HTTPException(status_code=400, detail="Invalid scratchpad path")
        if not path.exists():
            raise HTTPException(status_code=404, detail="Review scratchpad not found")
        media_type = (
            "application/zip" if path.suffix == ".zip" else "application/octet-stream"
        )
        return FileResponse(path, media_type=media_type)

    return router
