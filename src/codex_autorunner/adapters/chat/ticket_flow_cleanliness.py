from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from ...core.archive import dirty_car_state_paths

_PREVIOUS_RUN_KEYS = frozenset({"runs", "flows", "flows.db"})
_RELEVANT_KEYS = frozenset({"tickets", "contextspace", *_PREVIOUS_RUN_KEYS})
TicketFlowCleanlinessState = Literal["clean", "dirty", "unknown"]


@dataclass(frozen=True)
class TicketFlowCleanliness:
    state: TicketFlowCleanlinessState
    labels: tuple[str, ...] = ()
    dirty_keys: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        return self.state == "clean"

    @property
    def is_dirty(self) -> bool:
        return self.state == "dirty"

    @property
    def line(self) -> str:
        if self.state == "unknown":
            return "Ticket flow: Unknown"
        if self.state == "clean":
            return "Ticket flow: Clean"
        return f"Ticket flow: Dirty ({', '.join(self.labels)})"


def get_ticket_flow_cleanliness(workspace_root: Optional[Any]) -> TicketFlowCleanliness:
    """Return compact live ticket-flow cleanliness state for chat surfaces."""
    if workspace_root is None:
        return TicketFlowCleanliness("unknown")
    root = Path(workspace_root)
    try:
        dirty_keys = set(dirty_car_state_paths(root))
    except (OSError, RuntimeError, ValueError):
        return TicketFlowCleanliness("unknown")

    relevant_dirty = dirty_keys & _RELEVANT_KEYS
    if relevant_dirty & _PREVIOUS_RUN_KEYS and not _has_previous_run_state(root):
        relevant_dirty -= _PREVIOUS_RUN_KEYS
    if not relevant_dirty:
        return TicketFlowCleanliness("clean")

    labels: list[str] = []
    if "tickets" in relevant_dirty:
        labels.append("tickets")
    if "contextspace" in relevant_dirty:
        labels.append("contextspace")
    if relevant_dirty & _PREVIOUS_RUN_KEYS:
        labels.append("previous runs")
    return TicketFlowCleanliness(
        "dirty",
        labels=tuple(labels),
        dirty_keys=tuple(sorted(relevant_dirty)),
    )


def _has_previous_run_state(workspace_root: Path) -> bool:
    car_root = workspace_root / ".codex-autorunner"
    for key in ("runs", "flows"):
        path = car_root / key
        if path.exists() and path.is_dir():
            try:
                next(path.iterdir())
            except StopIteration:
                pass
            except OSError:
                return True
            else:
                return True
    db_path = car_root / "flows.db"
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute("SELECT COUNT(*) FROM flow_runs").fetchone()
    except sqlite3.Error:
        return True
    count = row[0] if row else 0
    return isinstance(count, int) and count > 0


__all__ = [
    "TicketFlowCleanliness",
    "TicketFlowCleanlinessState",
    "get_ticket_flow_cleanliness",
]
