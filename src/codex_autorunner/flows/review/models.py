from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from ...core.state import now_iso


class ReviewStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


ACTIVE_REVIEW_STATUSES = frozenset({ReviewStatus.RUNNING, ReviewStatus.STOPPING})


class ReviewPromptKind(str, Enum):
    CODE = "code"
    SPEC_PROGRESS = "spec_progress"


class ReviewState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int = 1
    id: Optional[str] = None
    status: ReviewStatus = ReviewStatus.IDLE
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    max_wallclock_seconds: Optional[int] = None
    run_dir: Optional[str] = None
    scratchpad_dir: Optional[str] = None
    final_output_path: Optional[str] = None
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    stop_requested: bool = False
    worker_id: Optional[str] = None
    worker_pid: Optional[int] = None
    worker_started_at: Optional[str] = None
    started_at: Optional[str] = None
    updated_at: Optional[str] = None
    finished_at: Optional[str] = None
    scratchpad_bundle_path: Optional[str] = None
    last_error: Optional[str] = None
    prompt_kind: ReviewPromptKind = ReviewPromptKind.CODE

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: object) -> ReviewStatus:
        if isinstance(value, ReviewStatus):
            return value
        raw = str(value or "").strip().lower()
        if not raw:
            return ReviewStatus.IDLE
        try:
            return ReviewStatus(raw)
        except ValueError as exc:
            raise ValueError(f"Unknown review status: {raw}") from exc

    @field_validator("prompt_kind", mode="before")
    @classmethod
    def _normalize_prompt_kind(cls, value: object) -> ReviewPromptKind:
        if isinstance(value, ReviewPromptKind):
            return value
        raw = str(value or "").strip().lower()
        if not raw:
            return ReviewPromptKind.CODE
        try:
            return ReviewPromptKind(raw)
        except ValueError:
            return ReviewPromptKind.CODE

    def persist_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def with_worker(
        self, *, worker_id: str, worker_pid: int, worker_started_at: str
    ) -> "ReviewState":
        return self.model_copy(
            update={
                "worker_id": worker_id,
                "worker_pid": worker_pid,
                "worker_started_at": worker_started_at,
            }
        )

    def with_session(self, *, session_id: str, turn_id: str) -> "ReviewState":
        return self.model_copy(
            update={
                "session_id": session_id,
                "turn_id": turn_id,
                "updated_at": now_iso(),
            }
        )

    def with_runtime(self, *, running: bool) -> "ReviewStateSnapshot":
        return ReviewStateSnapshot.model_validate(
            {**self.persist_payload(), "running": running}
        )


class ReviewStateSnapshot(ReviewState):
    running: bool = False
