"""Binary-gated live smoke test for the OMP ACP agent.

Runs the harness end-to-end against a real ``omp acp`` subprocess. Gated on the
``omp`` binary and marked ``integration`` so it never runs in the fast lane.

Note: omp loads its model registry asynchronously and, in some CI/pytest
environments, never finishes that load (so session/prompt returns an internal
error). When omp itself is degraded this test SKIPS rather than fails — the CAR
wire path itself is covered hermetically by tests/agents/omp/test_omp_supervisor.py
and the shared acp suite.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codex_autorunner.agents.acp.errors import ACPResponseError
from codex_autorunner.agents.omp import OMPHarness, OMPSupervisor

_OMP = shutil.which("omp")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_OMP is None, reason="omp binary not found"),
]


@pytest.mark.asyncio
async def test_omp_live_end_to_end_turn(tmp_path: Path) -> None:
    supervisor = OMPSupervisor([_OMP, "acp"], request_timeout=120.0)
    harness = OMPHarness(supervisor)
    try:
        await harness.ensure_ready(tmp_path)
        conversation = await harness.new_conversation(tmp_path, title="omp-live-smoke")
        turn = await harness.start_turn(
            tmp_path,
            conversation.id,
            "Reply with exactly the single word PONG and nothing else. Do not use any tools.",
            model=None,
            reasoning=None,
            approval_mode=None,
            sandbox_policy=None,
        )
        try:
            result = await harness.wait_for_turn(
                tmp_path, conversation.id, turn.turn_id, timeout=150
            )
        except ACPResponseError as exc:
            pytest.skip(
                f"omp could not run a turn in this environment ({exc}); "
                "model registry likely unavailable"
            )
        assert result.status == "completed"
        assert result.errors == []
        assert "PONG" in (result.assistant_text or "").upper()
    finally:
        await supervisor.close_all()
