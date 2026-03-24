import json
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.cli import app

runner = CliRunner()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_telegram_trace_parses_failure_text_and_surfaces_errors(repo: Path) -> None:
    conversation_id = "-1003679298862:7073"
    log_path = repo / ".codex-autorunner" / "codex-server.log"
    _write(
        log_path,
        "\n".join(
            [
                '2026-03-24 10:00:00,000 [INFO] {"event":"telegram.turn.starting","chat_id":-1003679298862,"thread_id":7073,"conversation_id":"-1003679298862:7073"}',
                '2026-03-24 10:00:02,000 [WARNING] {"event":"app_server.turn_error","conversation_id":"-1003679298862:7073","error":"Reconnecting... 2/5","error_type":"CodexAppServerDisconnected"}',
                '2026-03-24 10:00:03,000 [WARNING] {"event":"telegram.turn.failed","chat_id":-1003679298862,"thread_id":7073,"reason":"app_server_disconnected","error":"Reconnecting... 2/5","error_type":"CodexAppServerDisconnected"}',
                '2026-03-24 10:01:00,000 [INFO] {"event":"telegram.turn.completed","chat_id":-1003679298862,"thread_id":9999,"conversation_id":"-1003679298862:9999"}',
            ]
        )
        + "\n",
    )

    result = runner.invoke(
        app,
        [
            "telegram",
            "trace",
            f"Telegram turn failed (conversation {conversation_id})",
            "--path",
            str(repo),
            "--limit",
            "20",
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"Conversation: {conversation_id}" in result.output
    assert "Matched lines: 3 | Error candidates: 2" in result.output
    assert "event=app_server.turn_error" in result.output
    assert "event=telegram.turn.failed" in result.output
    assert "Reconnecting... 2/5" in result.output


def test_telegram_trace_json_output_contains_matches(repo: Path) -> None:
    conversation_id = "-1003679298862:7073"
    token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    token_url = f"https://api.telegram.org/bot{token}/editMessageText"
    log_path = repo / ".codex-autorunner" / "codex-server.log"
    _write(
        log_path,
        "\n".join(
            [
                '2026-03-24 11:00:00,000 [INFO] {"event":"telegram.turn.starting","chat_id":-1003679298862,"thread_id":7073,"conversation_id":"-1003679298862:7073"}',
                (
                    "2026-03-24 11:00:01,000 [WARNING] "
                    '{"event":"telegram.turn.failed","chat_id":-1003679298862,'
                    '"thread_id":7073,"reason":"codex_turn_failed",'
                    f'"error":"Client error for url {token_url}",'
                    '"error_type":"RuntimeError"}'
                ),
            ]
        )
        + "\n",
    )

    result = runner.invoke(
        app,
        [
            "telegram",
            "trace",
            "--path",
            str(repo),
            "--json",
            f"--conversation={conversation_id}",
            "--limit",
            "10",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["conversation_id"] == conversation_id
    assert payload["chat_id"] == -1003679298862
    assert payload["thread_id"] == 7073
    assert len(payload["matches"]) == 2
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["event"] == "telegram.turn.failed"
    assert token not in result.output
    error_text = payload["errors"][0]["payload"]["error"]
    assert isinstance(error_text, str)
    assert "bot<redacted>" in error_text


def test_telegram_trace_default_scans_whole_file(repo: Path) -> None:
    conversation_id = "-1003679298862:7073"
    log_path = repo / ".codex-autorunner" / "codex-server.log"
    filler = [
        (
            f"2026-03-24 11:02:{idx % 60:02d},000 [INFO] "
            '{"event":"housekeeping.rule","chat_id":1}'
        )
        for idx in range(21050)
    ]
    lines = [
        (
            "2026-03-24 11:01:00,000 [WARNING] "
            '{"event":"telegram.turn.failed","chat_id":-1003679298862,'
            '"thread_id":7073,"reason":"timeout"}'
        ),
        *filler,
    ]
    _write(log_path, "\n".join(lines) + "\n")

    result = runner.invoke(
        app,
        [
            "telegram",
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


def test_telegram_trace_requires_parseable_conversation_id(repo: Path) -> None:
    result = runner.invoke(
        app,
        [
            "telegram",
            "trace",
            "Telegram turn failed",
            "--path",
            str(repo),
        ],
    )

    assert result.exit_code == 1
    assert "Could not parse conversation id" in result.output
