"""OpenCode supervisor handle-eviction behavior.

Regression tests: eviction driven by ``max_handles`` must never pick a
handle that has active turns in flight. Evicting a busy handle terminates
its managed OpenCode server mid-turn and surfaces as
``Cannot send a request, as the client has been closed.``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from codex_autorunner.agents.opencode.supervisor import (
    OpenCodeHandle,
    OpenCodeSupervisor,
)


def _make_handle(
    workspace_id: str, *, active_turns: int = 0, last_used_at: float = 0.0
) -> OpenCodeHandle:
    return OpenCodeHandle(
        workspace_id=workspace_id,
        workspace_root=Path(f"/tmp/opencode-{workspace_id}"),
        process=None,
        client=None,
        managed_process_record=None,
        base_url=None,
        health_info=None,
        version=None,
        openapi_spec=None,
        start_lock=asyncio.Lock(),
        stdout_task=None,
        last_used_at=last_used_at,
        active_turns=active_turns,
    )


def _make_supervisor(max_handles: int) -> OpenCodeSupervisor:
    return OpenCodeSupervisor(
        command=["opencode", "serve"],
        logger=logging.getLogger("test.opencode-eviction"),
        max_handles=max_handles,
    )


class TestEvictLRUHandle:
    def test_idle_handle_evicted_when_at_capacity(self) -> None:
        supervisor = _make_supervisor(max_handles=1)
        supervisor._handles["ws-a"] = _make_handle("ws-a", last_used_at=10.0)

        evicted = supervisor._evict_lru_handle_locked()

        assert evicted is not None
        assert evicted.workspace_id == "ws-a"

    def test_busy_handle_is_never_evicted(self) -> None:
        supervisor = _make_supervisor(max_handles=1)
        supervisor._handles["ws-a"] = _make_handle(
            "ws-a", active_turns=1, last_used_at=10.0
        )

        evicted = supervisor._evict_lru_handle_locked()

        assert evicted is None
        assert "ws-a" in supervisor._handles

    def test_busiest_workspace_survives_when_only_candidate_is_busy(self) -> None:
        supervisor = _make_supervisor(max_handles=1)
        # ws-a is older but busy; ws-b is newer but idle.
        supervisor._handles["ws-a"] = _make_handle(
            "ws-a", active_turns=2, last_used_at=1.0
        )
        supervisor._handles["ws-b"] = _make_handle(
            "ws-b", active_turns=0, last_used_at=100.0
        )

        evicted = supervisor._evict_lru_handle_locked()

        # At capacity, only ws-b is evictable; busy ws-a must survive.
        assert evicted is not None
        assert evicted.workspace_id == "ws-b"
        assert "ws-a" in supervisor._handles

    def test_eviction_skipped_logs_when_all_handles_busy(self) -> None:
        supervisor = _make_supervisor(max_handles=1)
        supervisor._handles["ws-a"] = _make_handle(
            "ws-a", active_turns=1, last_used_at=10.0
        )

        assert supervisor._evict_lru_handle_locked() is None
