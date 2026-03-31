from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.handlers.commands import (
    execution as execution_commands_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.agent_model_utils import (
    _build_agent_options,
)
from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
    _RuntimeStub,
)
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord


class _RouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self.record = record

    async def ensure_topic(
        self, _chat_id: int, _thread_id: Optional[int]
    ) -> TelegramTopicRecord:
        return self.record

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self.record

    async def update_topic(
        self, _chat_id: int, _thread_id: Optional[int], apply
    ) -> TelegramTopicRecord:
        apply(self.record)
        return self.record

    async def set_active_thread(
        self, _chat_id: int, _thread_id: Optional[int], active_thread_id: Optional[str]
    ) -> TelegramTopicRecord:
        self.record.active_thread_id = active_thread_id
        return self.record


class _StoreStub:
    async def update_topic(self, _key: str, apply) -> None:
        _ = apply


class _ClientStub:
    def __init__(self, workspace_path: str) -> None:
        self.workspace_path = workspace_path
        self.thread_start_calls: list[tuple[str, str]] = []
        self.thread_resume_calls: list[str] = []

    async def thread_start(self, cwd: str, *, agent: str, **_kwargs: object) -> dict:
        self.thread_start_calls.append((cwd, agent))
        return {
            "thread_id": "hermes-thread-1",
            "agent": agent,
            "cwd": cwd,
            "path": cwd,
            "thread": {"id": "hermes-thread-1", "path": cwd, "agent": agent},
        }

    async def thread_resume(self, thread_id: str) -> dict[str, Any]:
        self.thread_resume_calls.append(thread_id)
        return {
            "thread_id": thread_id,
            "agent": "hermes",
            "cwd": self.workspace_path,
            "path": self.workspace_path,
            "thread": {
                "id": thread_id,
                "path": self.workspace_path,
                "agent": "hermes",
            },
        }


class _HermesHandler(TelegramCommandHandlers):
    def __init__(self, record: TelegramTopicRecord, client: _ClientStub) -> None:
        self._logger = logging.getLogger("test.telegram.hermes")
        self._router = _RouterStub(record)
        self._store = _StoreStub()
        self._client = client
        self.sent_messages: list[str] = []
        self._resume_options: dict[str, object] = {}
        self._bind_options: dict[str, object] = {}
        self._agent_options: dict[str, object] = {}
        self._model_options: dict[str, object] = {}
        self._model_pending: dict[str, object] = {}
        self._review_commit_options: dict[str, object] = {}
        self._review_commit_subjects: dict[str, object] = {}
        self._pending_review_custom: dict[str, object] = {}
        self._config = SimpleNamespace(
            root=None,
            defaults=SimpleNamespace(
                policies_for_mode=lambda _mode: (None, None),
            ),
        )

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        suffix = "root" if thread_id is None else str(thread_id)
        return f"{chat_id}:{suffix}"

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        reply_markup: Optional[dict[str, object]] = None,
    ) -> None:
        _ = thread_id, reply_to, reply_markup
        self.sent_messages.append(text)

    async def _client_for_workspace(self, _workspace_path: str) -> _ClientStub:
        return self._client

    async def _refresh_workspace_id(
        self, _key: str, _record: TelegramTopicRecord
    ) -> Optional[str]:
        return None

    async def _require_thread_workspace(
        self,
        _message: TelegramMessage,
        _workspace_path: str,
        _thread: object,
        *,
        action: str,
    ) -> bool:
        _ = action
        return True

    def _touch_cache_timestamp(self, _cache_name: str, _key: object) -> None:
        return None

    def _build_resume_keyboard(self, _state: object) -> dict[str, object]:
        return {}

    def _selection_prompt(self, prompt: str, _state: object) -> str:
        return prompt

    async def _finalize_selection(
        self,
        _key: str,
        _callback: object,
        text: str,
    ) -> None:
        self.sent_messages.append(text)

    async def _find_thread_conflict(
        self, _thread_id: str, *, key: str
    ) -> Optional[str]:
        _ = key
        return None

    async def _apply_thread_result(
        self,
        chat_id: int,
        thread_id: Optional[int],
        result: Any,
        *,
        active_thread_id: Optional[str] = None,
        overwrite_defaults: bool = False,
    ) -> TelegramTopicRecord:
        _ = overwrite_defaults
        resolved_thread_id = active_thread_id or result.get("thread_id")

        def apply(record: TelegramTopicRecord) -> None:
            if not resolved_thread_id:
                return
            record.active_thread_id = resolved_thread_id
            if resolved_thread_id in record.thread_ids:
                record.thread_ids.remove(resolved_thread_id)
            record.thread_ids.insert(0, resolved_thread_id)

        return await self._router.update_topic(chat_id, thread_id, apply)


def _message(text: str) -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=123,
        thread_id=None,
        from_user_id=456,
        text=text,
        date=None,
        is_topic_message=False,
    )


