from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.agents.opencode import stream_lifecycle as stream_lifecycle_module
from codex_autorunner.agents.opencode.protocol_payload import PERMISSION_ALLOW
from codex_autorunner.agents.opencode.runtime import (
    OpenCodeTurnOutput,
    collect_opencode_output_from_events,
)
from codex_autorunner.agents.opencode.turn_lifecycle import (
    OpenCodeTurnLifecycleState,
)
from codex_autorunner.agents.opencode.turn_runtime import (
    OpenCodeTurnCommand,
    cancel_and_wait,
    observe_turn_runtime,
    pre_connect_event_stream,
    start_command,
)
from codex_autorunner.core.sse import SSEEvent


@pytest.mark.asyncio
async def test_turn_runtime_starts_prompt_and_review_commands() -> None:
    accepted: list[tuple[str, Any]] = []

    async def _accept_prompt() -> dict[str, str]:
        return {"id": "prompt-turn"}

    async def _accept_review() -> dict[str, str]:
        return {"id": "review-turn"}

    prompt = start_command(
        OpenCodeTurnCommand(
            kind="prompt",
            conversation_id="session-1",
            executor=_accept_prompt,
            on_acceptance=lambda result: _record(accepted, "prompt", result),
        )
    )
    review = start_command(
        OpenCodeTurnCommand(
            kind="review",
            conversation_id="session-1",
            executor=_accept_review,
            on_acceptance=lambda result: _record(accepted, "review", result),
        )
    )

    assert await prompt.task == {"id": "prompt-turn"}
    assert await review.task == {"id": "review-turn"}
    assert accepted == [
        ("prompt", {"id": "prompt-turn"}),
        ("review", {"id": "review-turn"}),
    ]


@pytest.mark.asyncio
async def test_turn_runtime_command_rejection_is_terminal_error() -> None:
    async def _collect() -> OpenCodeTurnOutput:
        await asyncio.sleep(1)
        return OpenCodeTurnOutput(text="late")

    async def _reject() -> None:
        raise RuntimeError("command rejected")

    result = await observe_turn_runtime(
        collect_task=asyncio.create_task(_collect()),
        command_task=asyncio.create_task(_reject()),
        raw_events=lambda: [],
    )

    assert result.output.error == "command rejected"
    assert result.lifecycle.state is OpenCodeTurnLifecycleState.FAILED


@pytest.mark.asyncio
async def test_turn_runtime_waits_for_command_after_stream_terminal() -> None:
    command_completed = asyncio.Event()

    async def _collect() -> OpenCodeTurnOutput:
        return OpenCodeTurnOutput(text="observed", terminal_signal="session.idle")

    async def _accept_late() -> dict[str, str]:
        await asyncio.sleep(0)
        command_completed.set()
        return {}

    result = await observe_turn_runtime(
        collect_task=asyncio.create_task(_collect()),
        command_task=asyncio.create_task(_accept_late()),
        raw_events=lambda: [{"method": "session.idle", "params": {}}],
    )

    assert command_completed.is_set()
    assert result.output.text == "observed"
    assert result.lifecycle.terminal_observed is True
    assert result.lifecycle.command_completed is True


@pytest.mark.asyncio
async def test_turn_runtime_preserves_snapshot_recovery_and_usage() -> None:
    async def _collect() -> OpenCodeTurnOutput:
        return OpenCodeTurnOutput(
            text="from snapshot",
            usage={"total_tokens": 7},
            output_source="messages_snapshot",
            terminal_signal="session.status:idle",
        )

    result = await observe_turn_runtime(
        collect_task=asyncio.create_task(_collect()),
        command_task=None,
        raw_events=lambda: [],
    )

    assert result.output.text == "from snapshot"
    assert result.output.usage == {"total_tokens": 7}
    assert result.lifecycle.state is OpenCodeTurnLifecycleState.SNAPSHOT_RECOVERED


@pytest.mark.asyncio
async def test_turn_runtime_collector_normalizes_permission_and_question_policy() -> (
    None
):
    permission_replies: list[tuple[str, str]] = []
    question_rejections: list[str] = []

    async def _events():
        yield SSEEvent(
            event="permission.asked",
            data=(
                '{"sessionID":"session-1","properties":{"id":"perm-1",'
                '"permission":"edit"}}'
            ),
        )
        yield SSEEvent(
            event="question.asked",
            data=(
                '{"sessionID":"session-1","properties":{"id":"q-1","questions":'
                '[{"text":"Continue?","options":[{"label":"Yes"},{"label":"No"}]}]}}'
            ),
        )
        yield SSEEvent(
            event="session.status",
            data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
        )

    output = await collect_opencode_output_from_events(
        _events(),
        session_id="session-1",
        permission_policy=PERMISSION_ALLOW,
        question_policy="reject",
        respond_permission=lambda request_id, reply: _record_permission(
            permission_replies, request_id, reply
        ),
        reject_question=lambda request_id: _record_question_rejection(
            question_rejections, request_id
        ),
    )

    assert output.error is None
    assert permission_replies == [("perm-1", "once")]
    assert question_rejections == ["q-1"]


