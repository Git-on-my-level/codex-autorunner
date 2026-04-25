import json
import sqlite3
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app
from codex_autorunner.integrations.chat.channel_directory import ChannelDirectoryStore
from codex_autorunner.integrations.chat.queue_control import ChatQueueControlStore
from codex_autorunner.surfaces.cli.commands import chat as chat_module

runner = CliRunner()


class _FakeDiscordRestClient:
    def __init__(self, *, bot_token: str, **_kwargs: Any) -> None:
        self.bot_token = bot_token

    async def __aenter__(self) -> "_FakeDiscordRestClient":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        return None

    async def get_channel(self, *, channel_id: str) -> dict[str, Any]:
        return {
            "id": channel_id,
            "type": 0,
            "name": "hermes-fork",
            "guild_id": "708566559182028821",
        }

    async def get_guild(self, *, guild_id: str) -> dict[str, Any]:
        return {"id": guild_id, "name": "CAR HQ"}


class _FakeTelegramBotClient:
    def __init__(self, _bot_token: str, **_kwargs: Any) -> None:
        return None

    async def __aenter__(self) -> "_FakeTelegramBotClient":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        return None

    async def get_chat(self, *, chat_id: int) -> dict[str, Any]:
        return {"id": chat_id, "type": "supergroup", "title": "Team Room"}


