from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration.bindings import OrchestrationBindingStore
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.telegram.state import TelegramStateStore, topic_key
from tests.conftest import write_test_config

pytestmark = pytest.mark.slow


def _enable_pma(
    hub_root: Path,
    *,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    max_text_chars: Optional[int] = None,
    managed_thread_terminal_followup_default: Optional[bool] = None,
    reactive_enabled: Optional[bool] = None,
) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    if model is not None:
        cfg["pma"]["model"] = model
    if reasoning is not None:
        cfg["pma"]["reasoning"] = reasoning
    if max_text_chars is not None:
        cfg["pma"]["max_text_chars"] = max_text_chars
    if managed_thread_terminal_followup_default is not None:
        cfg["pma"][
            "managed_thread_terminal_followup_default"
        ] = managed_thread_terminal_followup_default
    if reactive_enabled is not None:
        cfg["pma"]["reactive_enabled"] = reactive_enabled
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def _disable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def _install_fake_successful_chat_supervisor(
    app,
    *,
    turn_id: str,
    message: str = "assistant text",
    raw_events: Optional[list[dict]] = None,
) -> None:
    class FakeTurnHandle:
        def __init__(self) -> None:
            self.turn_id = turn_id

        async def wait(self, timeout=None):
            _ = timeout
            return type(
                "Result",
                (),
                {
                    "agent_messages": [message],
                    "raw_events": list(raw_events or []),
                    "errors": [],
                },
            )()

    class FakeClient:
        async def thread_resume(self, thread_id: str) -> None:
            _ = thread_id
            return None

        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()


async def _seed_discord_pma_binding(hub_env, *, channel_id: str) -> None:
    store = DiscordStateStore(
        hub_env.hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    try:
        await store.upsert_binding(
            channel_id=channel_id,
            guild_id="guild-1",
            workspace_path=str(hub_env.repo_root.resolve()),
            repo_id=hub_env.repo_id,
        )
        await store.update_pma_state(
            channel_id=channel_id,
            pma_enabled=True,
            pma_prev_workspace_path=str(hub_env.repo_root.resolve()),
            pma_prev_repo_id=hub_env.repo_id,
        )
    finally:
        await store.close()


async def _seed_telegram_pma_binding(
    hub_env, *, chat_id: int, thread_id: Optional[int]
) -> None:
    store = TelegramStateStore(
        hub_env.hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        key = topic_key(chat_id, thread_id)
        await store.bind_topic(
            key,
            str(hub_env.repo_root.resolve()),
            repo_id=hub_env.repo_id,
        )

        def _enable(record: Any) -> None:
            record.pma_enabled = True
            record.pma_prev_repo_id = hub_env.repo_id
            record.pma_prev_workspace_path = str(hub_env.repo_root.resolve())

        await store.update_topic(key, _enable)
    finally:
        await store.close()


async def _bind_thread_to_discord(
    hub_env,
    *,
    managed_thread_id: str,
    channel_id: str,
) -> None:
    OrchestrationBindingStore(hub_env.hub_root).upsert_binding(
        surface_kind="discord",
        surface_key=channel_id,
        thread_target_id=managed_thread_id,
        agent_id="codex",
        repo_id=hub_env.repo_id,
        mode="repo",
    )
    store = DiscordStateStore(
        hub_env.hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    try:
        await store.upsert_binding(
            channel_id=channel_id,
            guild_id="guild-1",
            workspace_path=str(hub_env.repo_root.resolve()),
            repo_id=hub_env.repo_id,
        )
    finally:
        await store.close()


async def _bind_thread_to_telegram(
    hub_env,
    *,
    managed_thread_id: str,
    chat_id: int,
    thread_id: int | None,
) -> None:
    surface_key = topic_key(chat_id, thread_id)
    OrchestrationBindingStore(hub_env.hub_root).upsert_binding(
        surface_kind="telegram",
        surface_key=surface_key,
        thread_target_id=managed_thread_id,
        agent_id="codex",
        repo_id=hub_env.repo_id,
        mode="repo",
    )
    store = TelegramStateStore(
        hub_env.hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        await store.bind_topic(
            surface_key,
            str(hub_env.repo_root.resolve()),
            repo_id=hub_env.repo_id,
        )
    finally:
        await store.close()


def _repo_owner(hub_env) -> dict[str, str]:
    return {"resource_kind": "repo", "resource_id": hub_env.repo_id}
