from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.agents.opencode.runtime import OpenCodeTurnOutput
from codex_autorunner.core.ports.run_event import Completed
from codex_autorunner.integrations.agents.opencode_backend import OpenCodeBackend


class _SessionClientStub:
    async def create_session(self, *, title: str, directory: str) -> dict[str, str]:
        _ = title, directory
        return {"sessionId": "session-1"}

    async def dispose(self, session_id: str) -> None:
        _ = session_id

    async def prompt_async(
        self, *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        return {}

    async def providers(self, directory=None):
        _ = directory
        return {}


@pytest.mark.anyio
async def test_opencode_backend_uses_approval_policy_default_when_not_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from codex_autorunner.integrations.agents import opencode_backend as backend_module

    backend = OpenCodeBackend(base_url="http://localhost:8080", workspace_root=tmp_path)
    backend._client = _SessionClientStub()
    backend.configure(approval_policy=None, approval_policy_default="never")

    observed: dict[str, object] = {}

    async def _fake_collect(*_args: object, **kwargs: object) -> OpenCodeTurnOutput:
        observed["permission_policy"] = kwargs.get("permission_policy")
        ready_event = kwargs.get("ready_event")
        if ready_event is not None:
            ready_event.set()
        return OpenCodeTurnOutput(text="done")

    async def _fake_missing_env(*_args: object, **_kwargs: object) -> list[str]:
        return []

    monkeypatch.setattr(backend_module, "collect_opencode_output", _fake_collect)
    monkeypatch.setattr(backend_module, "opencode_missing_env", _fake_missing_env)
    monkeypatch.setattr(
        backend_module, "build_turn_id", lambda session_id: f"{session_id}:turn"
    )

    events = [event async for event in backend.run_turn_events("", "hello")]

    assert isinstance(events[-1], Completed)
    assert observed["permission_policy"] == "allow"


def test_map_approval_policy_to_permission_keeps_never_as_allow() -> None:
    from codex_autorunner.agents.opencode.runtime import (
        map_approval_policy_to_permission,
    )

    assert map_approval_policy_to_permission("never", default="allow") == "allow"
