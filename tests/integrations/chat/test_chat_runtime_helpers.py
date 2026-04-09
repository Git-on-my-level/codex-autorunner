from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from codex_autorunner.core.orchestration import WorkspaceRuntimeAcquisition
from codex_autorunner.integrations.chat.runtime import (
    acquire_chat_workspace_runtime,
    resolve_chat_thread_runtime_binding,
)


class _RuntimeService:
    def __init__(self, runtime_instance_id: Optional[str]) -> None:
        self.runtime_instance_id = runtime_instance_id
        self.calls: list[tuple[str, Path]] = []
        self.harness = object()

    async def acquire_workspace_runtime(
        self, agent_id: str, workspace_root: Path
    ) -> WorkspaceRuntimeAcquisition:
        self.calls.append((agent_id, workspace_root))
        return WorkspaceRuntimeAcquisition(
            harness=self.harness,
            backend_runtime_instance_id=self.runtime_instance_id,
        )


@pytest.mark.anyio
async def test_acquire_chat_workspace_runtime_uses_orchestration_contract(
    tmp_path: Path,
) -> None:
    service = _RuntimeService("runtime-1")

    acquired = await acquire_chat_workspace_runtime(
        service,
        agent_id="codex",
        workspace_root=tmp_path,
    )

    assert acquired.harness is service.harness
    assert acquired.backend_runtime_instance_id == "runtime-1"
    assert service.calls == [("codex", tmp_path)]


@pytest.mark.anyio
async def test_resolve_chat_thread_runtime_binding_reuses_existing_binding_when_runtime_missing(
    tmp_path: Path,
) -> None:
    service = _RuntimeService(None)
    existing_thread = SimpleNamespace(
        agent_id="opencode",
        agent_profile=None,
        workspace_root=str(tmp_path.resolve()),
        backend_thread_id="backend-old",
    )

    binding = await resolve_chat_thread_runtime_binding(
        service,
        agent_id="opencode",
        workspace_root=tmp_path,
        requested_backend_thread_id="backend-new",
        existing_thread=existing_thread,
    )

    assert binding.backend_thread_id == "backend-old"
    assert binding.backend_runtime_instance_id is None
    assert binding.runtime_available is False
    assert binding.used_requested_backend_thread_id is False


@pytest.mark.anyio
async def test_resolve_chat_thread_runtime_binding_keeps_requested_binding_for_nonmatching_thread(
    tmp_path: Path,
) -> None:
    service = _RuntimeService(None)
    existing_thread = SimpleNamespace(
        agent_id="opencode",
        agent_profile=None,
        workspace_root=str((tmp_path / "other").resolve()),
        backend_thread_id="backend-old",
    )

    binding = await resolve_chat_thread_runtime_binding(
        service,
        agent_id="opencode",
        workspace_root=tmp_path,
        requested_backend_thread_id="backend-new",
        existing_thread=existing_thread,
    )

    assert binding.backend_thread_id == "backend-new"
    assert binding.backend_runtime_instance_id is None
    assert binding.runtime_available is False
    assert binding.used_requested_backend_thread_id is True
