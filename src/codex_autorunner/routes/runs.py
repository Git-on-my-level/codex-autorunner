"""
Run telemetry routes.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..core.utils import is_within


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

    return router
