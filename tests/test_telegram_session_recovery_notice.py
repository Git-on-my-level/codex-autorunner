from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from codex_autorunner.core.orchestration.runtime_bindings import (
    clear_runtime_thread_binding,
)
from tests import telegram_pma_routing_support as support

_SESSION_NOTICE = (
    "Notice: the previous live session was unavailable, so I started a new " "session."
)


@pytest.mark.anyio
async def test_repo_turn_notifies_user_when_runtime_binding_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = support.TelegramTopicRecord(
        pma_enabled=False,
        workspace_path=str(tmp_path),
        repo_id="repo-1",
        agent="codex",
    )
    handler = support._ManagedThreadPMAHandler(record, tmp_path)

    class _FakeHarness:
        display_name = "Fake"
        capabilities = frozenset(
            {
                "durable_threads",
                "message_turns",
                "interrupt",
                "event_streaming",
            }
        )

        def __init__(self) -> None:
            self.start_calls: list[tuple[str, str]] = []
            self._conversation_count = 0

        async def ensure_ready(self, workspace_root: Path) -> None:
            assert workspace_root == tmp_path

        async def backend_runtime_instance_id(
            self, workspace_root: Path
        ) -> Optional[str]:
            assert workspace_root == tmp_path
            return "runtime-test-1"

        def supports(self, capability: str) -> bool:
            return capability in self.capabilities

        async def new_conversation(
            self, workspace_root: Path, title: Optional[str] = None
        ) -> SimpleNamespace:
            _ = workspace_root, title
            self._conversation_count += 1
            return SimpleNamespace(id=f"repo-backend-thread-{self._conversation_count}")

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
            _ = model, reasoning, approval_mode, sandbox_policy, input_items
            assert workspace_root == tmp_path
            self.start_calls.append((conversation_id, prompt))
            turn_id = f"{conversation_id}:turn-{len(self.start_calls)}"
            return SimpleNamespace(conversation_id=conversation_id, turn_id=turn_id)

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
            _ = workspace_root, timeout
            assert isinstance(turn_id, str)
            return SimpleNamespace(
                status="ok",
                assistant_text=f"reply for {conversation_id}/{turn_id}",
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

    harness = _FakeHarness()
    monkeypatch.setattr(
        support.execution_commands_module,
        "get_registered_agents",
        lambda context=None: {
            "codex": support.AgentDescriptor(
                id="codex",
                name="Codex",
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )

    first_message = support.TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="first repo orchestration prompt",
        date=None,
        is_topic_message=True,
    )
    second_message = support.TelegramMessage(
        update_id=2,
        message_id=11,
        chat_id=-1001,
        thread_id=101,
        from_user_id=42,
        text="second repo orchestration prompt",
        date=None,
        is_topic_message=True,
    )

    await handler._handle_normal_message(first_message, runtime=support._RuntimeStub())

    orchestration_service = (
        support.execution_commands_module._build_telegram_thread_orchestration_service(
            handler
        )
    )
    binding = orchestration_service.get_binding(
        surface_kind="telegram",
        surface_key="-1001:101",
    )
    assert binding is not None
    clear_runtime_thread_binding(tmp_path, binding.thread_target_id)

    await handler._handle_normal_message(second_message, runtime=support._RuntimeStub())

    assert harness.start_calls[0] == (
        "repo-backend-thread-1",
        "first repo orchestration prompt",
    )
    assert harness.start_calls[1][0] == "repo-backend-thread-2"
    assert harness.start_calls[1][1] == "second repo orchestration prompt"
    assert handler._sent[-1].startswith(_SESSION_NOTICE)
    assert "reply for repo-backend-thread-2/" in handler._sent[-1]
    assert record.active_thread_id == "repo-backend-thread-2"
