from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.adapters.app_server.callback_registry import (
    RuntimeCallbackRegistry,
)


def _registry(**handlers: Any) -> RuntimeCallbackRegistry:
    return RuntimeCallbackRegistry(
        approval_handler=handlers.get("approval_handler"),
        question_handler=handlers.get("question_handler"),
        notification_handler=handlers.get("notification_handler"),
        default_approval_decision="cancel",
        default_user_input_result=lambda _envelope: {"answers": {}},
    )


def _envelope(thread_id: str, turn_id: str) -> SimpleNamespace:
    raw_message = {
        "method": "item/commandExecution/requestApproval",
        "params": {"threadId": thread_id, "turnId": turn_id},
    }
    return SimpleNamespace(
        params=raw_message["params"],
        request=SimpleNamespace(thread_id=thread_id, turn_id=turn_id),
        raw_message=raw_message,
    )


@pytest.mark.anyio
async def test_callback_registry_prefers_turn_then_thread_then_global_handler() -> None:
    calls: list[str] = []

    async def global_handler(_message: dict[str, Any]) -> str:
        calls.append("global")
        return "global"

    async def thread_handler(_message: dict[str, Any]) -> str:
        calls.append("thread")
        return "thread"

    async def turn_handler(_message: dict[str, Any]) -> str:
        calls.append("turn")
        return "turn"

    registry = _registry(approval_handler=global_handler)
    registry.register(thread_id="thread-1", approval_handler=thread_handler)
    registry.register(
        thread_id="thread-1",
        turn_id="turn-1",
        approval_handler=turn_handler,
    )

    assert (
        await registry.approval_adapter_for(_envelope("thread-1", "turn-1")).decide(
            _envelope("thread-1", "turn-1")
        )
        == "turn"
    )
    registry.unregister(thread_id="thread-1", turn_id="turn-1")
    assert (
        await registry.approval_adapter_for(_envelope("thread-1", "turn-1")).decide(
            _envelope("thread-1", "turn-1")
        )
        == "global"
    )

    registry.register(thread_id="thread-1", approval_handler=thread_handler)
    assert (
        await registry.approval_adapter_for(_envelope("thread-1", "turn-1")).decide(
            _envelope("thread-1", "turn-1")
        )
        == "thread"
    )

    assert calls == ["turn", "global", "thread"]


@pytest.mark.anyio
async def test_callback_registry_unregister_thread_cleans_scoped_turn_handlers() -> (
    None
):
    async def thread_handler(_message: dict[str, Any]) -> str:
        return "thread"

    async def turn_handler(_message: dict[str, Any]) -> str:
        return "turn"

    registry = _registry()
    registry.register(thread_id="thread-1", approval_handler=thread_handler)
    registry.register(
        thread_id="thread-1",
        turn_id="turn-1",
        approval_handler=turn_handler,
    )

    registry.unregister(thread_id="thread-1")

    assert (
        await registry.approval_adapter_for(_envelope("thread-1", "turn-1")).decide(
            _envelope("thread-1", "turn-1")
        )
        == "cancel"
    )
    assert not registry.has_thread_callbacks("thread-1")