def test_chat_channels_list_shows_entries(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    store = ChannelDirectoryStore(tmp_path)
    store.record_seen(
        "telegram",
        "-1001",
        "77",
        "Team Room / Ops",
        {"chat_type": "supergroup"},
    )

    result = runner.invoke(app, ["chat", "channels", "list", "--path", str(tmp_path)])

    assert result.exit_code == 0
    output = result.output
    assert "telegram:-1001:77" in output


def test_chat_channels_list_query_filters_with_json_output(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    store = ChannelDirectoryStore(tmp_path)
    store.record_seen(
        "discord",
        "channel-1",
        "guild-1",
        "CAR HQ / #general",
        {"guild_id": "guild-1"},
    )
    store.record_seen(
        "telegram",
        "-1002",
        "42",
        "Team Room / Build",
        {"chat_type": "supergroup"},
    )

    result = runner.invoke(
        app,
        [
            "chat",
            "channels",
            "list",
            "--query",
            "car hq",
            "--json",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    entries = payload["entries"]
    assert len(entries) == 1
    assert entries[0]["key"] == "discord:channel-1:guild-1"
    assert entries[0]["display"] == "CAR HQ / #general"


def test_chat_queue_status_and_reset_round_trip(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    store = ChatQueueControlStore(tmp_path)
    store.record_snapshot(
        {
            "conversation_id": "discord:123:-",
            "platform": "discord",
            "chat_id": "123",
            "thread_id": None,
            "pending_count": 2,
            "active": True,
            "active_update_id": "discord:message:m-1",
            "active_started_at": "2026-04-02T01:02:03Z",
            "updated_at": "2026-04-02T01:02:05Z",
        }
    )

    status_result = runner.invoke(
        app,
        [
            "chat",
            "queue",
            "status",
            "--channel",
            "123",
            "--json",
            "--path",
            str(tmp_path),
        ],
    )

    assert status_result.exit_code == 0
    status_payload = json.loads(status_result.output)
    assert status_payload["conversation_id"] == "discord:123:-"
    assert status_payload["status"]["pending_count"] == 2
    assert status_payload["status"]["active"] is True

    reset_result = runner.invoke(
        app,
        [
            "chat",
            "queue",
            "reset",
            "--channel",
            "123",
            "--reason",
            "stuck worker",
            "--json",
            "--path",
            str(tmp_path),
        ],
    )

    assert reset_result.exit_code == 0
    reset_payload = json.loads(reset_result.output)
    request = reset_payload["reset_request"]
    assert request["conversation_id"] == "discord:123:-"
    assert request["reason"] == "stuck worker"

    taken = store.take_reset_requests(platform="discord")
    assert len(taken) == 1
    assert taken[0]["conversation_id"] == "discord:123:-"


def test_chat_resolve_discord_returns_api_metadata(tmp_path: Path, monkeypatch) -> None:
    seed_hub_files(tmp_path, force=True)
    monkeypatch.setenv("CAR_DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setattr(chat_module, "DiscordRestClient", _FakeDiscordRestClient)

    result = runner.invoke(
        app,
        [
            "chat",
            "resolve",
            "discord:1497177978256232530",
            "--format",
            "json",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["results"]) == 1
    resolved = payload["results"][0]
    assert resolved["identifier"] == "discord:1497177978256232530"
    assert resolved["display"] == "CAR HQ / #hermes-fork"
    assert resolved["guild_id"] == "708566559182028821"
    assert resolved["guild_name"] == "CAR HQ"
    assert resolved["type"] == "text"
    assert resolved["source"] == "api"


def test_chat_resolve_telegram_bare_topic_uses_directory_for_topic_title(
    tmp_path: Path, monkeypatch
) -> None:
    seed_hub_files(tmp_path, force=True)
    ChannelDirectoryStore(tmp_path).record_seen(
        "telegram",
        "-1001",
        "77",
        "Team Room / Ops",
        {"chat_type": "supergroup", "topic_title": "Ops"},
    )
    monkeypatch.setenv("CAR_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(chat_module, "TelegramBotClient", _FakeTelegramBotClient)

    result = runner.invoke(
        app,
        [
            "chat",
            "resolve",
            "--format",
            "json",
            "--path",
            str(tmp_path),
            "--",
            "-1001:77",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["results"]) == 1
    resolved = payload["results"][0]
    assert resolved["identifier"] == "telegram:-1001:77"
    assert resolved["display"] == "Team Room / Ops"
    assert resolved["topic_title"] == "Ops"
    assert resolved["type"] == "supergroup"
    assert resolved["source"] == "api+directory"


def test_chat_resolve_reports_unavailable_without_tokens(
    tmp_path: Path, monkeypatch
) -> None:
    seed_hub_files(tmp_path, force=True)
    monkeypatch.delenv("CAR_DISCORD_BOT_TOKEN", raising=False)

    result = runner.invoke(
        app,
        [
            "chat",
            "resolve",
            "discord:1234567890",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "discord:1234567890  (unavailable:" in result.output
    assert "CAR_DISCORD_BOT_TOKEN" in result.output


def test_chat_resolve_from_notifications_uses_recent_targets(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    channel_store = ChannelDirectoryStore(tmp_path)
    channel_store.record_seen(
        "discord",
        "channel-9",
        None,
        "CAR HQ / #general",
        {"guild_id": "guild-9"},
    )
    channel_store.record_seen(
        "telegram",
        "-1002",
        "42",
        "Team Room / Build",
        {"chat_type": "supergroup", "topic_title": "Build"},
    )

    db_path = tmp_path / ".codex-autorunner" / "orchestration.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE orch_notification_conversations (
                notification_id TEXT PRIMARY KEY,
                surface_kind TEXT NOT NULL,
                surface_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO orch_notification_conversations (
                notification_id, surface_kind, surface_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    "n-1",
                    "discord",
                    "channel-9",
                    "2026-04-25T00:00:00Z",
                    "2026-04-25T00:00:10Z",
                ),
                (
                    "n-2",
                    "telegram",
                    "-1002:42",
                    "2026-04-25T00:00:01Z",
                    "2026-04-25T00:00:09Z",
                ),
                (
                    "n-3",
                    "discord",
                    "channel-9",
                    "2026-04-25T00:00:02Z",
                    "2026-04-25T00:00:08Z",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        app,
        [
            "chat",
            "resolve",
            "--from-notifications",
            "--format",
            "json",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    identifiers = [item["identifier"] for item in payload["results"]]
    assert identifiers == ["discord:channel-9", "telegram:-1002:42"]
    assert payload["results"][0]["display"] == "CAR HQ / #general"
    assert payload["results"][1]["display"] == "Team Room / Build"