@pytest.mark.asyncio
async def test_turn_runtime_collector_handles_question_tool_part() -> None:
    question_answers: list[tuple[str, list[list[str]]]] = []

    async def _events():
        yield SSEEvent(
            event="message.part.updated",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "properties": {
                        "part": {
                            "id": "part-q1",
                            "type": "tool",
                            "tool": "question",
                            "state": {
                                "id": "que-q1",
                                "status": "pending",
                                "input": {
                                    "questions": [
                                        {
                                            "question": "Continue?",
                                            "options": [{"label": "Yes"}],
                                        }
                                    ]
                                },
                            },
                        }
                    },
                }
            ),
        )
        yield SSEEvent(
            event="session.status",
            data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
        )

    output = await collect_opencode_output_from_events(
        _events(),
        session_id="session-1",
        question_handler=lambda request_id, props: _answer_question(request_id, props),
        reply_question=lambda request_id, answers: _record_question_answer(
            question_answers, request_id, answers
        ),
    )

    assert output.error is None
    assert question_answers == [("que-q1", [["Yes"]])]


@pytest.mark.asyncio
async def test_collector_fails_stalled_stream_after_relevant_event_when_command_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stream_lifecycle_module,
        "_OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS",
        (0.0,),
    )
    monkeypatch.setattr(
        stream_lifecycle_module,
        "_OPENCODE_STREAM_MAX_STALL_RECONNECT_ATTEMPTS",
        1,
    )

    stream_count = 0

    async def _event_stream():
        nonlocal stream_count
        stream_count += 1
        if stream_count == 1:
            yield SSEEvent(
                event="message.part.updated",
                data=json.dumps(
                    {
                        "sessionID": "session-1",
                        "properties": {
                            "part": {
                                "id": "tool-1",
                                "type": "tool",
                                "tool": "bash",
                                "state": {"status": "running"},
                            }
                        },
                    }
                ),
            )
        while True:
            await asyncio.sleep(3600)
            yield SSEEvent(event="server.heartbeat", data="")

    async def _session_status() -> dict[str, object]:
        return {}

    output = await asyncio.wait_for(
        collect_opencode_output_from_events(
            session_id="session-1",
            event_stream_factory=_event_stream,
            session_fetcher=_session_status,
            turn_activity_fetcher=lambda: True,
            stall_timeout_seconds=0.001,
            first_event_timeout_seconds=1.0,
        ),
        timeout=1.0,
    )

    assert output.error is not None
    assert output.error.startswith("opencode_stream_stalled_timeout")


@pytest.mark.asyncio
async def test_turn_runtime_collector_fails_failed_question_tool_part() -> None:
    async def _events():
        yield SSEEvent(
            event="message.updated",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "info": {"id": "msg-1", "role": "assistant"},
                }
            ),
        )
        yield SSEEvent(
            event="message.part.delta",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "properties": {
                        "part": {
                            "id": "text-1",
                            "messageID": "msg-1",
                            "type": "text",
                        },
                        "delta": {"text": "Let me check first."},
                    },
                }
            ),
        )
        yield SSEEvent(
            event="message.completed",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "info": {
                        "id": "msg-1",
                        "role": "assistant",
                        "finish": "tool-calls",
                    },
                    "parts": [{"type": "text", "text": "Let me check first."}],
                }
            ),
        )
        yield SSEEvent(
            event="message.part.updated",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "properties": {
                        "part": {
                            "id": "part-q1",
                            "type": "tool",
                            "tool": "question",
                            "state": {
                                "status": "error",
                                "input": {
                                    "questions": [{"question": "Continue?"}],
                                },
                                "error": "The user dismissed this question",
                            },
                        }
                    },
                }
            ),
        )
        yield SSEEvent(
            event="session.status",
            data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
        )

    output = await collect_opencode_output_from_events(
        _events(),
        session_id="session-1",
    )

    assert output.text == ""
    assert output.error == (
        "OpenCode question tool failed: The user dismissed this question"
    )


@pytest.mark.asyncio
async def test_turn_runtime_collector_fails_question_tool_part_missing_request_id() -> (
    None
):
    async def _events():
        yield SSEEvent(
            event="message.part.updated",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "properties": {
                        "part": {
                            "id": "prt-q1",
                            "type": "tool",
                            "tool": "question",
                            "state": {
                                "status": "pending",
                                "input": {
                                    "questions": [{"question": "Continue?"}],
                                },
                            },
                        }
                    },
                }
            ),
        )
        yield SSEEvent(
            event="session.status",
            data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
        )

    output = await collect_opencode_output_from_events(
        _events(),
        session_id="session-1",
    )

    assert output.text == ""
    assert output.error == "OpenCode question tool missing request id"


