import json
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.cli import app

runner = CliRunner()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_discord_trace_parses_failure_text_and_surfaces_errors(repo: Path) -> None:
    conversation_id = "discord:123456789012345678:987654321098765432"
    log_path = repo / ".codex-autorunner" / "codex-server.log"
    _write(
        log_path,
        "\n".join(
            [
                '2026-03-24 10:00:00,000 [INFO] {"event":"chat.dispatch.received","chat_id":"123456789012345678","thread_id":"987654321098765432","conversation_id":"discord:123456789012345678:987654321098765432"}',
                '2026-03-24 10:00:02,000 [WARNING] {"event":"discord.turn.failed","channel_id":"123456789012345678","conversation_id":"discord:123456789012345678:987654321098765432","error":"Gateway reconnecting","error_type":"GatewayDisconnected"}',
                '2026-03-24 10:00:03,000 [WARNING] {"event":"discord.outbox.send_failed","channel_id":"123456789012345678","guild_id":"987654321098765432","error":"Missing Access"}',
                '2026-03-24 10:01:00,000 [INFO] {"event":"discord.turn.completed","channel_id":"111111111111111111","conversation_id":"discord:111111111111111111:222222222222222222"}',
            ]
        )
        + "\n",
    )

    result = runner.invoke(
        app,
        [
            "discord",
            "trace",
            f"Turn failed: Gateway reconnecting (conversation {conversation_id})",
            "--path",
            str(repo),
            "--limit",
            "20",
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"Conversation: {conversation_id}" in result.output
    assert "Matched lines: 3 | Error candidates: 2" in result.output
    assert "event=discord.turn.failed" in result.output
    assert "event=discord.outbox.send_failed" in result.output
    assert "Gateway reconnecting" in result.output


def test_discord_trace_json_output_contains_matches(repo: Path) -> None:
    conversation_id = "discord:123456789012345678:987654321098765432"
    webhook_url = (
        "https://discord.com/api/webhooks/123456789012345678/"
        "super-secret-webhook-token"
    )
    log_path = repo / ".codex-autorunner" / "codex-server.log"
    _write(
        log_path,
        "\n".join(
            [
                '2026-03-24 11:00:00,000 [INFO] {"event":"chat.dispatch.received","chat_id":"123456789012345678","thread_id":"987654321098765432","conversation_id":"discord:123456789012345678:987654321098765432"}',
                (
                    "2026-03-24 11:00:01,000 [WARNING] "
                    '{"event":"discord.turn.failed","channel_id":"123456789012345678",'
                    '"guild_id":"987654321098765432",'
                    f'"error":"Discord API call failed: {webhook_url}",'
                    '"error_type":"RuntimeError"}'
                ),
            ]
        )
        + "\n",
    )

    result = runner.invoke(
        app,
        [
            "discord",
            "trace",
            "--path",
            str(repo),
            "--json",
            "--conversation=123456789012345678:987654321098765432",
            "--limit",
            "10",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["conversation_id"] == conversation_id
    assert payload["channel_id"] == "123456789012345678"
    assert payload["thread_id"] == "987654321098765432"
    assert len(payload["matches"]) == 2
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["event"] == "discord.turn.failed"
    assert webhook_url not in result.output
    error_text = payload["errors"][0]["payload"]["error"]
    assert isinstance(error_text, str)
    assert "/webhooks/123456789012345678/<redacted>" in error_text


def test_discord_trace_default_scans_whole_file(repo: Path) -> None:
    conversation_id = "discord:123456789012345678:987654321098765432"
    log_path = repo / ".codex-autorunner" / "codex-server.log"
    filler = [
        (
            f"2026-03-24 11:02:{idx % 60:02d},000 [INFO] "
            '{"event":"housekeeping.rule","channel_id":"3"}'
        )
        for idx in range(21050)
    ]
    lines = [
        (
            "2026-03-24 11:01:00,000 [WARNING] "
            '{"event":"discord.turn.failed","channel_id":"123456789012345678",'
            '"guild_id":"987654321098765432","reason":"timeout"}'
        ),
        *filler,
    ]
    _write(log_path, "\n".join(lines) + "\n")

    result = runner.invoke(
        app,
        [
            "discord",
            "trace",
            "--path",
            str(repo),
            f"--conversation={conversation_id}",
            "--limit",
            "5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Matched lines: 1 | Error candidates: 1" in result.output


def test_discord_trace_thread_scoped_filter_skips_channel_only_payload(
    repo: Path,
) -> None:
    conversation_id = "discord:123456789012345678:987654321098765432"
    log_path = repo / ".codex-autorunner" / "codex-server.log"
    _write(
        log_path,
        "\n".join(
            [
                '2026-03-24 12:00:00,000 [INFO] {"event":"chat.dispatch.received","chat_id":"123456789012345678","thread_id":"987654321098765432","conversation_id":"discord:123456789012345678:987654321098765432"}',
                '2026-03-24 12:00:01,000 [WARNING] {"event":"discord.turn.failed","channel_id":"123456789012345678","error":"no guild discriminator"}',
                '2026-03-24 12:00:02,000 [WARNING] {"event":"discord.turn.failed","channel_id":"123456789012345678","guild_id":"987654321098765432","error":"matched"}',
            ]
        )
        + "\n",
    )

    result = runner.invoke(
        app,
        [
            "discord",
            "trace",
            f"--conversation={conversation_id}",
            "--path",
            str(repo),
            "--context-lines",
            "0",
            "--limit",
            "10",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Matched lines: 2 | Error candidates: 1" in result.output
    assert "no guild discriminator" not in result.output
    assert "matched" in result.output


def test_discord_trace_requires_parseable_conversation_id(repo: Path) -> None:
    result = runner.invoke(
        app,
        [
            "discord",
            "trace",
            "Turn failed",
            "--path",
            str(repo),
        ],
    )

    assert result.exit_code == 1
    assert "Could not parse conversation id" in result.output


def test_discord_trace_rejects_malformed_discord_conversation_id(repo: Path) -> None:
    result = runner.invoke(
        app,
        [
            "discord",
            "trace",
            "--conversation=discord:abc:def",
            "--path",
            str(repo),
        ],
    )

    assert result.exit_code == 1
    assert "Could not parse conversation id" in result.output
