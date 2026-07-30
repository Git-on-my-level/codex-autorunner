from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from codex_autorunner.agents.acp import ACPSubprocessSupervisor
from codex_autorunner.agents.acp.client import ACPClientDiagnostics

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "fake_acp_server.py"
pytestmark = pytest.mark.slow


def fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FIXTURE_PATH), "--scenario", scenario]


class _StubDiagnosticsClient:
    """A fake ACPClient double used to prove the supervisor only ever reads
    the public `diagnostics()` accessor (and `initialize_result`), never the
    client's private `_process`/`_prompts` attributes."""

    def __init__(
        self,
        *,
        started: bool,
        diagnostics: ACPClientDiagnostics,
    ) -> None:
        self._started = started
        self._diagnostics_snapshot = diagnostics

    @property
    def initialize_result(self) -> Optional[object]:
        return object() if self._started else None

    def diagnostics(self) -> ACPClientDiagnostics:
        return self._diagnostics_snapshot

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _install_stub_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    started: bool,
    diagnostics: ACPClientDiagnostics,
) -> None:
    def _factory(*args: Any, **kwargs: Any) -> _StubDiagnosticsClient:
        return _StubDiagnosticsClient(started=started, diagnostics=diagnostics)

    monkeypatch.setattr(
        "codex_autorunner.agents.acp.supervisor.ACPClient",
        _factory,
    )


@pytest.mark.asyncio
async def test_lifecycle_snapshot_maps_diagnostics_for_running_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub_client(
        monkeypatch,
        started=True,
        diagnostics=ACPClientDiagnostics(
            pid=4321, pgid=4321, returncode=None, active_prompts=2
        ),
    )
    supervisor = ACPSubprocessSupervisor(["fake-command"])

    await supervisor.get_client(tmp_path)
    snapshot = await supervisor.lifecycle_snapshot()

    assert len(snapshot) == 1
    entry = snapshot[0]
    assert entry.pid == 4321
    assert entry.pgid == 4321
    assert entry.active_prompts == 2
    assert entry.started is True
    assert entry.healthy is True


@pytest.mark.asyncio
async def test_lifecycle_snapshot_reports_unhealthy_for_exited_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub_client(
        monkeypatch,
        started=True,
        diagnostics=ACPClientDiagnostics(
            pid=None, pgid=None, returncode=1, active_prompts=0
        ),
    )
    supervisor = ACPSubprocessSupervisor(["fake-command"])

    await supervisor.get_client(tmp_path)
    snapshot = await supervisor.lifecycle_snapshot()

    entry = snapshot[0]
    assert entry.pid is None
    assert entry.pgid is None
    assert entry.active_prompts == 0
    assert entry.started is True
    assert entry.healthy is False


@pytest.mark.asyncio
async def test_lifecycle_snapshot_reports_not_started_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub_client(
        monkeypatch,
        started=False,
        diagnostics=ACPClientDiagnostics(
            pid=None, pgid=None, returncode=None, active_prompts=0
        ),
    )
    supervisor = ACPSubprocessSupervisor(["fake-command"])

    await supervisor.get_client(tmp_path)
    snapshot = await supervisor.lifecycle_snapshot()

    entry = snapshot[0]
    assert entry.pid is None
    assert entry.pgid is None
    assert entry.active_prompts == 0
    assert entry.started is False
    assert entry.healthy is False


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
        assert snapshot[0].runtime_kind == "acp"
        assert snapshot[0].server_scope == "workspace"
        assert snapshot[0].handle_id == str(tmp_path.resolve())
        assert snapshot[0].workspace_root == str(tmp_path.resolve())
        assert snapshot[0].pid is not None
        assert snapshot[0].base_url is None
        assert snapshot[0].started is True
        assert snapshot[0].healthy is True
    finally:
        await supervisor.close_all()
