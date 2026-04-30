from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import codex_autorunner.agents.registry as agent_registry_module
from codex_autorunner.agents.registry import AgentDescriptor


class _BaseTelegramFakeHarness:
    display_name = "Fake"
    capabilities = frozenset(
        {"durable_threads", "message_turns", "interrupt", "event_streaming"}
    )
    _conversation_id = "telegram-backend-thread-1"
    _turn_id = "telegram-backend-turn-1"
    _assistant_text = "telegram managed final reply"
    _runtime_id = "runtime-test-1"

    async def ensure_ready(self, workspace_root: Path) -> None:
        _ = workspace_root

    async def backend_runtime_instance_id(self, workspace_root: Path) -> Optional[str]:
        _ = workspace_root
        return self._runtime_id

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> SimpleNamespace:
        _ = workspace_root, title
        return SimpleNamespace(id=self._conversation_id)

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> SimpleNamespace:
        _ = workspace_root
        return SimpleNamespace(id=conversation_id)

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> SimpleNamespace:
        _ = (
            workspace_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode,
            sandbox_policy,
            input_items,
        )
        return SimpleNamespace(
            conversation_id=conversation_id,
            turn_id=self._turn_id,
        )

    async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        raise AssertionError("review mode should not be used in this test")

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> SimpleNamespace:
        _ = workspace_root, conversation_id, turn_id, timeout
        return SimpleNamespace(
            status="ok",
            assistant_text=self._assistant_text,
            errors=[],
        )

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        _ = workspace_root, conversation_id, turn_id

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        _ = workspace_root, conversation_id, turn_id
        if False:
            yield ""


class _StreamingTelegramFakeHarness(_BaseTelegramFakeHarness):
    def __init__(
        self,
        events: list[Any],
        *,
        stream_finished: asyncio.Event,
        assistant_text: Optional[str] = None,
        wait_exception: Optional[BaseException] = None,
    ) -> None:
        self._events = events
        self._stream_finished = stream_finished
        if assistant_text is not None:
            self._assistant_text = assistant_text
        self._wait_exception = wait_exception

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> SimpleNamespace:
        _ = workspace_root, conversation_id, turn_id, timeout
        await self._stream_finished.wait()
        if self._wait_exception is not None:
            raise self._wait_exception
        return SimpleNamespace(
            status="ok",
            assistant_text=self._assistant_text,
            errors=[],
        )

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        _ = workspace_root, conversation_id, turn_id
        for event in self._events:
            yield event
        self._stream_finished.set()


def _patch_telegram_harness(
    monkeypatch: pytest.MonkeyPatch,
    harness: Any,
    target_module: Any = agent_registry_module,
    *,
    agent_id: str = "codex",
    agent_name: str = "Codex",
) -> None:
    monkeypatch.setattr(
        target_module,
        "get_registered_agents",
        lambda context=None: {
            agent_id: AgentDescriptor(
                id=agent_id,
                name=agent_name,
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )
