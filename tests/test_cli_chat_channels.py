import json
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app
from codex_autorunner.integrations.chat.channel_directory import ChannelDirectoryStore
from codex_autorunner.integrations.chat.queue_control import ChatQueueControlStore

runner = CliRunner()


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
