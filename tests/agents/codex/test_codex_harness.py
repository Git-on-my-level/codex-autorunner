from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.agents.codex.harness import CodexHarness
from codex_autorunner.agents.registry import get_registered_agents


class _TurnHandle:
    def __init__(self, result: object) -> None:
        self._result = result

    async def wait(self, *, timeout: float | None = None) -> object:
        _ = timeout
        return self._result


class _Supervisor:
    def __init__(self, client: object) -> None:
        self._client = client

    async def get_client(self, _workspace_root: Path) -> object:
        return self._client


@pytest.mark.asyncio
async def test_codex_harness_reports_capabilities_from_contract() -> None:
    harness = CodexHarness(supervisor=object(), events=object())  # type: ignore[arg-type]

    report = await harness.runtime_capability_report(Path("."))

    assert harness.capabilities == get_registered_agents()["codex"].capabilities
    assert harness.supports("review") is True
    assert harness.supports("interrupt") is True
    assert harness.supports("approvals") is True
    assert harness.supports("transcript_history") is False
    assert report.capabilities == harness.capabilities


@pytest.mark.asyncio
async def test_codex_harness_wait_for_turn_returns_plain_text_terminal_result() -> None:
    harness = CodexHarness(supervisor=object(), events=object())  # type: ignore[arg-type]
    harness._turn_handles[("thread-1", "turn-1")] = _TurnHandle(  # type: ignore[attr-defined]
        SimpleNamespace(
            status="completed",
            final_message="fallback message",
            agent_messages=["first line", "second line"],
            errors=[],
            raw_events=[{"method": "message.completed"}],
        )
    )

    result = await harness.wait_for_turn(Path("."), "thread-1", "turn-1")

    assert result.status == "completed"
    assert result.assistant_text == "first line\nsecond line"
    assert result.errors == []
    assert result.raw_events == [{"method": "message.completed"}]
    assert ("thread-1", "turn-1") not in harness._turn_handles  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_codex_harness_resume_conversation_propagates_resume_failures() -> None:
    class _Client:
        async def thread_resume(self, _thread_id: str) -> None:
            raise RuntimeError("missing thread")

    harness = CodexHarness(supervisor=_Supervisor(_Client()), events=object())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="missing thread"):
        await harness.resume_conversation(Path("."), "thread-1")
