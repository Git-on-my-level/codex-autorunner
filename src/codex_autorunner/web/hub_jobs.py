from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, Optional

from ..core.logging_utils import log_event
from ..core.state import now_iso


@dataclasses.dataclass
class HubJob:
    job_id: str
    kind: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


class HubJobManager:
    def __init__(
        self,
        *,
        logger: logging.Logger,
        max_jobs: int = 200,
        max_age_seconds: int = 3600,
    ) -> None:
        self._logger = logger
        self._max_jobs = max(10, max_jobs)
        self._max_age_seconds = max(300, max_age_seconds)
        self._jobs: Dict[str, HubJob] = {}
        self._order: Deque[str] = deque()
        try:
            self._lock = asyncio.Lock()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
            self._lock = asyncio.Lock()

    async def submit(
        self,
        kind: str,
        func: Callable[[], Any] | Callable[[], Awaitable[Any]],
        *,
        request_id: Optional[str] = None,
    ) -> HubJob:
        job_id = uuid.uuid4().hex
        job = HubJob(job_id=job_id, kind=kind, status="queued", created_at=now_iso())
        async with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._prune_locked()
        asyncio.create_task(self._run_job(job_id, func, request_id=request_id))
        return job

    async def get(self, job_id: str) -> Optional[HubJob]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def _run_job(
        self,
        job_id: str,
        func: Callable[[], Any] | Callable[[], Awaitable[Any]],
        *,
        request_id: Optional[str],
    ) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = "running"
            job.started_at = now_iso()
        log_event(
            self._logger,
            logging.INFO,
            "hub.job.start",
            job_id=job_id,
            job_kind=job.kind,
            request_id=request_id,
        )
        started = time.monotonic()
        try:
            if asyncio.iscoroutinefunction(func):
                result = await func()
            else:
                result = await asyncio.to_thread(func)
        except Exception as exc:
            duration_ms = (time.monotonic() - started) * 1000
            async with self._lock:
                job = self._jobs.get(job_id)
                if job:
                    job.status = "failed"
                    job.finished_at = now_iso()
                    job.error = str(exc)
            log_event(
                self._logger,
                logging.ERROR,
                "hub.job.finish",
                job_id=job_id,
                job_kind=job.kind if job else "",
                request_id=request_id,
                status="failed",
                duration_ms=round(duration_ms, 2),
                exc=exc,
            )
            return
        duration_ms = (time.monotonic() - started) * 1000
        async with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = "succeeded"
                job.finished_at = now_iso()
                if isinstance(result, dict):
                    job.result = result
        log_event(
            self._logger,
            logging.INFO,
            "hub.job.finish",
            job_id=job_id,
            job_kind=job.kind if job else "",
            request_id=request_id,
            status="succeeded",
            duration_ms=round(duration_ms, 2),
        )

    def _prune_locked(self) -> None:
        now = time.time()
        attempts = 0
        while len(self._jobs) > self._max_jobs and self._order:
            job_id = self._order.popleft()
            job = self._jobs.get(job_id)
            if (
                job
                and job.status in ("queued", "running")
                and attempts < len(self._order)
            ):
                self._order.append(job_id)
                attempts += 1
                continue
            self._jobs.pop(job_id, None)
        stale_ids = []
        for job_id, job in self._jobs.items():
            if (
                job.finished_at
                and now - self._parse_age(job.finished_at) > self._max_age_seconds
            ):
                stale_ids.append(job_id)
        for job_id in stale_ids:
            self._jobs.pop(job_id, None)
            try:
                self._order.remove(job_id)
            except ValueError:
                pass

    def _parse_age(self, iso_ts: str) -> float:
        # Best-effort: treat missing/invalid timestamps as now to avoid mis-pruning.
        try:
            import datetime

            dt = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return time.time()
