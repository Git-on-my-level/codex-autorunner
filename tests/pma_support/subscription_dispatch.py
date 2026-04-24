from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration import OrchestrationBindingStore
from codex_autorunner.core.pma_automation_store import PmaAutomationStore
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.telegram.state import TelegramStateStore, topic_key
from codex_autorunner.surfaces.web.routes.pma_routes.publish import (
    publish_automation_result,
)
from tests.conftest import write_test_config


@dataclass(frozen=True)
class ChatBindingSpec:
    surface_kind: str
    surface_key: str
    pma_enabled: bool = False


@dataclass(frozen=True)
class SubscriptionDispatchSnapshot:
    subscription: dict[str, Any]
    transition: dict[str, Any]
    wake_up: dict[str, Any]
    publish_result: dict[str, Any]
    discord_targets: tuple[str, ...]
    telegram_targets: tuple[str, ...]


class SubscriptionDispatchHarness:
    def __init__(self, tmp_path: Path) -> None:
        self.hub_root = (tmp_path / "hub").resolve()
        seed_hub_files(self.hub_root, force=True)
        cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
        cfg["discord_bot"]["enabled"] = True
        cfg["telegram_bot"]["enabled"] = True
        write_test_config(self.hub_root / CONFIG_FILENAME, cfg)
        self._workspaces: dict[str, Path] = {}
        self.thread_store = PmaThreadStore(self.hub_root)
        self.binding_store = OrchestrationBindingStore(self.hub_root)
        self.automation_store = PmaAutomationStore(self.hub_root)

    def add_repo(self, repo_id: str) -> Path:
        workspace = (self.hub_root / "worktrees" / repo_id).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        self._workspaces[repo_id] = workspace
        self._write_manifest()
        return workspace

    def create_thread(
        self,
        *,
        repo_id: str,
        binding: Optional[ChatBindingSpec] = None,
        agent: str = "codex",
    ) -> str:
        workspace = self.workspace(repo_id)
        thread = self.thread_store.create_thread(agent, workspace, repo_id=repo_id)
        thread_id = str(thread["managed_thread_id"])
        if binding is not None:
            self.binding_store.upsert_binding(
                surface_kind=binding.surface_kind,
                surface_key=binding.surface_key,
                thread_target_id=thread_id,
            )
        return thread_id

    def workspace(self, repo_id: str) -> Path:
        return self._workspaces[repo_id]

    async def seed_binding(
        self,
        *,
        repo_id: str,
        binding: ChatBindingSpec,
    ) -> None:
        workspace = self.workspace(repo_id)
        if binding.surface_kind == "discord":
            store = DiscordStateStore(
                self.hub_root / ".codex-autorunner" / "discord_state.sqlite3"
            )
            try:
                await store.upsert_binding(
                    channel_id=binding.surface_key,
                    guild_id="guild-1",
                    workspace_path=str(workspace),
                    repo_id=repo_id,
                )
                if binding.pma_enabled:
                    await store.update_pma_state(
                        channel_id=binding.surface_key,
                        pma_enabled=True,
                        pma_prev_workspace_path=str(workspace),
                        pma_prev_repo_id=repo_id,
                    )
            finally:
                await store.close()
            return
        if binding.surface_kind == "telegram":
            store = TelegramStateStore(
                self.hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
            )
            try:
                await store.bind_topic(
                    binding.surface_key,
                    str(workspace),
                    repo_id=repo_id,
                )
                if binding.pma_enabled:
                    await store.update_topic(
                        binding.surface_key,
                        lambda topic: _enable_telegram_pma(
                            topic,
                            repo_id=repo_id,
                            workspace=workspace,
                        ),
                    )
            finally:
                await store.close()
            return
        raise ValueError(f"Unsupported surface kind: {binding.surface_kind}")

    async def run_managed_thread_terminal_case(
        self,
        *,
        watched_repo_id: str,
        watched_thread_id: str,
        origin_thread_id: Optional[str],
        result_message: str = "Terminal follow-up",
        rebind_origin_to: Optional[ChatBindingSpec] = None,
        rebind_origin_after_wakeup_to: Optional[ChatBindingSpec] = None,
    ) -> SubscriptionDispatchSnapshot:
        subscription = self.automation_store.create_subscription(
            {
                "event_type": "managed_thread_completed",
                "thread_id": watched_thread_id,
                "origin_thread_id": origin_thread_id,
            }
        )["subscription"]
        if rebind_origin_to is not None and origin_thread_id is not None:
            self.binding_store.upsert_binding(
                surface_kind=rebind_origin_to.surface_kind,
                surface_key=rebind_origin_to.surface_key,
                thread_target_id=origin_thread_id,
            )
        transition = self.automation_store.notify_transition(
            {
                "event_type": "managed_thread_completed",
                "repo_id": watched_repo_id,
                "thread_id": watched_thread_id,
                "from_state": "running",
                "to_state": "completed",
                "transition_id": f"{watched_thread_id}:completed",
            }
        )
        wake_up = self.automation_store.list_pending_wakeups(limit=1)[0]
        if rebind_origin_after_wakeup_to is not None and origin_thread_id is not None:
            self.binding_store.upsert_binding(
                surface_kind=rebind_origin_after_wakeup_to.surface_kind,
                surface_key=rebind_origin_after_wakeup_to.surface_key,
                thread_target_id=origin_thread_id,
            )
        publish_result = await publish_automation_result(
            request=self._request_for(self.hub_root),
            result={"status": "ok", "message": result_message},
            client_turn_id="client-turn-1",
            lifecycle_event=None,
            wake_up=wake_up,
        )
        return SubscriptionDispatchSnapshot(
            subscription=subscription,
            transition=transition,
            wake_up=wake_up,
            publish_result=publish_result,
            discord_targets=await self.discord_targets(),
            telegram_targets=await self.telegram_targets(),
        )

    async def discord_targets(self) -> tuple[str, ...]:
        store = DiscordStateStore(
            self.hub_root / ".codex-autorunner" / "discord_state.sqlite3"
        )
        try:
            outbox = await store.list_outbox()
        finally:
            await store.close()
        return tuple(record.channel_id for record in outbox)

    async def telegram_targets(self) -> tuple[str, ...]:
        store = TelegramStateStore(
            self.hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
        )
        try:
            outbox = await store.list_outbox()
        finally:
            await store.close()
        return tuple(topic_key(record.chat_id, record.thread_id) for record in outbox)

    @staticmethod
    def _request_for(hub_root: Path) -> Any:
        return SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(config=SimpleNamespace(root=hub_root, raw={}))
            )
        )

    def _write_manifest(self) -> None:
        manifest_path = self.hub_root / ".codex-autorunner" / "manifest.yml"
        lines = ["# GENERATED by CAR - DO NOT EDIT", "version: 3", "repos:"]
        for repo_id, workspace in sorted(self._workspaces.items()):
            rel_path = workspace.relative_to(self.hub_root).as_posix()
            lines.extend(
                [
                    f"  - id: {repo_id}",
                    f"    path: {rel_path}",
                    "    enabled: true",
                    "    auto_run: false",
                    "    kind: base",
                    "",
                ]
            )
        manifest_path.write_text("\n".join(lines), encoding="utf-8")


def _enable_telegram_pma(topic: Any, *, repo_id: str, workspace: Path) -> None:
    topic.pma_enabled = True
    topic.pma_prev_repo_id = repo_id
    topic.pma_prev_workspace_path = str(workspace)


def set_telegram_topic_updated_at(
    state_path: Path, topic: str, updated_at: str
) -> None:
    conn = sqlite3.connect(state_path)
    try:
        conn.execute(
            "UPDATE telegram_topics SET updated_at = ?, last_active_at = ? WHERE topic_key = ?",
            (updated_at, updated_at, topic),
        )
        conn.commit()
    finally:
        conn.close()


__all__ = [
    "ChatBindingSpec",
    "SubscriptionDispatchHarness",
    "SubscriptionDispatchSnapshot",
    "set_telegram_topic_updated_at",
    "topic_key",
]
