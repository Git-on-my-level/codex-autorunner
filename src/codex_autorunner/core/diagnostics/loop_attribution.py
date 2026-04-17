from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class LoopWakeupCounters:
    wakeups: int = 0
    productive_wakeups: int = 0
    idle_wakeups: int = 0
    disk_reads: int = 0
    db_reads: int = 0
    total_wakeup_duration_seconds: float = 0.0


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, LoopWakeupCounters] = {}


def _ensure_counters(loop_name: str) -> LoopWakeupCounters:
    with _REGISTRY_LOCK:
        counters = _REGISTRY.get(loop_name)
        if counters is None:
            counters = LoopWakeupCounters()
            _REGISTRY[loop_name] = counters
        return counters


class WakeupScope:
    __slots__ = ("_counters", "_productive", "_disk_reads", "_db_reads", "_start")

    def __init__(self, counters: LoopWakeupCounters) -> None:
        self._counters = counters
        self._productive = False
        self._disk_reads = 0
        self._db_reads = 0
        self._start = 0.0

    def mark_productive(self) -> None:
        self._productive = True

    def record_disk_read(self, count: int = 1) -> None:
        self._disk_reads += count

    def record_db_read(self, count: int = 1) -> None:
        self._db_reads += count

    def __enter__(self) -> WakeupScope:
        self._start = time.monotonic()
        return self

    def __exit__(self, *args: Any) -> None:
        elapsed = time.monotonic() - self._start
        c = self._counters
        c.wakeups += 1
        c.total_wakeup_duration_seconds += elapsed
        if self._productive:
            c.productive_wakeups += 1
        else:
            c.idle_wakeups += 1
        c.disk_reads += self._disk_reads
        c.db_reads += self._db_reads


def track_loop(loop_name: str) -> WakeupScope:
    return WakeupScope(_ensure_counters(loop_name))


def snapshot_loop_attribution() -> dict[str, Any]:
    with _REGISTRY_LOCK:
        items: dict[str, dict[str, Any]] = {}
        for name, counters in sorted(_REGISTRY.items()):
            items[name] = {
                "wakeups": counters.wakeups,
                "productive_wakeups": counters.productive_wakeups,
                "idle_wakeups": counters.idle_wakeups,
                "disk_reads": counters.disk_reads,
                "db_reads": counters.db_reads,
                "total_wakeup_duration_seconds": round(
                    counters.total_wakeup_duration_seconds, 6
                ),
                "avg_wakeup_duration_seconds": (
                    round(counters.total_wakeup_duration_seconds / counters.wakeups, 6)
                    if counters.wakeups > 0
                    else 0.0
                ),
            }
    return {"loops": items, "snapshot_at": time.time()}


def reset_loop_attribution() -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def get_loop_names() -> list[str]:
    with _REGISTRY_LOCK:
        return sorted(_REGISTRY.keys())


__all__ = [
    "LoopWakeupCounters",
    "WakeupScope",
    "get_loop_names",
    "reset_loop_attribution",
    "snapshot_loop_attribution",
    "track_loop",
]
