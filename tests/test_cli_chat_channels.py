import json
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app
from codex_autorunner.integrations.chat.channel_directory import ChannelDirectoryStore

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
    assert "Chat channel directory entries:" in output
    assert "telegram:-1001:77" in output
    assert "Team Room / Ops" in output


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
