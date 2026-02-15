import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.integrations.app_server.supervisor import (
    WorkspaceAppServerSupervisor,
)
from codex_autorunner.workspace import canonical_workspace_root, workspace_id_for_path


@pytest.mark.anyio
async def test_get_client_touches_handle_before_prune(tmp_path: Path) -> None:
    def env_builder(
        _workspace_root: Path, _workspace_id: str, _state_dir: Path
    ) -> dict:
        return {}

    supervisor = WorkspaceAppServerSupervisor(
        [sys.executable, "-c", "print('noop')"],
        state_root=tmp_path,
        env_builder=env_builder,
        idle_ttl_seconds=1,
    )
    canonical_root = canonical_workspace_root(tmp_path)
    workspace_id = workspace_id_for_path(canonical_root)
    handle = await supervisor._ensure_handle(workspace_id, canonical_root)
    handle.last_used_at = time.monotonic() - 10

    started_event = asyncio.Event()
    release_event = asyncio.Event()

    async def hold_start(_handle) -> None:
        started_event.set()
        await release_event.wait()

    async def no_op_close() -> None:
        return None

    supervisor._ensure_started = hold_start  # type: ignore[assignment]
    handle.client.close = no_op_close  # type: ignore[assignment]

    get_task = asyncio.create_task(supervisor.get_client(tmp_path))
    await started_event.wait()

    closed = await supervisor.prune_idle()
    release_event.set()
    await get_task

    assert closed == 0
    assert workspace_id in supervisor._handles


@pytest.mark.anyio
async def test_get_client_same_workspace_reuses_single_client_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient:
        instances: list["FakeClient"] = []

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.start_calls = 0
            self.close_calls = 0
            FakeClient.instances.append(self)

        async def start(self) -> None:
            self.start_calls += 1

        async def close(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr(
        "codex_autorunner.integrations.app_server.supervisor.CodexAppServerClient",
        FakeClient,
    )

    def env_builder(
        _workspace_root: Path, _workspace_id: str, _state_dir: Path
    ) -> dict[str, str]:
        return {}

    supervisor = WorkspaceAppServerSupervisor(
        [sys.executable, "-c", "print('noop')"],
        state_root=tmp_path,
        env_builder=env_builder,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    client_one = await supervisor.get_client(workspace)
    client_two = await supervisor.get_client(workspace)

    assert client_one is client_two
    assert len(FakeClient.instances) == 1
    assert FakeClient.instances[0].start_calls == 1
