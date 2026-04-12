from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

import pytest

from codex_autorunner.agents.opencode.run_prompt import (
    OpenCodeRunConfig,
    _abort_session,
    _apply_prompt_fallback,
    _collect_output_after_interrupt,
    _create_session,
    _dispose_session,
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


class _AbortClientStub:
    def __init__(self) -> None:
        self.aborts: list[tuple[str, str]] = []

    async def abort(self, session_id: str) -> None:
        self.aborts.append((session_id, "ok"))


class _DisposeClientStub:
    def __init__(self) -> None:
        self.disposed: list[str] = []

    async def dispose(self, session_id: str) -> None:
        self.disposed.append(session_id)


class _FailingDisposeClientStub:
    async def dispose(self, _session_id: str) -> None:
        raise RuntimeError("dispose broken")


class _FailingAbortClientStub:
    async def abort(self, _session_id: str) -> None:
        raise ProcessLookupError("no process")


class _CreateSessionClientStub:
    def __init__(self, session_id: str) -> None:
        self._sid = session_id

    async def create_session(self, *, directory: str = "") -> dict[str, str]:
        return {"sessionId": self._sid}


@pytest.mark.anyio
async def test_create_session_returns_valid_id() -> None:
    client = _CreateSessionClientStub("s-abc")
    sid = await _create_session(client, "/tmp", None)
    assert sid == "s-abc"


@pytest.mark.anyio
async def test_create_session_raises_on_missing_id() -> None:
    client = _CreateSessionClientStub.__new__(_CreateSessionClientStub)
    client._sid = ""

    async def _empty_session(self: Any, **_kw: Any) -> dict[str, str]:
        return {"sessionId": ""}

    import types

    client.create_session = types.MethodType(_empty_session, client)
    with pytest.raises(ValueError, match="did not return a session id"):
        await _create_session(client, "/tmp", None)


@pytest.mark.anyio
async def test_dispose_session_succeeds() -> None:
    client = _DisposeClientStub()
    await _dispose_session(client, "s-1", "/tmp", None)
    assert client.disposed == ["s-1"]


@pytest.mark.anyio
async def test_dispose_session_logs_failure_without_raising() -> None:
    client = _FailingDisposeClientStub()
    await _dispose_session(client, "s-1", "/tmp", None)


@pytest.mark.anyio
async def test_abort_session_succeeds() -> None:
    client = _AbortClientStub()
    await _abort_session(client, "s-1", "stop", None)
    assert client.aborts == [("s-1", "ok")]


@pytest.mark.anyio
async def test_abort_session_swallows_failure() -> None:
    client = _FailingAbortClientStub()
    await _abort_session(client, "s-1", "timeout", None)


@pytest.mark.anyio
async def test_collect_output_after_interrupt_returns_done_result() -> None:
    result = OpenCodeTurnOutput(text="hi")

    async def _produce() -> OpenCodeTurnOutput:
        return result

    task = asyncio.create_task(_produce())
    await asyncio.sleep(0)
    assert task.done()
    out = await _collect_output_after_interrupt(
        task, grace_seconds=0, ignore_errors=False, logger=None
    )
    assert out is not None
    assert out.text == "hi"


@pytest.mark.anyio
async def test_collect_output_after_interrupt_cancels_with_zero_grace() -> None:
    blocker = asyncio.Event()

    async def _hang() -> OpenCodeTurnOutput:
        await blocker.wait()
        return OpenCodeTurnOutput(text="never")

    task = asyncio.create_task(_hang())
    out = await _collect_output_after_interrupt(
        task, grace_seconds=0, ignore_errors=False, logger=None
    )
    assert out is None
    assert task.cancelled()


@pytest.mark.anyio
async def test_collect_output_after_interrupt_waits_grace_then_cancels() -> None:
    async def _hang() -> OpenCodeTurnOutput:
        await asyncio.sleep(10)
        return OpenCodeTurnOutput(text="never")

    task = asyncio.create_task(_hang())
    out = await _collect_output_after_interrupt(
        task, grace_seconds=0, ignore_errors=True, logger=None
    )
    assert out is None


@pytest.mark.anyio
async def test_collect_output_after_interrupt_ignores_errors() -> None:
    async def _fail() -> OpenCodeTurnOutput:
        raise RuntimeError("boom")

    task = asyncio.create_task(_fail())
    out = await _collect_output_after_interrupt(
        task, grace_seconds=5, ignore_errors=True, logger=None
    )
    assert out is None


@pytest.mark.anyio
async def test_collect_output_after_interrupt_propagates_errors() -> None:
    async def _fail() -> OpenCodeTurnOutput:
        raise RuntimeError("boom")

    task = asyncio.create_task(_fail())
    with pytest.raises(RuntimeError, match="boom"):
        await _collect_output_after_interrupt(
            task, grace_seconds=5, ignore_errors=False, logger=None
        )


@pytest.mark.anyio
async def test_apply_prompt_fallback_keeps_existing_text() -> None:
    async def _ok() -> dict[str, Any]:
        return {"text": "from prompt"}

    task = asyncio.create_task(_ok())
    await task
    text, error = _apply_prompt_fallback("existing", None, task)
    assert text == "existing"


@pytest.mark.anyio
async def test_apply_prompt_fallback_uses_prompt_text_when_non_echo() -> None:
    async def _ok() -> dict[str, Any]:
        return {"text": "from prompt"}

    task = asyncio.create_task(_ok())
    await task
    text, error = _apply_prompt_fallback("", None, task, prompt="different prompt")
    assert text == "from prompt"


@pytest.mark.anyio
async def test_apply_prompt_fallback_ignores_prompt_echo_when_empty() -> None:
    async def _ok() -> dict[str, Any]:
        return {"parts": [{"type": "text", "text": "repeat me"}]}

    task = asyncio.create_task(_ok())
    await task
    text, error = _apply_prompt_fallback("", None, task, prompt="repeat me")
    assert text == ""
    assert error is None


@pytest.mark.anyio
async def test_apply_prompt_fallback_preserves_first_error() -> None:
    async def _ok() -> dict[str, Any]:
        return {}

    task = asyncio.create_task(_ok())
    await task
    text, error = _apply_prompt_fallback("", "first-error", task)
    assert error == "first-error"


@pytest.mark.anyio
async def test_apply_prompt_fallback_handles_failed_task() -> None:
    async def _fail() -> dict[str, Any]:
        raise RuntimeError("fail")

    task = asyncio.create_task(_fail())
    await asyncio.sleep(0)
    text, error = _apply_prompt_fallback("", None, task)
    assert text == ""
    assert error is None


@pytest.mark.anyio
async def test_apply_prompt_fallback_handles_none_response() -> None:
    async def _none() -> None:
        return None

    task = asyncio.create_task(_none())
    await task
    text, error = _apply_prompt_fallback("", None, task)
    assert text == ""
