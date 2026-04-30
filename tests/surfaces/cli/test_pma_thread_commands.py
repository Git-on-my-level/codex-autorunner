import inspect
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from typer.testing import CliRunner

from codex_autorunner.core.pma_audit import PmaActionType, PmaAuditLog
from codex_autorunner.surfaces.cli import pma_control_plane, pma_thread_commands
from codex_autorunner.surfaces.cli.pma_cli import pma_app

_SENTINEL = object()


def _always_timeout_with_status(
    method: str, url: str, payload=None, token_env=None, timeout=None
):
    _ = method, url, payload, token_env, timeout
    raise httpx.TimeoutException("timed out")


def _make_next_status_request_json(payloads, default=_SENTINEL):
    def _request_json(method, url, payload=None, token_env=None, params=None):
        _ = method, url, payload, token_env, params
        if default is _SENTINEL:
            return next(payloads)
        return next(payloads, default)

    return _request_json


def test_pma_cli_thread_group_has_required_commands():
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "spawn" in output, "PMA thread should have 'spawn' command"
    assert "create" in output, "PMA thread should have 'create' alias"
    assert "list" in output, "PMA thread should have 'list' command"
    assert "info" in output, "PMA thread should have 'info' command"
    assert "status" in output, "PMA thread should have 'status' command"
    assert "send" in output, "PMA thread should have 'send' command"
    assert "turns" in output, "PMA thread should have 'turns' command"
    assert "queue" in output, "PMA thread should have 'queue' command"
    assert "cancel-queued" in output, "PMA thread should have 'cancel-queued' command"
    assert "clear-queue" in output, "PMA thread should have 'clear-queue' command"
    assert "output" in output, "PMA thread should have 'output' command"
    assert "subscribe" in output, "PMA thread should have 'subscribe' command"
    assert "tail" in output, "PMA thread should have 'tail' command"
    assert "compact" in output, "PMA thread should have 'compact' command"
    assert "fork" in output, "PMA thread should have 'fork' command"
    assert "resume" in output, "PMA thread should have 'resume' command"
    assert "archive" in output, "PMA thread should have 'archive' command"
    assert "interrupt" in output, "PMA thread should have 'interrupt' command"


def test_pma_cli_thread_spawn_help_shows_json_option():
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "spawn", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA thread spawn should support --json"
    assert "--notify-on" in output, "PMA thread spawn should support --notify-on"


def test_pma_cli_thread_list_help_shows_json_option():
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "list", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA thread list should support --json"
    assert "--ndjson" in output, "PMA thread list should support --ndjson"


def test_pma_cli_thread_send_help_shows_json_option():
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "send", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA thread send should support --json"
    assert "--watch" in output, "PMA thread send should support --watch"
    assert "--no-wait" in output, "PMA thread send should support --no-wait"
    assert "--if-busy" in output, "PMA thread send should support busy-thread policy"
    assert "--notify-on" in output, "PMA thread send should support --notify-on"
    assert "--message-file" in output, "PMA thread send should support file input"
    assert "--message-stdin" in output, "PMA thread send should support stdin input"


def test_pma_cli_thread_status_help_shows_json_option():
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "status", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA thread status should support --json"
    assert "--output" in output
    assert "--lines" in output
    assert "assistant_text-only" in output
    assert "--continue" in output
    assert "--output-file" in output


def test_pma_cli_thread_output_help_shows_pagination_options() -> None:
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "output", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--turn" in output
    assert "--lines" in output
    assert "--continue" in output
    assert "--output-file" in output


def test_pma_cli_thread_queue_help_shows_json_option() -> None:
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "queue", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output
    assert "--limit" in output


def test_pma_cli_thread_subscribe_help_mentions_thread_scope() -> None:
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "subscribe", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--event" in output
    assert "--lane" in output
    assert "thread-scoped" in output.lower()


def test_pma_cli_thread_fork_help_shows_json_option():
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "fork", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output
    assert "--name" in output


def test_pma_cli_thread_archive_help_shows_bulk_options() -> None:
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "archive", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--id" in output
    assert "--ids" in output
    assert "--ids-stdin" in output


def test_pma_tail_snapshot_render_lines_use_typed_serializer() -> None:
    snapshot = pma_thread_commands._PmaTailSnapshot.from_dict(
        {
            "managed_turn_id": "turn-9",
            "turn_status": "running",
            "activity": "waiting",
            "phase": "tool",
            "elapsed_seconds": 125,
            "idle_seconds": 35,
            "guidance": "waiting on tool",
            "active_turn_diagnostics": {
                "request_kind": "message",
                "model": "gpt-5",
                "reasoning": "low",
                "stream_available": True,
                "stalled": True,
                "stall_reason": "tool timeout",
            },
            "last_tool": {
                "name": "status-check",
                "status": "running",
                "in_flight": True,
            },
            "lifecycle_events": ["turn_started", "tool_invoked"],
            "events": [],
        }
    )

    assert snapshot.render_lines() == [
        "managed_turn_id=turn-9 turn_status=running activity=waiting phase=tool elapsed=2m05s idle=35s",
        "guidance: waiting on tool",
        "active_turn: kind=message model=gpt-5 reasoning=low stream=yes stalled=yes",
        "stall_reason: tool timeout",
        "last_tool=status-check status=running in_flight=yes",
        "lifecycle: turn_started, tool_invoked",
        "No tail events.",
        "No events for 35s (possibly stalled).",
    ]


def test_pma_thread_status_snapshot_render_lines_include_queue_and_excerpt() -> None:
    snapshot = pma_thread_commands._PmaThreadStatusSnapshot.from_dict(
        {
            "managed_thread_id": "thread-1",
            "thread": {
                "agent": "codex",
                "repo_id": "repo-1",
                "status": "completed",
                "lifecycle_status": "active",
                "status_reason": "managed_turn_completed",
            },
            "status": "completed",
            "status_reason": "managed_turn_completed",
            "is_alive": True,
            "turn": {
                "managed_turn_id": "turn-1",
                "status": "ok",
                "activity": "completed",
                "phase": "finalize",
                "elapsed_seconds": 61,
                "idle_seconds": 0,
                "guidance": "wrap up",
                "last_tool": {
                    "name": "status-check",
                    "status": "ok",
                    "in_flight": False,
                },
            },
            "active_turn_diagnostics": {
                "request_kind": "message",
                "model": "gpt-5",
                "reasoning": "high",
                "stream_available": True,
                "stalled": False,
                "last_event_at": "2026-03-17T10:11:12Z",
                "last_event_type": "tool_completed",
                "last_event_summary": "tool: status-check",
            },
            "recent_progress": [
                {
                    "event_type": "assistant_output",
                    "summary": "Drafted a reply",
                    "received_at": "2026-03-17T10:11:13Z",
                    "event_id": 3,
                }
            ],
            "queue_depth": 2,
            "queued_turns": [
                {
                    "managed_turn_id": "turn-2",
                    "request_kind": "message",
                    "state": "queued",
                    "position": 1,
                    "enqueued_at": "2026-03-17T10:12:00Z",
                    "prompt_preview": "follow up on the same thread",
                }
            ],
            "latest_output_excerpt": "assistant conclusion",
        }
    )

    lines = snapshot.render_lines()

    assert (
        "id=thread-1 agent=codex repo=repo-1 operator_status=reusable "
        "runtime_status=completed lifecycle_status=active alive=yes" in lines
    )
    assert "status_reason=managed_turn_completed" in lines
    assert (
        "managed_turn_id=turn-1 turn_status=ok activity=completed "
        "phase=finalize elapsed=1m01s idle=0s" in lines
    )
    assert "guidance: wrap up" in lines
    assert (
        "active_turn: kind=message model=gpt-5 reasoning=high stream=yes stalled=no"
        in lines
    )
    assert "last_event: tool_completed @10:11:12 tool: status-check" in lines
    assert "last_tool=status-check status=ok in_flight=no" in lines
    assert "recent progress:" in lines
    assert "[10:11:13] #3 assistant_output: Drafted a reply" in lines
    assert "queued=2" in lines
    assert (
        "queued_turn_id=turn-2 position=1 state=queued kind=message "
        "enqueued=2026-03-17T10:12:00Z prompt=follow up on the same thread" in lines
    )
    assert lines[-2:] == ["assistant_text_excerpt:", "assistant conclusion"]