def test_agent_picker_includes_hermes() -> None:
    options = _build_agent_options(current="codex", availability="available")

    assert ("hermes", "hermes") in options


def test_agent_picker_includes_registered_hermes_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_registered_agents",
        lambda: {
            "hermes-m4-pma": SimpleNamespace(
                name="Hermes (hermes-m4-pma)",
                capabilities=frozenset(
                    {
                        "durable_threads",
                        "message_turns",
                        "interrupt",
                        "event_streaming",
                        "approvals",
                    }
                ),
            ),
        },
    )

    options = _build_agent_options(current="codex", availability="available")

    assert ("hermes-m4-pma", "hermes-m4-pma") in options


@pytest.mark.anyio
async def test_hermes_reports_resume_support() -> None:
    workspace = "/tmp/workspace"
    handler = _HermesHandler(
        TelegramTopicRecord(agent="hermes", workspace_path=workspace),
        _ClientStub(workspace),
    )

    assert handler._agent_supports_resume("hermes") is True


@pytest.mark.anyio
async def test_agent_command_accepts_registered_hermes_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_registered_agents",
        lambda: {
            "hermes-m4-pma": SimpleNamespace(
                name="Hermes (hermes-m4-pma)",
                capabilities=frozenset(
                    {
                        "durable_threads",
                        "message_turns",
                        "interrupt",
                        "event_streaming",
                        "approvals",
                    }
                ),
            ),
        },
    )
    workspace = str(tmp_path)
    record = TelegramTopicRecord(agent="codex", workspace_path=workspace)
    handler = _HermesHandler(record, _ClientStub(workspace))

    await handler._handle_agent(
        _message("/agent hermes-m4-pma"),
        "hermes-m4-pma",
        _RuntimeStub(),
    )

    assert record.agent == "hermes-m4-pma"
    assert handler.sent_messages
    assert handler.sent_messages[0].startswith("Agent set to hermes-m4-pma")


@pytest.mark.anyio
async def test_model_command_reports_catalog_unavailable_for_hermes(
    tmp_path: Path,
) -> None:
    workspace = str(tmp_path)
    handler = _HermesHandler(
        TelegramTopicRecord(agent="hermes", workspace_path=workspace),
        _ClientStub(workspace),
    )

    await handler._handle_model(_message("/model"), "", _RuntimeStub())

    assert handler.sent_messages == [
        "Hermes does not expose a model catalog. Use /model set <model> to override manually."
    ]


@pytest.mark.anyio
async def test_model_command_allows_manual_override_for_hermes(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    record = TelegramTopicRecord(agent="hermes", workspace_path=workspace)
    handler = _HermesHandler(record, _ClientStub(workspace))

    await handler._handle_model(
        _message("/model set haiku-4"), "set haiku-4", _RuntimeStub()
    )

    assert record.model == "haiku-4"
    assert handler.sent_messages == [
        "Model set to haiku-4. Will apply on the next turn."
    ]


@pytest.mark.anyio
async def test_review_command_reports_unsupported_capability_for_hermes(
    tmp_path: Path,
) -> None:
    workspace = str(tmp_path)
    handler = _HermesHandler(
        TelegramTopicRecord(agent="hermes", workspace_path=workspace),
        _ClientStub(workspace),
    )

    await handler._handle_review(_message("/review"), "", _RuntimeStub())

    assert handler.sent_messages == [
        "Hermes does not support /review in Telegram. Switch to an agent with review support: codex, opencode."
    ]


@pytest.mark.anyio
async def test_new_command_starts_hermes_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = str(tmp_path)
    record = TelegramTopicRecord(agent="hermes", workspace_path=workspace)
    client = _ClientStub(workspace)
    handler = _HermesHandler(record, client)

    async def _noop_sync(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        execution_commands_module,
        "_sync_telegram_thread_binding",
        _noop_sync,
    )

    await handler._handle_new(_message("/new"))

    assert client.thread_start_calls == [(workspace, "hermes")]
    assert record.active_thread_id == "hermes-thread-1"
    assert record.thread_ids == ["hermes-thread-1"]
    assert "Started new thread hermes-thread-1." in handler.sent_messages[0]
    assert "Agent: hermes" in handler.sent_messages[0]


@pytest.mark.anyio
async def test_resume_thread_by_id_works_for_hermes(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    record = TelegramTopicRecord(
        agent="hermes",
        workspace_path=workspace,
        thread_ids=["hermes-thread-2"],
    )
    client = _ClientStub(workspace)
    handler = _HermesHandler(record, client)

    await handler._resume_thread_by_id("123:root", "hermes-thread-2")

    assert client.thread_resume_calls == ["hermes-thread-2"]
    assert record.active_thread_id == "hermes-thread-2"
    assert record.thread_ids[0] == "hermes-thread-2"
    assert any(
        "Resumed thread `hermes-thread-2`" in text for text in handler.sent_messages
    )
