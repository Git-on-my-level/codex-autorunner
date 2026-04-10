from __future__ import annotations

import sys
from pathlib import Path

import pytest

from codex_autorunner.agents.acp import ACPSubprocessSupervisor

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "fake_acp_server.py"
pytestmark = pytest.mark.slow


def fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FIXTURE_PATH), "--scenario", scenario]


@pytest.mark.asyncio
async def test_supervisor_reuses_workspace_client_and_closes_all(
    tmp_path: Path,
) -> None:
    supervisor = ACPSubprocessSupervisor(fixture_command("official"))
    try:
        client_a = await supervisor.get_client(tmp_path)
        client_b = await supervisor.get_client(tmp_path)
        session = await supervisor.create_session(tmp_path, title="Fixture Session")
        listed = await supervisor.list_sessions(tmp_path)
        snapshot = await supervisor.lifecycle_snapshot()

        assert client_a is client_b
        assert session.session_id == listed[0].session_id
        assert snapshot[0].workspace_root == str(tmp_path.resolve())
        assert snapshot[0].started is True
    finally:
        await supervisor.close_all()