def test_pma_thread_status_snapshot_info_level_filters_progress_noise() -> None:
    snapshot = pma_thread_commands._PmaThreadStatusSnapshot.from_dict(
        {
            "managed_thread_id": "thread-1",
            "thread": {
                "agent": "codex",
                "repo_id": "repo-1",
                "status": "running",
                "lifecycle_status": "active",
            },
            "status": "running",
            "turn": {
                "managed_turn_id": "turn-1",
                "status": "running",
                "activity": "working",
                "phase": "streaming",
            },
            "recent_progress": [
                {
                    "event_type": "progress",
                    "summary": "No decoder for method: server.heartbeat",
                    "received_at": "2026-03-17T10:11:10Z",
                    "event_id": 19,
                },
                {
                    "event_type": "assistant_update",
                    "summary": "The",
                    "received_at": "2026-03-17T10:11:11Z",
                    "event_id": 20,
                },
                {
                    "event_type": "assistant_update",
                    "summary": "The user wants me to sleep 15 seconds",
                    "received_at": "2026-03-17T10:11:11Z",
                    "event_id": 26,
                },
                {
                    "event_type": "tool_started",
                    "summary": "tool: bash",
                    "received_at": "2026-03-17T10:11:12Z",
                    "event_id": 27,
                },
                {
                    "event_type": "tool_started",
                    "summary": "tool: bash",
                    "received_at": "2026-03-17T10:11:12Z",
                    "event_id": 28,
                },
            ],
        }
    )

    lines = snapshot.render_lines(level=pma_thread_commands._PmaVerbosityLevel.INFO)

    assert "recent progress:" in lines
    assert (
        "[10:11:11] #20-#26 assistant_update: The user wants me to sleep 15 seconds"
        in lines
    )
    assert "[10:11:12] #27-#28 tool_started: tool: bash (x2)" in lines
    assert "No decoder for method: server.heartbeat" not in "\n".join(lines)


def test_pma_tail_snapshot_debug_level_keeps_raw_progress_noise() -> None:
    snapshot = pma_thread_commands._PmaTailSnapshot.from_dict(
        {
            "managed_turn_id": "turn-1",
            "turn_status": "running",
            "activity": "working",
            "events": [
                {
                    "event_type": "progress",
                    "summary": "No decoder for method: server.connected",
                    "received_at": "2026-03-17T10:11:10Z",
                    "event_id": 2,
                },
                {
                    "event_type": "assistant_update",
                    "summary": "The",
                    "received_at": "2026-03-17T10:11:11Z",
                    "event_id": 20,
                },
                {
                    "event_type": "assistant_update",
                    "summary": "The user",
                    "received_at": "2026-03-17T10:11:11Z",
                    "event_id": 21,
                },
            ],
        }
    )

    lines = snapshot.render_lines(level=pma_thread_commands._PmaVerbosityLevel.DEBUG)

    assert "[10:11:10] #2 progress: No decoder for method: server.connected" in lines
    assert "[10:11:11] #20 assistant_update: The" in lines
    assert "[10:11:11] #21 assistant_update: The user" in lines


def test_pma_thread_send_request_and_response_helpers() -> None:
    request = pma_thread_commands._ManagedThreadSendRequest(
        message="follow up",
        busy_policy="interrupt",
        defer_execution=True,
        wait_for_confirmation=True,
        model="gpt-5",
        reasoning="high",
        notify_on="terminal",
        notify_lane="lane-1",
        notify_once=False,
    )

    assert request.to_payload() == {
        "message": "follow up",
        "busy_policy": "interrupt",
        "defer_execution": True,
        "wait_for_confirmation": True,
        "model": "gpt-5",
        "reasoning": "high",
        "notify_on": "terminal",
        "notify_lane": "lane-1",
        "notify_once": False,
    }

    response = pma_thread_commands._ManagedThreadSendResponse.from_http(
        200,
        {
            "status": "ok",
            "send_state": "accepted",
            "execution_state": "running",
            "managed_turn_id": "turn-2",
            "active_managed_turn_id": "turn-1",
            "queue_depth": "1",
            "delivered_message": "follow up",
            "notification": {
                "mode": "terminal",
                "subscription": {"lane_id": "lane-1"},
            },
        },
        default_message="fallback",
    )
    error_response = pma_thread_commands._ManagedThreadSendResponse.from_http(
        409,
        {
            "status": "error",
            "send_state": "rejected",
            "detail": "Managed thread send failed",
            "next_step": "wait for the active turn",
        },
        default_message="fallback",
    )

    assert response.is_ok is True
    assert response.accepted_line() == (
        "send_state=accepted managed_turn_id=turn-2 "
        "active_managed_turn_id=turn-1 queue_depth=1 "
        "subscription=terminal lane=lane-1"
    )
    assert response.delivered_message == "follow up"
    assert error_response.is_ok is False
    assert error_response.error_detail() == "Managed thread send failed"
    assert error_response.next_step == "wait for the active turn"


def test_pma_cli_thread_query_commands_use_orchestration_routes(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    responses = {
        "/hub/pma/threads": {
            "threads": [
                {
                    "managed_thread_id": "thread-1",
                    "agent": "codex",
                    "status": "idle",
                    "status_reason": "thread_created",
                    "repo_id": "repo-1",
                }
            ]
        },
        "/hub/pma/threads/thread-1": {
            "thread": {
                "managed_thread_id": "thread-1",
                "agent": "codex",
                "status": "completed",
                "latest_turn_id": "turn-1",
                "latest_turn_status": "ok",
                "latest_assistant_text": "assistant conclusion",
            }
        },
        "/hub/pma/threads/thread-1/status": {
            "managed_thread_id": "thread-1",
            "thread": {
                "managed_thread_id": "thread-1",
                "agent": "codex",
                "repo_id": "repo-1",
                "status": "completed",
                "lifecycle_status": "active",
                "status_reason": "managed_turn_completed",
                "latest_turn_id": "turn-1",
                "latest_turn_status": "ok",
                "latest_assistant_text": "assistant conclusion",
            },
            "status": "completed",
            "status_reason": "managed_turn_completed",
            "turn": {
                "managed_turn_id": "turn-1",
                "status": "ok",
                "activity": "completed",
                "elapsed_seconds": 60,
                "idle_seconds": 0,
            },
            "active_turn_diagnostics": {
                "managed_turn_id": "turn-1",
                "request_kind": "message",
                "model": "gpt-5",
                "reasoning": "high",
                "prompt_preview": "Inspect the live thread state",
                "backend_thread_id": "backend-thread-1",
                "backend_turn_id": "backend-turn-1",
                "stream_available": True,
                "last_event_at": "2026-03-17T10:11:12Z",
                "last_event_type": "tool_completed",
                "last_event_summary": "tool: status-check",
                "stalled": False,
                "stall_reason": None,
            },
            "recent_progress": [],
            "latest_output_excerpt": "assistant conclusion",
            "latest_assistant_text": "assistant conclusion",
        },
        "/hub/pma/threads/thread-1/tail": {
            "managed_thread_id": "thread-1",
            "managed_turn_id": "turn-1",
            "turn_status": "ok",
            "activity": "completed",
            "elapsed_seconds": 60,
            "idle_seconds": 0,
            "lifecycle_events": ["turn_started", "turn_completed"],
            "active_turn_diagnostics": {
                "managed_turn_id": "turn-1",
                "request_kind": "message",
                "model": "gpt-5",
                "reasoning": "high",
                "prompt_preview": "Inspect the live thread state",
                "backend_thread_id": "backend-thread-1",
                "backend_turn_id": "backend-turn-1",
                "stream_available": True,
                "last_event_at": "2026-03-17T10:11:12Z",
                "last_event_type": "tool_completed",
                "last_event_summary": "tool: status-check",
                "stalled": False,
                "stall_reason": None,
            },
            "events": [],
        },
    }

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = payload, token_env
        calls.append((method, url, params))
        for suffix, response in responses.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    runner = CliRunner()
    list_result = runner.invoke(
        pma_app,
        [
            "thread",
            "list",
            "--agent",
            "codex",
            "--status",
            "idle",
            "--repo",
            "repo-1",
            "--path",
            str(tmp_path),
        ],
    )
    info_result = runner.invoke(
        pma_app, ["thread", "info", "--id", "thread-1", "--path", str(tmp_path)]
    )
    status_result = runner.invoke(
        pma_app, ["thread", "status", "--id", "thread-1", "--path", str(tmp_path)]
    )
    tail_result = runner.invoke(
        pma_app, ["thread", "tail", "--id", "thread-1", "--path", str(tmp_path)]
    )

    assert list_result.exit_code == 0
    assert "thread-1 agent=codex status=idle" in list_result.stdout
    assert info_result.exit_code == 0
    assert '"managed_thread_id": "thread-1"' in info_result.stdout
    assert '"latest_assistant_text": "assistant conclusion"' in info_result.stdout
    assert status_result.exit_code == 0
    assert (
        "id=thread-1 agent=codex repo=repo-1 operator_status=reusable runtime_status=completed"
        in status_result.stdout
    )
    assert (
        "active_turn: kind=message model=gpt-5 reasoning=high stream=yes stalled=no"
        in status_result.stdout
    )
    assert "prompt: Inspect the live thread state" in status_result.stdout
    assert (
        "last_event: tool_completed @10:11:12 tool: status-check"
        in status_result.stdout
    )
    assert "assistant_text_excerpt:" in status_result.stdout
    assert "assistant conclusion" in status_result.stdout
    assert tail_result.exit_code == 0
    assert (
        "managed_turn_id=turn-1 turn_status=ok activity=completed" in tail_result.stdout
    )
    assert (
        "active_turn: kind=message model=gpt-5 reasoning=high stream=yes stalled=no"
        in tail_result.stdout
    )

    assert calls == [
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads",
            {
                "agent": "codex",
                "status": "idle",
                "resource_kind": "repo",
                "resource_id": "repo-1",
                "limit": 200,
            },
        ),
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1",
            None,
        ),
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1/status",
            {"limit": 200, "level": "info"},
        ),
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1/tail",
            {"limit": 200, "level": "info"},
        ),
    ]


