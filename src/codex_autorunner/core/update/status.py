from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..update_transaction import write_update_status_projection

__all__ = (
    "StatusReporter",
    "write_update_status_projection",
)


class StatusReporter:
    """Write update status projections with phase timing helpers."""

    def __init__(self, status_path: Path, run_id: str | None = None) -> None:
        self.status_path = status_path
        self.run_id = run_id

    def write(self, status: str, message: str, **kwargs: Any) -> dict[str, Any]:
        phase = kwargs.pop("phase", None)
        error_type = kwargs.pop("error_type", None)
        exit_code = kwargs.pop("exit_code", None)
        extra = kwargs or None
        return write_update_status_projection(
            self.status_path,
            status=status,
            message=message,
            phase=phase,
            error_type=error_type,
            exit_code=exit_code,
            run_id=self.run_id,
            extra=extra,
        )

    def log_phase_timing(
        self,
        phase: str,
        status: str,
        duration_ms: float,
    ) -> dict[str, Any]:
        current_status = "running"
        current_message = "Update running."
        if self.status_path.exists():
            try:
                payload = json.loads(self.status_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    current_status = str(payload.get("status") or current_status)
                    current_message = str(payload.get("message") or current_message)
            except (OSError, json.JSONDecodeError):
                pass

        phase_timing: dict[str, Any] = {
            "phase": phase,
            "status": status,
            "duration_ms": int(duration_ms),
            "at": time.time(),
        }
        if self.run_id:
            phase_timing["update_run_id"] = self.run_id

        return write_update_status_projection(
            self.status_path,
            status=current_status,
            message=current_message,
            run_id=self.run_id,
            extra={"phase_timing": phase_timing},
        )

    @contextmanager
    def timed_phase(self, phase: str) -> Iterator[None]:
        start = time.monotonic()
        try:
            yield
        except Exception:
            duration_ms = (time.monotonic() - start) * 1000
            self.log_phase_timing(phase, "failed", duration_ms)
            raise
        else:
            duration_ms = (time.monotonic() - start) * 1000
            self.log_phase_timing(phase, "ok", duration_ms)
