from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from codex_autorunner.agents.opencode.run_prompt import (
    OpenCodeRunConfig,
    run_opencode_prompt,
)
from codex_autorunner.agents.opencode.runtime import OpenCodeTurnOutput


class _ClientStub:
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self.dispose_calls: list[str] = []

    async def create_session(
        self, *, directory: Optional[str] = None
    ) -> dict[str, str]:
        _ = directory
        return {"sessionId": self._session_id}

    async def prompt_async(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def abort(self, _session_id: str) -> None:
        return None

    async def dispose(self, session_id: str) -> None:
        self.dispose_calls.append(session_id)


class _SupervisorStub:
    def __init__(self, client: _ClientStub) -> None:
        self._client = client
        self.started: list[Path] = []
        self.finished: list[Path] = []

    @property
    def session_stall_timeout_seconds(self) -> Optional[float]:
        return None

    async def get_client(self, _workspace_root: Path) -> _ClientStub:
        return self._client

    async def mark_turn_started(self, workspace_root: Path) -> None:
        self.started.append(workspace_root)

    async def mark_turn_finished(self, workspace_root: Path) -> None:
        self.finished.append(workspace_root)


@pytest.mark.anyio
async def test_run_opencode_prompt_disposes_temporary_session_after_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from codex_autorunner.agents.opencode import run_prompt as run_prompt_module

    client = _ClientStub("session-1")
    supervisor = _SupervisorStub(client)

    async def _fake_collect(*_args: Any, **_kwargs: Any) -> OpenCodeTurnOutput:
        ready_event = _kwargs.get("ready_event")
        if ready_event is not None:
            ready_event.set()
        return OpenCodeTurnOutput(text="done")

    async def _fake_missing_env(*_args: Any, **_kwargs: Any) -> list[str]:
        return []

    monkeypatch.setattr(run_prompt_module, "collect_opencode_output", _fake_collect)
    monkeypatch.setattr(run_prompt_module, "opencode_missing_env", _fake_missing_env)
    monkeypatch.setattr(
        run_prompt_module, "build_turn_id", lambda session_id: f"{session_id}:turn"
    )

    result = await run_opencode_prompt(
        supervisor,  # type: ignore[arg-type]
        OpenCodeRunConfig(
            agent="opencode",
            model=None,
            reasoning=None,
            prompt="hello",
            workspace_root=str(tmp_path),
            timeout_seconds=5,
        ),
    )

    assert result.session_id == "session-1"
    assert result.turn_id == "session-1:turn"
    assert result.output_text == "done"
    assert client.dispose_calls == ["session-1"]
    assert supervisor.started == [tmp_path]
    assert supervisor.finished == [tmp_path]


@pytest.mark.anyio
async def test_run_opencode_prompt_disposes_session_when_env_check_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from codex_autorunner.agents.opencode import run_prompt as run_prompt_module

    client = _ClientStub("session-2")
    supervisor = _SupervisorStub(client)

    async def _fake_missing_env(*_args: Any, **_kwargs: Any) -> list[str]:
        return ["OPENAI_API_KEY"]

    monkeypatch.setattr(run_prompt_module, "opencode_missing_env", _fake_missing_env)

    with pytest.raises(RuntimeError, match="requires env vars"):
        await run_opencode_prompt(
            supervisor,  # type: ignore[arg-type]
            OpenCodeRunConfig(
                agent="opencode",
                model="openai/gpt-4.1",
                reasoning=None,
                prompt="hello",
                workspace_root=str(tmp_path),
                timeout_seconds=5,
            ),
        )

    assert client.dispose_calls == ["session-2"]