def test_pma_cli_thread_status_info_applies_post_filter_limit(
    monkeypatch, tmp_path: Path
) -> None:
    seen_params: list[dict[str, object] | None] = []
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env
        seen_params.append(params)
        if url.endswith("/threads/thread-1/status"):
            return {
                "managed_thread_id": "thread-1",
                "thread": {
                    "managed_thread_id": "thread-1",
                    "agent": "codex",
                    "repo_id": "repo-1",
                    "status": "running",
                    "lifecycle_status": "active",
                },
                "status": "running",
                "turn": {
                    "managed_turn_id": "turn-1",
                    "status": "running",
                    "activity": "working",
                    "phase": "streaming",
                },
                "recent_progress": [
                    {
                        "event_type": "assistant_update",
                        "summary": "The",
                        "received_at": "2026-03-17T10:11:11Z",
                        "event_id": 20,
                    },
                    {
                        "event_type": "assistant_update",
                        "summary": "The user wants me to sleep 15 seconds",
                        "received_at": "2026-03-17T10:11:11Z",
                        "event_id": 26,
                    },
                    {
                        "event_type": "tool_started",
                        "summary": "tool: bash",
                        "received_at": "2026-03-17T10:11:12Z",
                        "event_id": 27,
                    },
                    {
                        "event_type": "tool_started",
                        "summary": "tool: bash",
                        "received_at": "2026-03-17T10:11:12Z",
                        "event_id": 28,
                    },
                    {
                        "event_type": "progress",
                        "summary": "No decoder for method: session.diff",
                        "received_at": "2026-03-17T10:11:13Z",
                        "event_id": 29,
                    },
                    {
                        "event_type": "tool_completed",
                        "summary": "tool: bash",
                        "received_at": "2026-03-17T10:11:14Z",
                        "event_id": 30,
                    },
                ],
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    result = CliRunner().invoke(
        pma_app,
        [
            "thread",
            "status",
            "--id",
            "thread-1",
            "--limit",
            "2",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen_params == [{"limit": 200, "level": "info"}]
    assert (
        "[10:11:11] #20-#26 assistant_update: The user wants me to sleep 15 seconds"
        not in result.stdout
    )
    assert "[10:11:12] #27-#28 tool_started: tool: bash (x2)" in result.stdout
    assert "[10:11:14] #30 tool_completed: tool: bash" in result.stdout
    assert "No decoder for method: session.diff" not in result.stdout


def test_pma_cli_thread_queue_renders_table(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env, params
        assert url.endswith("/hub/pma/threads/thread-1/queue")
        return {
            "managed_thread_id": "thread-1",
            "queue_depth": 2,
            "queued_turns": [
                {
                    "managed_turn_id": "turn-2",
                    "prompt": "Reply: queue-B-r3",
                    "enqueued_at": "2026-04-26T14:44:06Z",
                    "position": 1,
                },
                {
                    "managed_turn_id": "turn-3",
                    "prompt": "Reply: stress-C",
                    "enqueued_at": "2026-04-26T14:46:04Z",
                    "position": 2,
                },
            ],
        }

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    result = CliRunner().invoke(
        pma_app,
        ["thread", "queue", "--id", "thread-1", "--path", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "queued_turn_id" in result.stdout
    assert '"Reply: queue-B-r3"' in result.stdout
    assert "2026-04-26T14:44:06Z" in result.stdout
    assert "turn-3" in result.stdout


def test_pma_cli_thread_queue_handles_only_malformed_rows(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env, params
        assert url.endswith("/hub/pma/threads/thread-1/queue")
        return {
            "managed_thread_id": "thread-1",
            "queue_depth": 2,
            "queued_turns": ["bad-row", 123],
        }

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    result = CliRunner().invoke(
        pma_app,
        ["thread", "queue", "--id", "thread-1", "--path", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "No queued turns"


def test_pma_cli_thread_cancel_and_clear_queue_commands(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    calls: list[tuple[str, str]] = []

    def _fake_request_json_with_status(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
        timeout=30.0,
    ):
        _ = payload, token_env, params, timeout
        calls.append((method, url))
        return (
            200,
            {
                "status": "ok",
                "managed_turn_id": "turn-2",
                "position": 1,
            },
        )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = payload, token_env, params
        calls.append((method, url))
        return {
            "status": "ok",
            "managed_thread_id": "thread-1",
            "cleared_count": 2,
        }

    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _fake_request_json_with_status
    )
    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    runner = CliRunner()
    cancel_result = runner.invoke(
        pma_app,
        [
            "thread",
            "cancel-queued",
            "--id",
            "thread-1",
            "--turn",
            "turn-2",
            "--path",
            str(tmp_path),
        ],
    )
    clear_result = runner.invoke(
        pma_app,
        ["thread", "clear-queue", "--id", "thread-1", "--path", str(tmp_path)],
    )

    assert cancel_result.exit_code == 0
    assert "Cancelled queued turn turn-2 (was position 1)" in cancel_result.stdout
    assert clear_result.exit_code == 0
    assert "Cleared 2 queued turns" in clear_result.stdout
    assert calls == [
        ("POST", "http://127.0.0.1:4321/hub/pma/threads/thread-1/queue/turn-2/cancel"),
        ("POST", "http://127.0.0.1:4321/hub/pma/threads/thread-1/queue/clear"),
    ]


def test_pma_cli_thread_status_output_renders_paginated_assistant_text(
    monkeypatch, tmp_path: Path
) -> None:
    assistant_text = "\n".join(f"line {index}" for index in range(1, 26))

    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    monkeypatch.setattr(
        pma_thread_commands.shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): SimpleNamespace(columns=80, lines=12),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env, params
        if url.endswith("/threads/thread-1/status"):
            return {
                "managed_thread_id": "thread-1",
                "status": "completed",
                "operator_status": "reusable",
                "status_reason": "managed_turn_completed",
                "thread": {
                    "agent": "codex",
                    "repo_id": "repo-1",
                    "status": "completed",
                    "lifecycle_status": "active",
                },
                "turn": {
                    "managed_turn_id": "turn-1",
                    "status": "ok",
                    "activity": "completed",
                    "phase": "finalize",
                },
                "latest_turn_id": "turn-1",
                "latest_assistant_text": assistant_text,
                "latest_output_excerpt": "line 1",
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    result = CliRunner().invoke(
        pma_app,
        [
            "thread",
            "status",
            "--id",
            "thread-1",
            "--output",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "assistant_text lines 1:20 of 25:" in result.stdout
    assert "line 1" in result.stdout
    assert "line 20" in result.stdout
    assert "line 21" not in result.stdout
    assert "--continue or --lines 21:25" in result.stdout


def test_pma_cli_thread_status_lines_header_clarifies_slice(
    monkeypatch, tmp_path: Path
) -> None:
    assistant_text = "\n".join(f"line {index}" for index in range(1, 6))

    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env, params
        if url.endswith("/threads/thread-1/status"):
            return {
                "managed_thread_id": "thread-1",
                "status": "completed",
                "operator_status": "reusable",
                "status_reason": "managed_turn_completed",
                "thread": {
                    "agent": "codex",
                    "repo_id": "repo-1",
                    "status": "completed",
                    "lifecycle_status": "active",
                },
                "turn": {
                    "managed_turn_id": "turn-1",
                    "status": "ok",
                    "activity": "completed",
                    "phase": "finalize",
                },
                "latest_turn_id": "turn-1",
                "latest_assistant_text": assistant_text,
                "latest_output_excerpt": "line 1",
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    result = CliRunner().invoke(
        pma_app,
        [
            "thread",
            "status",
            "--id",
            "thread-1",
            "--output",
            "--lines",
            "2:3",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "assistant_text lines 2:3 of 5:" in result.stdout
    assert "line 2" in result.stdout
    assert "line 3" in result.stdout
    assert "line 1\nline 2\nline 3" not in result.stdout


def test_pma_cli_thread_output_supports_continue_and_output_file(
    monkeypatch, tmp_path: Path
) -> None:
    assistant_text = "\n".join(f"line {index}" for index in range(1, 26))

    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    monkeypatch.setattr(
        pma_thread_commands.shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): SimpleNamespace(columns=80, lines=12),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env
        if url.endswith("/threads/thread-1/turns"):
            assert params == {"limit": 1}
            return {"turns": [{"managed_turn_id": "turn-1"}]}
        if url.endswith("/threads/thread-1/turns/turn-1"):
            return {
                "turn": {"managed_turn_id": "turn-1", "assistant_text": assistant_text}
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)
    runner = CliRunner()

    first = runner.invoke(
        pma_app,
        ["thread", "output", "--id", "thread-1", "--path", str(tmp_path)],
    )
    second = runner.invoke(
        pma_app,
        [
            "thread",
            "output",
            "--id",
            "thread-1",
            "--continue",
            "--path",
            str(tmp_path),
        ],
    )
    output_path = tmp_path / "assistant.txt"
    file_result = runner.invoke(
        pma_app,
        [
            "thread",
            "output",
            "--id",
            "thread-1",
            "--lines",
            "24:25",
            "--output-file",
            str(output_path),
            "--path",
            str(tmp_path),
        ],
    )

    assert first.exit_code == 0, first.output
    assert "line 20" in first.stdout
    assert "line 21" not in first.stdout
    assert second.exit_code == 0, second.output
    assert "line 21" in second.stdout
    assert "line 25" in second.stdout
    assert "line 1" not in second.stdout
    assert file_result.exit_code == 0, file_result.output
    assert output_path.read_text(encoding="utf-8") == "line 24\nline 25"


def test_pma_cli_thread_subscribe_posts_thread_scoped_payload(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    calls: list[tuple[str, str, dict[str, object]]] = []

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = token_env, params
        calls.append((method, url, payload or {}))
        return {
            "subscription": {
                "subscription_id": "sub-1",
                "thread_id": "thread-1",
                "lane_id": "lane-1",
                "event_types": ["managed_thread_completed"],
            }
        }

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    result = CliRunner().invoke(
        pma_app,
        [
            "thread",
            "subscribe",
            "--id",
            "thread-1",
            "--event",
            "managed_thread_completed",
            "--lane",
            "lane-1",
            "--persistent",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "subscription_id=sub-1 scope=thread thread_id=thread-1 lane_id=lane-1" in (
        result.stdout
    )
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/subscriptions",
            {
                "thread_id": "thread-1",
                "event_types": ["managed_thread_completed"],
                "notify_once": False,
                "lane_id": "lane-1",
            },
        )
    ]


def test_pma_cli_thread_compact_dry_run_with_status_filter(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / ".codex-autorunner").mkdir()
    (tmp_path / ".codex-autorunner" / "config.yml").write_text(
        "pma: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = payload, token_env
        calls.append((method, url, params))
        if method == "GET" and url.endswith("/hub/pma/threads"):
            return {
                "threads": [
                    {
                        "managed_thread_id": "thread-1",
                        "agent": "codex",
                        "status": "completed",
                        "status_reason": "managed_turn_completed",
                        "repo_id": "repo-1",
                        "resource_kind": "repo",
                        "resource_id": "repo-1",
                        "chat_bound": True,
                        "cleanup_protected": True,
                    },
                    {
                        "managed_thread_id": "thread-2",
                        "agent": "codex",
                        "status": "completed",
                        "status_reason": "managed_turn_completed",
                        "repo_id": "repo-1",
                        "resource_kind": "repo",
                        "resource_id": "repo-1",
                    },
                ]
            }
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "compact",
            "--status",
            "completed",
            "--summary",
            "cleanup",
            "--dry-run",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Dry run summary: 2 threads would be compacted" in result.stdout
    assert "thread-1 agent=codex status=completed" in result.stdout
    assert "chat_bound=yes" in result.stdout
    assert "protected=yes" in result.stdout
    assert "thread-2 agent=codex status=completed" in result.stdout
    assert calls == [
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads",
            {"status": "completed", "limit": 1000},
        )
    ]

    audit_entries = PmaAuditLog(tmp_path).list_recent(
        action_type=PmaActionType.SESSION_COMPACT
    )
    assert audit_entries
    assert audit_entries[-1].details["mode"] == "dry_run"
    assert audit_entries[-1].details["thread_ids"] == ["thread-1", "thread-2"]


def test_pma_cli_thread_compact_executes_batch_for_completed_threads(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / ".codex-autorunner").mkdir()
    (tmp_path / ".codex-autorunner" / "config.yml").write_text(
        "pma: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    calls: list[
        tuple[
            str,
            str,
            dict[str, object] | None,
            dict[str, object] | None,
        ]
    ] = []

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = token_env
        calls.append((method, url, payload, params))
        if method == "GET" and url.endswith("/hub/pma/threads"):
            return {
                "threads": [
                    {
                        "managed_thread_id": "thread-1",
                        "agent": "codex",
                        "status": "completed",
                        "status_reason": "managed_turn_completed",
                        "repo_id": "repo-1",
                        "resource_kind": "repo",
                        "resource_id": "repo-1",
                        "chat_bound": True,
                        "cleanup_protected": True,
                    },
                    {
                        "managed_thread_id": "thread-2",
                        "agent": "codex",
                        "status": "completed",
                        "status_reason": "managed_turn_completed",
                        "repo_id": "repo-1",
                        "resource_kind": "repo",
                        "resource_id": "repo-1",
                    },
                ]
            }
        if method == "POST" and url.endswith("/hub/pma/threads/thread-1/compact"):
            return {
                "thread": {"managed_thread_id": "thread-1", "compact_seed": "cleanup"}
            }
        if method == "POST" and url.endswith("/hub/pma/threads/thread-2/compact"):
            return {
                "thread": {"managed_thread_id": "thread-2", "compact_seed": "cleanup"}
            }
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "compact",
            "--status",
            "completed",
            "--summary",
            "cleanup",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Dry run summary: 2 threads would be compacted" in result.stdout
    assert "Compacted thread-1" in result.stdout
    assert "Compacted thread-2" in result.stdout
    assert "Compacted 2 threads" in result.stdout
    assert calls == [
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads",
            None,
            {"status": "completed", "limit": 1000},
        ),
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1/compact",
            {"summary": "cleanup", "reset_backend": True},
            None,
        ),
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads/thread-2/compact",
            {"summary": "cleanup", "reset_backend": True},
            None,
        ),
    ]

    audit_entries = PmaAuditLog(tmp_path).list_recent(
        action_type=PmaActionType.SESSION_COMPACT
    )
    assert audit_entries
    assert audit_entries[-1].details["mode"] == "result"
    assert audit_entries[-1].details["thread_ids"] == ["thread-1", "thread-2"]


def test_pma_cli_thread_compact_all_requires_force(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".codex-autorunner").mkdir()
    (tmp_path / ".codex-autorunner" / "config.yml").write_text(
        "pma: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "compact",
            "--all",
            "--summary",
            "cleanup",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "--force is required" in result.output


def test_pma_cli_thread_send_reports_queued_busy_thread(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    captured: dict[str, object] = {}

    def _fake_request_json_with_status(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        timeout=None,
    ):
        _ = method, token_env, timeout
        captured["url"] = url
        captured["payload"] = payload
        return (
            200,
            {
                "status": "ok",
                "send_state": "accepted",
                "execution_state": "running",
                "managed_turn_id": "turn-2",
                "active_managed_turn_id": "turn-1",
                "queue_depth": 1,
                "delivered_message": "follow up",
                "assistant_text": "",
                "notification": {
                    "mode": "terminal",
                    "subscription": {"lane_id": "pma:default"},
                },
            },
        )

    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _fake_request_json_with_status
    )

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "follow up",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "send_state=accepted managed_turn_id=turn-2" in result.stdout
    assert "active_managed_turn_id=turn-1" in result.stdout
    assert "queue_depth=1" in result.stdout
    assert "subscription=terminal lane=pma:default" in result.stdout
    assert "delivered message:\nfollow up\n" in result.stdout
    assert captured["url"] == "http://127.0.0.1:4321/hub/pma/threads/thread-1/messages"
    assert captured["payload"] == {
        "message": "follow up",
        "busy_policy": "queue",
        "defer_execution": True,
        "wait_for_confirmation": True,
    }


def test_pma_cli_thread_send_reads_message_from_file(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    captured: dict[str, object] = {}
    message_path = tmp_path / "prompt.md"
    message_path.write_text("literal `glm-5-turbo`\nsecond line\n", encoding="utf-8")

    def _fake_request_json_with_status(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        timeout=None,
    ):
        _ = method, url, token_env, timeout
        captured["payload"] = payload
        return (
            200,
            {
                "status": "ok",
                "send_state": "accepted",
                "execution_state": "running",
                "managed_turn_id": "turn-3",
                "delivered_message": payload["message"],
                "assistant_text": "",
            },
        )

    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _fake_request_json_with_status
    )

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message-file",
            str(message_path),
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert captured["payload"] == {
        "message": "literal `glm-5-turbo`\nsecond line\n",
        "busy_policy": "queue",
        "defer_execution": True,
        "wait_for_confirmation": True,
    }
    assert "delivered message:\nliteral `glm-5-turbo`\nsecond line\n" in result.stdout
    assert "\nassistant:\n" not in result.stdout


def test_pma_cli_thread_send_reads_message_from_stdin(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    captured: dict[str, object] = {}

    def _fake_request_json_with_status(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        timeout=None,
    ):
        _ = method, url, token_env, timeout
        captured["payload"] = payload
        return (
            200,
            {
                "status": "ok",
                "send_state": "accepted",
                "execution_state": "running",
                "managed_turn_id": "turn-4",
                "delivered_message": payload["message"],
                "assistant_text": "",
            },
        )

    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _fake_request_json_with_status
    )

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message-stdin",
            "--path",
            str(tmp_path),
        ],
        input="stdin payload with backticks `glm-5-turbo`\n",
    )

    assert result.exit_code == 0
    assert captured["payload"] == {
        "message": "stdin payload with backticks `glm-5-turbo`\n",
        "busy_policy": "queue",
        "defer_execution": True,
        "wait_for_confirmation": True,
    }
    assert (
        "delivered message:\nstdin payload with backticks `glm-5-turbo`\n"
        in result.stdout
    )


def test_pma_cli_thread_send_no_wait_sets_enqueue_mode(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    captured: dict[str, object] = {}

    def _fake_request_json_with_status(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        timeout=None,
    ):
        _ = method, url, token_env, timeout
        captured["payload"] = payload
        return (
            200,
            {
                "status": "ok",
                "send_state": "enqueued",
                "execution_state": "running",
                "managed_turn_id": "turn-5",
                "delivered_message": "follow up",
            },
        )

    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _fake_request_json_with_status
    )

    result = CliRunner().invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "follow up",
            "--no-wait",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "send_state=enqueued managed_turn_id=turn-5" in result.stdout
    assert captured["payload"] == {
        "message": "follow up",
        "busy_policy": "queue",
        "defer_execution": True,
        "wait_for_confirmation": False,
    }


def test_pma_cli_thread_send_uses_extended_post_timeout(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    captured: dict[str, object] = {}

    def _fake_request_json_with_status(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        timeout=None,
    ):
        _ = method, url, payload, token_env
        captured["timeout"] = timeout
        return (
            200,
            {
                "status": "ok",
                "send_state": "accepted",
                "execution_state": "running",
                "managed_turn_id": "turn-timeout",
                "delivered_message": "follow up",
                "assistant_text": "",
            },
        )

    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _fake_request_json_with_status
    )

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "follow up",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert (
        captured["timeout"]
        == pma_control_plane.MANAGED_THREAD_SEND_REQUEST_TIMEOUT_SECONDS
    )


def test_pma_cli_thread_send_no_wait_does_not_error_when_timeout_recovery_fails(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    status_payloads = itertools.cycle(
        [
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 0,
                "queued_turns": [],
            }
        ]
    )

    _status_req = _make_next_status_request_json(status_payloads)
    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _always_timeout_with_status
    )
    monkeypatch.setattr(pma_thread_commands, "_request_json", _status_req)
    monkeypatch.setattr(pma_control_plane, "request_json", _status_req)
    monotonic_values = iter([100.0, 116.0])
    monkeypatch.setattr(
        pma_control_plane.time, "monotonic", lambda: next(monotonic_values, 116.0)
    )
    monkeypatch.setattr(pma_control_plane.time, "sleep", lambda seconds: None)

    result = CliRunner().invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "follow up",
            "--no-wait",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "send_state=enqueued managed_turn_id=" in result.stdout
    assert "Timed out waiting for enqueue confirmation" in result.stdout


def test_pma_cli_thread_send_recovers_timeout_from_status_probe(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    status_payloads = iter(
        [
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 0,
            },
            {
                "thread": {
                    "last_turn_id": "turn-2",
                    "latest_turn_id": "turn-2",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-2", "status": "running"},
                "queue_depth": 0,
                "queued_turns": [],
            },
        ]
    )

    _status_req = _make_next_status_request_json(status_payloads)
    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _always_timeout_with_status
    )
    monkeypatch.setattr(pma_thread_commands, "_request_json", _status_req)
    monkeypatch.setattr(pma_control_plane, "request_json", _status_req)
    monotonic_values = itertools.count(100.0, 0.01)
    monkeypatch.setattr(
        pma_control_plane.time, "monotonic", lambda: next(monotonic_values)
    )
    monkeypatch.setattr(pma_control_plane.time, "sleep", lambda seconds: None)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "follow up",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "send_state=accepted managed_turn_id=turn-2" in result.stdout
    assert "delivered message:\nfollow up\n" in result.stdout
    assert "recovered delivery from thread status" in result.stdout


def test_pma_cli_thread_send_recovers_queued_timeout_from_status_probe(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    status_payloads = iter(
        [
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 0,
                "queued_turns": [],
            },
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 1,
                "queued_turns": [
                    {
                        "managed_turn_id": "turn-2",
                        "prompt_preview": "follow up",
                        "state": "queued",
                    }
                ],
            },
        ]
    )

    _status_req = _make_next_status_request_json(status_payloads)
    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _always_timeout_with_status
    )
    monkeypatch.setattr(pma_thread_commands, "_request_json", _status_req)
    monkeypatch.setattr(pma_control_plane, "request_json", _status_req)
    monotonic_seq = itertools.count(100.0, 1.0)
    monkeypatch.setattr(
        pma_control_plane.time, "monotonic", lambda: next(monotonic_seq)
    )
    monkeypatch.setattr(pma_control_plane.time, "sleep", lambda seconds: None)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "follow up",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert (
        "send_state=queued managed_turn_id=turn-2 active_managed_turn_id=turn-1 "
        "queue_depth=1" in result.stdout
    )
    assert "delivered message:\nfollow up\n" in result.stdout
    assert "recovered delivery from thread status" in result.stdout


def test_pma_cli_thread_send_recovers_queued_timeout_when_last_turn_advances(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    status_payloads = iter(
        [
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 0,
                "queued_turns": [],
            },
            {
                "thread": {
                    "last_turn_id": "turn-2",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "follow up",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 1,
                "queued_turns": [
                    {
                        "managed_turn_id": "turn-2",
                        "prompt_preview": "follow up",
                        "state": "queued",
                    }
                ],
            },
        ]
    )

    _status_req = _make_next_status_request_json(status_payloads)
    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _always_timeout_with_status
    )
    monkeypatch.setattr(pma_thread_commands, "_request_json", _status_req)
    monkeypatch.setattr(pma_control_plane, "request_json", _status_req)
    monotonic_values = iter([100.0, 115.0])
    monkeypatch.setattr(
        pma_control_plane.time, "monotonic", lambda: next(monotonic_values, 115.0)
    )
    monkeypatch.setattr(pma_control_plane.time, "sleep", lambda seconds: None)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "follow up",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert (
        "send_state=queued managed_turn_id=turn-2 active_managed_turn_id=turn-1 "
        "queue_depth=1" in result.stdout
    )
    assert "recovered delivery from thread status" in result.stdout


def test_pma_cli_thread_send_timeout_recovery_keeps_queued_turn_rows_aligned(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    status_payloads = iter(
        [
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 0,
                "queued_turns": [],
            },
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 2,
                "queued_turns": [
                    {
                        "managed_turn_id": "turn-missing-preview",
                        "prompt_preview": "",
                        "state": "queued",
                    },
                    {
                        "managed_turn_id": "turn-2",
                        "prompt_preview": "follow up",
                        "state": "queued",
                    },
                ],
            },
        ]
    )

    _status_req = _make_next_status_request_json(status_payloads)
    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _always_timeout_with_status
    )
    monkeypatch.setattr(pma_thread_commands, "_request_json", _status_req)
    monkeypatch.setattr(pma_control_plane, "request_json", _status_req)
    monotonic_seq = itertools.count(100.0, 1.0)
    monkeypatch.setattr(
        pma_control_plane.time, "monotonic", lambda: next(monotonic_seq)
    )
    monkeypatch.setattr(pma_control_plane.time, "sleep", lambda seconds: None)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "follow up",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert (
        "send_state=queued managed_turn_id=turn-2 active_managed_turn_id=turn-1 "
        "queue_depth=2" in result.stdout
    )
    assert "turn-missing-preview" not in result.stdout


def test_pma_cli_thread_send_timeout_recovery_preview_match_prefers_queued_turn_id(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    status_payloads = iter(
        [
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 0,
                "queued_turns": [],
            },
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "follow up",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 1,
                "queued_turns": [
                    {
                        "managed_turn_id": "turn-new",
                        "prompt_preview": "follow up",
                        "state": "queued",
                    }
                ],
            },
        ]
    )

    _status_req = _make_next_status_request_json(status_payloads)
    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _always_timeout_with_status
    )
    monkeypatch.setattr(pma_thread_commands, "_request_json", _status_req)
    monkeypatch.setattr(pma_control_plane, "request_json", _status_req)
    monotonic_values = iter([100.0, 103.0])
    monkeypatch.setattr(
        pma_control_plane.time, "monotonic", lambda: next(monotonic_values, 103.0)
    )
    monkeypatch.setattr(pma_control_plane.time, "sleep", lambda seconds: None)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "follow up",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert (
        "send_state=queued managed_turn_id=turn-new active_managed_turn_id=turn-1 "
        "queue_depth=1" in result.stdout
    )


def test_pma_cli_thread_send_recovers_timeout_from_redacted_active_prompt(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    message_body = "please use sk-abcdefghijklmnopqrstuvwxyz1234567890 now"
    status_payloads = iter(
        [
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "active_turn_diagnostics": {"prompt_preview": "previous prompt"},
                "queue_depth": 0,
                "queued_turns": [],
            },
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-2", "status": "running"},
                "active_turn_diagnostics": {
                    "prompt_preview": "please use sk-[REDACTED] now"
                },
                "queue_depth": 0,
                "queued_turns": [],
            },
        ]
    )

    _status_req = _make_next_status_request_json(status_payloads)
    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _always_timeout_with_status
    )
    monkeypatch.setattr(pma_thread_commands, "_request_json", _status_req)
    monkeypatch.setattr(pma_control_plane, "request_json", _status_req)
    monotonic_values = iter([100.0, 103.0])
    monkeypatch.setattr(
        pma_control_plane.time, "monotonic", lambda: next(monotonic_values, 103.0)
    )
    monkeypatch.setattr(pma_control_plane.time, "sleep", lambda seconds: None)

    result = CliRunner().invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            message_body,
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "send_state=accepted managed_turn_id=turn-2" in result.stdout
    assert "recovered delivery from thread status" in result.stdout


def test_pma_cli_thread_send_active_prompt_recovery_ignores_preexisting_queue(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    baseline_payload = {
        "thread": {
            "last_turn_id": "turn-1",
            "latest_turn_id": "turn-1",
            "last_message_preview": "previous prompt",
        },
        "turn": {"managed_turn_id": "turn-1", "status": "running"},
        "active_turn_diagnostics": {"prompt_preview": "previous prompt"},
        "queue_depth": 1,
        "queued_turns": [
            {
                "managed_turn_id": "turn-old",
                "prompt_preview": "follow up",
                "state": "queued",
            }
        ],
    }
    current_payload = {
        "thread": {
            "last_turn_id": "turn-1",
            "latest_turn_id": "turn-1",
            "last_message_preview": "previous prompt",
        },
        "turn": {"managed_turn_id": "turn-1", "status": "running"},
        "active_turn_diagnostics": {"prompt_preview": "follow up"},
        "queue_depth": 1,
        "queued_turns": [
            {
                "managed_turn_id": "turn-old",
                "prompt_preview": "follow up",
                "state": "queued",
            }
        ],
    }
    status_payloads = iter([baseline_payload, current_payload])

    _status_req = _make_next_status_request_json(status_payloads, current_payload)
    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _always_timeout_with_status
    )
    monkeypatch.setattr(pma_thread_commands, "_request_json", _status_req)
    monkeypatch.setattr(pma_control_plane, "request_json", _status_req)
    monotonic_seq = itertools.count(100.0, 1.0)
    monkeypatch.setattr(
        pma_control_plane.time, "monotonic", lambda: next(monotonic_seq)
    )
    monkeypatch.setattr(pma_control_plane.time, "sleep", lambda seconds: None)

    result = CliRunner().invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "follow up",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "Timed out waiting for send confirmation." in result.output


def test_pma_cli_thread_send_retries_timeout_recovery_until_status_catches_up(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    status_payloads = iter(
        [
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 0,
                "queued_turns": [],
            },
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 0,
                "queued_turns": [],
            },
            {
                "thread": {
                    "last_turn_id": "turn-2",
                    "latest_turn_id": "turn-2",
                    "last_message_preview": "follow up",
                },
                "turn": {"managed_turn_id": "turn-2", "status": "running"},
                "queue_depth": 0,
                "queued_turns": [],
            },
        ]
    )

    _status_req = _make_next_status_request_json(status_payloads)
    monotonic_seq = itertools.count(100.0, 1.0)
    sleep_calls: list[float] = []

    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _always_timeout_with_status
    )
    monkeypatch.setattr(pma_thread_commands, "_request_json", _status_req)
    monkeypatch.setattr(pma_control_plane, "request_json", _status_req)
    monkeypatch.setattr(
        pma_control_plane.time, "monotonic", lambda: next(monotonic_seq)
    )
    monkeypatch.setattr(
        pma_control_plane.time, "sleep", lambda seconds: sleep_calls.append(seconds)
    )

    message_path = tmp_path / "prompt.md"
    message_path.write_text("follow up\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message-file",
            str(message_path),
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "send_state=accepted managed_turn_id=turn-2" in result.stdout
    assert "recovered delivery from thread status" in result.stdout
    assert sleep_calls == [
        pma_control_plane.MANAGED_THREAD_SEND_TIMEOUT_RECOVERY_POLL_SECONDS
    ]


def test_pma_cli_thread_send_timeout_warns_before_retry_when_status_unclear(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    status_payloads = itertools.cycle(
        [
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 0,
            },
            {
                "thread": {
                    "last_turn_id": "turn-1",
                    "latest_turn_id": "turn-1",
                    "last_message_preview": "previous prompt",
                },
                "turn": {"managed_turn_id": "turn-1", "status": "running"},
                "queue_depth": 0,
            },
        ]
    )

    _status_req = _make_next_status_request_json(status_payloads)
    monkeypatch.setattr(
        pma_thread_commands, "_request_json_with_status", _always_timeout_with_status
    )
    monkeypatch.setattr(pma_thread_commands, "_request_json", _status_req)
    monkeypatch.setattr(pma_control_plane, "request_json", _status_req)
    monotonic_seq = itertools.count(100.0, 1.0)
    monkeypatch.setattr(
        pma_control_plane.time, "monotonic", lambda: next(monotonic_seq)
    )
    monkeypatch.setattr(pma_control_plane.time, "sleep", lambda seconds: None)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "follow up",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "Timed out waiting for send confirmation." in result.output
    assert "may still have been delivered" in result.output
    assert "Check `car pma thread status --id thread-1" in result.output


def test_pma_cli_thread_send_requires_exactly_one_message_source(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    message_path = tmp_path / "prompt.md"
    message_path.write_text("hello\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "inline",
            "--message-file",
            str(message_path),
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert (
        "Provide exactly one of --message, --message-file, or --message-stdin."
        in result.output
    )


def test_pma_cli_thread_control_commands_use_orchestration_routes(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = token_env, params
        calls.append((method, url, payload))
        if url.endswith("/hub/pma/threads"):
            return {"thread": {"managed_thread_id": "thread-1"}}
        if url.endswith("/hub/pma/threads/thread-1"):
            return {
                "thread": {
                    "managed_thread_id": "thread-1",
                    "lifecycle_status": "archived",
                    "status": "archived",
                }
            }
        if url.endswith("/resume"):
            return {
                "thread": {
                    "managed_thread_id": "thread-1",
                }
            }
        if url.endswith("/archive"):
            return {
                "thread": {
                    "managed_thread_id": "thread-1",
                    "name": "CLI thread",
                    "status": "archived",
                }
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)
    monkeypatch.setattr(
        pma_thread_commands,
        "_fetch_agent_capabilities",
        lambda config, path: {"codex": {"durable_threads"}},
    )

    runner = CliRunner()
    create_result = runner.invoke(
        pma_app,
        [
            "thread",
            "create",
            "--agent",
            "codex",
            "--repo",
            "repo-1",
            "--name",
            "CLI thread",
            "--path",
            str(tmp_path),
        ],
    )
    resume_result = runner.invoke(
        pma_app,
        [
            "thread",
            "resume",
            "--id",
            "thread-1",
            "--path",
            str(tmp_path),
        ],
    )
    archive_result = runner.invoke(
        pma_app,
        ["thread", "archive", "--id", "thread-1", "--path", str(tmp_path)],
    )

    assert create_result.exit_code == 0
    assert create_result.stdout.strip() == "thread-1"
    assert resume_result.exit_code == 0
    assert "Resumed thread-1" in resume_result.stdout
    assert archive_result.exit_code == 0
    assert "Archived thread-1 (CLI thread)" in archive_result.stdout

    assert calls == [
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads",
            {
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": "repo-1",
                "workspace_root": None,
                "name": "CLI thread",
                "context_profile": "car_ambient",
                "notify_on": None,
                "terminal_followup": None,
                "notify_lane": None,
                "notify_once": True,
            },
        ),
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1",
            None,
        ),
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1/resume",
            {},
        ),
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1/archive",
            None,
        ),
    ]


def test_pma_cli_thread_resume_reports_already_active_noop(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = token_env, params
        calls.append((method, url, payload))
        if url.endswith("/hub/pma/threads/thread-1"):
            return {
                "thread": {
                    "managed_thread_id": "thread-1",
                    "lifecycle_status": "active",
                    "status": "completed",
                }
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    result = CliRunner().invoke(
        pma_app,
        ["thread", "resume", "--id", "thread-1", "--path", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Thread thread-1 is already active (status: completed)" in result.stdout
    assert calls == [
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1",
            None,
        )
    ]


def test_pma_cli_thread_interrupt_reports_running_conflict_cleanly(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    monkeypatch.setattr(
        pma_thread_commands,
        "_fetch_agent_capabilities",
        lambda config, path: {"codex": {"interrupt"}},
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env, params
        if url.endswith("/threads/thread-1"):
            return {
                "thread": {
                    "managed_thread_id": "thread-1",
                    "agent": "codex",
                    "status": "completed",
                    "lifecycle_status": "active",
                }
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)
    monkeypatch.setattr(
        pma_thread_commands,
        "_request_json_with_status",
        lambda method, url, payload=None, token_env=None, params=None, timeout=30.0: (
            409,
            {"detail": "Managed thread has no running turn"},
        ),
    )

    result = CliRunner().invoke(
        pma_app,
        ["thread", "interrupt", "--id", "thread-1", "--path", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert (
        "Cannot interrupt: thread is not running (status: completed)" in result.output
    )
    assert "409 Conflict" not in result.output


def test_pma_cli_thread_archive_bulk_uses_bulk_route(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = token_env, params
        calls.append((method, url, payload))
        if url.endswith("/hub/pma/threads/archive"):
            return {
                "threads": [
                    {
                        "managed_thread_id": "thread-1",
                        "name": "Thread One",
                        "status": "archived",
                    },
                    {
                        "managed_thread_id": "thread-2",
                        "name": "Thread Two",
                        "status": "archived",
                    },
                ],
                "archived_count": 2,
                "requested_count": 2,
                "errors": [],
                "error_count": 0,
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    result = CliRunner().invoke(
        pma_app,
        [
            "thread",
            "archive",
            "--ids",
            "thread-1,thread-2",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Archived thread-1 (Thread One)" in result.stdout
    assert "Archived thread-2 (Thread Two)" in result.stdout
    assert "Archived 2 managed threads." in result.stdout
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads/archive",
            {"thread_ids": ["thread-1", "thread-2"]},
        )
    ]


def test_pma_cli_thread_archive_id_is_single_value_not_split_on_commas(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = token_env, params
        calls.append((method, url, payload))
        if url.endswith("/hub/pma/threads/a,b/archive"):
            return {
                "thread": {
                    "managed_thread_id": "a,b",
                    "name": "comma id",
                    "status": "archived",
                }
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    result = CliRunner().invoke(
        pma_app,
        [
            "thread",
            "archive",
            "--id",
            "a,b",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Archived a,b (comma id)" in result.stdout
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads/a,b/archive",
            None,
        )
    ]


def test_pma_cli_thread_archive_reads_ids_from_stdin(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = token_env, params
        calls.append((method, url, payload))
        if url.endswith("/hub/pma/threads/archive"):
            return {
                "threads": [
                    {"managed_thread_id": "thread-1", "status": "archived"},
                    {"managed_thread_id": "thread-2", "status": "archived"},
                ],
                "archived_count": 2,
                "requested_count": 2,
                "errors": [],
                "error_count": 0,
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    result = CliRunner().invoke(
        pma_app,
        [
            "thread",
            "archive",
            "--ids-stdin",
            "--path",
            str(tmp_path),
        ],
        input="thread-1\nthread-2\nthread-1\n",
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads/archive",
            {"thread_ids": ["thread-1", "thread-2"]},
        )
    ]


def test_pma_cli_thread_archive_bulk_json_exits_nonzero_on_errors(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env, params
        if not url.endswith("/hub/pma/threads/archive"):
            raise AssertionError(f"unexpected url: {url}")
        return {
            "threads": [{"managed_thread_id": "thread-1", "status": "archived"}],
            "archived_count": 1,
            "requested_count": 2,
            "errors": [
                {"thread_id": "missing-thread", "detail": "Managed thread not found"}
            ],
            "error_count": 1,
        }

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    result = CliRunner().invoke(
        pma_app,
        [
            "thread",
            "archive",
            "--ids",
            "thread-1,missing-thread",
            "--json",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["archived_count"] == 1
    assert payload["error_count"] == 1


def test_pma_cli_thread_create_rejects_backend_id_option() -> None:
    sig = inspect.signature(pma_thread_commands.pma_thread_spawn)
    assert "backend_id" not in sig.parameters


def test_pma_cli_thread_archive_reports_unreachable_host_port(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _raise_connect_error(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env, params
        raise httpx.ConnectError(
            "[Errno 1] Operation not permitted", request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(pma_thread_commands, "_request_json", _raise_connect_error)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["thread", "archive", "--id", "thread-1", "--path", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Failed to archive managed PMA thread thread-1." in result.output
    assert "Resolved URL: http://127.0.0.1:4321/hub/pma/threads/thread-1/archive" in (
        result.output
    )
    assert "Failure type: hub host/port unreachable." in result.output
    assert "running in this runtime" in result.output


def test_pma_cli_thread_archive_reports_base_path_mismatch(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _raise_http_status(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env, params
        request = httpx.Request("POST", url)
        response = httpx.Response(404, request=request, json={"detail": "Not Found"})
        raise httpx.HTTPStatusError("Not Found", request=request, response=response)

    monkeypatch.setattr(pma_thread_commands, "_request_json", _raise_http_status)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["thread", "archive", "--id", "thread-1", "--path", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Failed to archive managed PMA thread thread-1." in result.output
    assert "Failure type: possible base-path mismatch." in result.output
    assert "HTTP status: 404" in result.output
    assert "server.base_path" in result.output


def test_pma_cli_thread_archive_preserves_not_found_detail(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _raise_thread_not_found(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env, params
        request = httpx.Request("POST", url)
        response = httpx.Response(
            404, request=request, json={"detail": "Managed thread not found"}
        )
        raise httpx.HTTPStatusError("Not Found", request=request, response=response)

    monkeypatch.setattr(pma_thread_commands, "_request_json", _raise_thread_not_found)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["thread", "archive", "--id", "thread-missing", "--path", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Failed to archive managed PMA thread thread-missing." in result.output
    assert "Failure type: HTTP status 404." in result.output
    assert "Server detail: Managed thread not found" in result.output
    assert "possible base-path mismatch" not in result.output


def test_pma_cli_thread_spawn_defaults_agent_for_agent_workspace(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None, dict[str, object] | None]] = (
        []
    )

    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = token_env
        calls.append((method, url, payload, params))
        if method == "POST" and url.endswith("/hub/pma/threads"):
            return {
                "thread": {
                    "managed_thread_id": "thread-zc-1",
                    "agent": "zeroclaw",
                    "resource_kind": "agent_workspace",
                    "resource_id": "zc-main",
                }
            }
        if method == "GET" and url.endswith("/hub/pma/threads"):
            return {
                "threads": [
                    {
                        "managed_thread_id": "thread-zc-1",
                        "agent": "zeroclaw",
                        "status": "idle",
                        "status_reason": "thread_created",
                        "resource_kind": "agent_workspace",
                        "resource_id": "zc-main",
                    }
                ]
            }
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    runner = CliRunner()
    create_result = runner.invoke(
        pma_app,
        [
            "thread",
            "spawn",
            "--resource-kind",
            "agent_workspace",
            "--resource-id",
            "zc-main",
            "--name",
            "ZeroClaw Main",
            "--path",
            str(tmp_path),
        ],
    )
    list_result = runner.invoke(
        pma_app,
        [
            "thread",
            "list",
            "--resource-kind",
            "agent_workspace",
            "--resource-id",
            "zc-main",
            "--path",
            str(tmp_path),
        ],
    )

    assert create_result.exit_code == 0
    assert create_result.stdout.strip() == "thread-zc-1"
    assert list_result.exit_code == 0
    assert "thread-zc-1 agent=zeroclaw status=idle" in list_result.stdout
    assert "owner=agent_workspace:zc-main" in list_result.stdout

    assert calls == [
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads",
            {
                "agent": None,
                "resource_kind": "agent_workspace",
                "resource_id": "zc-main",
                "workspace_root": None,
                "name": "ZeroClaw Main",
                "context_profile": "none",
                "notify_on": None,
                "terminal_followup": None,
                "notify_lane": None,
                "notify_once": True,
            },
            None,
        ),
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads",
            None,
            {
                "resource_kind": "agent_workspace",
                "resource_id": "zc-main",
                "limit": 200,
            },
        ),
    ]


def test_pma_cli_thread_list_json_emits_array(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = payload, token_env, params
        assert method == "GET"
        assert url == "http://127.0.0.1:4321/hub/pma/threads"
        return {
            "threads": [
                {
                    "managed_thread_id": "thread-1",
                    "agent": "codex",
                    "status": "idle",
                    "status_reason": "thread_created",
                },
                {
                    "managed_thread_id": "thread-2",
                    "agent": "opencode",
                    "status": "running",
                    "status_reason": "turn_active",
                },
            ]
        }

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["thread", "list", "--json", "--path", str(tmp_path)],
    )

    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, list)
    assert [item["managed_thread_id"] for item in parsed] == ["thread-1", "thread-2"]
    assert all(isinstance(item, dict) for item in parsed)


def test_pma_cli_thread_list_ndjson_emits_one_object_per_line(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = payload, token_env, params
        assert method == "GET"
        assert url == "http://127.0.0.1:4321/hub/pma/threads"
        return {
            "threads": [
                {
                    "managed_thread_id": "thread-1",
                    "agent": "codex",
                    "status": "idle",
                },
                {
                    "managed_thread_id": "thread-2",
                    "agent": "opencode",
                    "status": "running",
                },
            ]
        }

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["thread", "list", "--ndjson", "--path", str(tmp_path)],
    )

    assert result.exit_code == 0
    parsed = [json.loads(line) for line in result.stdout.strip().splitlines()]
    assert [item["managed_thread_id"] for item in parsed] == ["thread-1", "thread-2"]


def test_pma_cli_thread_list_rejects_json_and_ndjson_together(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["thread", "list", "--json", "--ndjson", "--path", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "Choose only one of --json or --ndjson." in result.output


def test_pma_cli_thread_fork_posts_to_api(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    calls: list[tuple[str, str, object]] = []

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = token_env, params
        calls.append((method, url, payload))
        return {
            "thread": {
                "managed_thread_id": "thread-forked-1",
                "agent": "hermes",
            }
        }

    monkeypatch.setattr(pma_thread_commands, "_request_json", _fake_request_json)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "fork",
            "--id",
            "thread-source-1",
            "--name",
            "Forked Hermes thread",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "thread-forked-1"
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads/thread-source-1/fork",
            {"name": "Forked Hermes thread"},
        )
    ]


def test_pma_cli_thread_spawn_rejects_invalid_context_profile(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_thread_commands,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "spawn",
            "--agent",
            "codex",
            "--repo",
            "repo-1",
            "--context-profile",
            "car-ambent",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "--context-profile must be one of: car_core, car_ambient, none" in (
        result.stdout
    )