@pytest.mark.asyncio
async def test_turn_runtime_collector_uses_question_event_after_tool_part_without_id() -> (
    None
):
    question_answers: list[tuple[str, list[list[str]]]] = []

    async def _events():
        yield SSEEvent(
            event="message.part.updated",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "properties": {
                        "part": {
                            "id": "prt-q1",
                            "type": "tool",
                            "tool": "question",
                            "state": {
                                "status": "pending",
                                "input": {},
                            },
                        }
                    },
                }
            ),
        )
        yield SSEEvent(
            event="question.asked",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "properties": {
                        "id": "que-q1",
                        "questions": [
                            {
                                "question": "Continue?",
                                "options": [{"label": "Yes"}],
                            }
                        ],
                    },
                }
            ),
        )
        yield SSEEvent(
            event="session.status",
            data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
        )

    output = await collect_opencode_output_from_events(
        _events(),
        session_id="session-1",
        question_handler=lambda request_id, props: _answer_question(request_id, props),
        reply_question=lambda request_id, answers: _record_question_answer(
            question_answers, request_id, answers
        ),
    )

    assert output.error is None
    assert question_answers == [("que-q1", [["Yes"]])]


@pytest.mark.asyncio
async def test_turn_runtime_collector_error_clears_partial_text() -> None:
    async def _events():
        yield SSEEvent(
            event="message.updated",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "info": {"id": "msg-1", "role": "assistant"},
                }
            ),
        )
        yield SSEEvent(
            event="message.part.delta",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "properties": {
                        "part": {
                            "id": "text-1",
                            "messageID": "msg-1",
                            "type": "text",
                        },
                        "delta": {"text": "partial"},
                    },
                }
            ),
        )
        yield SSEEvent(
            event="session.error",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "error": {"message": "boom"},
                }
            ),
        )

    output = await collect_opencode_output_from_events(
        _events(),
        session_id="session-1",
    )

    assert output.text == ""
    assert output.error == "boom"


@pytest.mark.asyncio
async def test_turn_runtime_idle_after_only_tool_call_checkpoint_is_not_final_output() -> (
    None
):
    async def _events():
        yield SSEEvent(
            event="message.updated",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "info": {"id": "msg-1", "role": "assistant"},
                }
            ),
        )
        yield SSEEvent(
            event="message.part.delta",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "properties": {
                        "part": {
                            "id": "text-1",
                            "messageID": "msg-1",
                            "type": "text",
                        },
                        "delta": {"text": "Let me check."},
                    },
                }
            ),
        )
        yield SSEEvent(
            event="message.completed",
            data=json.dumps(
                {
                    "sessionID": "session-1",
                    "info": {
                        "id": "msg-1",
                        "role": "assistant",
                        "finish": "tool-calls",
                    },
                    "parts": [{"type": "text", "text": "Let me check."}],
                }
            ),
        )
        yield SSEEvent(
            event="session.status",
            data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
        )

    output = await collect_opencode_output_from_events(
        _events(),
        session_id="session-1",
    )

    assert output.text == ""
    assert output.error == "OpenCode turn ended without terminal assistant message"


@pytest.mark.asyncio
async def test_turn_runtime_cancels_preconnected_stream_once(tmp_path: Path) -> None:
    client = _HangingStreamClient()
    stream = await pre_connect_event_stream(client, tmp_path, "session-1")

    await cancel_and_wait(stream.stream_task)
    await cancel_and_wait(stream.stream_task)

    assert client.finalized == 1


async def _record(target: list[tuple[str, Any]], kind: str, result: Any) -> None:
    target.append((kind, result))


async def _record_permission(
    target: list[tuple[str, str]], request_id: str, reply: str
) -> None:
    target.append((request_id, reply))


async def _record_question_rejection(target: list[str], request_id: str) -> None:
    target.append(request_id)


async def _answer_question(request_id: str, props: dict[str, Any]) -> list[list[str]]:
    _ = request_id, props
    return [["Yes"]]


async def _record_question_answer(
    target: list[tuple[str, list[list[str]]]],
    request_id: str,
    answers: list[list[str]],
) -> None:
    target.append((request_id, answers))


class _HangingStreamClient:
    def __init__(self) -> None:
        self.finalized = 0

    async def stream_events(
        self,
        *,
        directory: str,
        session_id: str,
        ready_event: asyncio.Event,
    ):
        _ = directory, session_id
        ready_event.set()
        try:
            while True:
                await asyncio.sleep(1)
                yield SSEEvent(event="server.heartbeat", data="")
        finally:
            self.finalized += 1
