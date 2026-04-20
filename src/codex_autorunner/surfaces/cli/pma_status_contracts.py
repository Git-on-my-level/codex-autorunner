"""Typed contracts for PMA status payloads used by non-thread CLI commands.

These dataclasses replace phantom dict reads with explicit typed shapes,
providing clear field contracts for PMA API responses consumed by the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class PmaTurnSnapshot:
    status: str
    agent: str
    started_at: str
    finished_at: str

    @classmethod
    def from_dict(cls, data: Any) -> Optional["PmaTurnSnapshot"]:
        if not isinstance(data, dict) or not data:
            return None
        return cls(
            status=str(data.get("status") or "unknown"),
            agent=str(data.get("agent") or "unknown"),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
        )


@dataclass(frozen=True)
class PmaActiveResponse:
    active: bool
    current: Optional[PmaTurnSnapshot]
    last_result: Optional[PmaTurnSnapshot]

    @classmethod
    def from_dict(cls, data: Any) -> "PmaActiveResponse":
        if not isinstance(data, dict):
            data = {}
        return cls(
            active=bool(data.get("active")),
            current=PmaTurnSnapshot.from_dict(data.get("current")),
            last_result=PmaTurnSnapshot.from_dict(data.get("last_result")),
        )


@dataclass(frozen=True)
class PmaInterruptResponse:
    interrupted: bool
    detail: str
    agent: str

    @classmethod
    def from_dict(cls, data: Any) -> "PmaInterruptResponse":
        if not isinstance(data, dict):
            data = {}
        return cls(
            interrupted=bool(data.get("interrupted")),
            detail=str(data.get("detail") or ""),
            agent=str(data.get("agent") or ""),
        )


@dataclass(frozen=True)
class PmaResetResponse:
    cleared: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: Any) -> "PmaResetResponse":
        if not isinstance(data, dict):
            data = {}
        raw_cleared = data.get("cleared")
        return cls(
            cleared=tuple(raw_cleared) if isinstance(raw_cleared, list) else (),
        )
