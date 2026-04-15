"""Tests for the OutputAssembler contract.

These tests exercise message-role tracking, text dedupe, usage snapshots,
fallback-message selection, and messages_fetcher recovery *without* running
the full stream lifecycle loop.
"""

import httpx
import pytest

from codex_autorunner.agents.opencode.output_assembly import (
    OutputAssembler,
)


@pytest.mark.anyio
async def test_text_delta_accumulation() -> None:
    asm = OutputAssembler(session_id="s1")
    await asm.on_text_delta(
        part_message_id=None,
        delta_text="Hello ",
        part_id=None,
        part_dict=None,
    )
    await asm.on_text_delta(
        part_message_id=None,
        delta_text="world",
        part_id=None,
        part_dict=None,
    )
    asm.flush_pending()
    result = await asm.build_result()
    assert result.text == "Hello world"
    assert result.error is None
    assert result.usage is None


@pytest.mark.anyio
async def test_text_delta_with_part_id_dedupe() -> None:
    asm = OutputAssembler(session_id="s1")
    await asm.on_text_delta(
        part_message_id=None,
        delta_text="Hello ",
        part_id="p1",
        part_dict={"type": "text", "text": "Hello "},
    )
    await asm.on_text_delta(
        part_message_id=None,
        delta_text="world",
        part_id="p1",
        part_dict={"type": "text", "text": "Hello world"},
    )
    asm.flush_pending()
    result = await asm.build_result()
    assert result.text == "Hello world"


@pytest.mark.anyio
async def test_full_text_part_incremental() -> None:
    asm = OutputAssembler(session_id="s1")
    await asm.on_full_text_part(
        part_message_id=None,
        part_dict={"id": "p1", "type": "text", "text": "Hello"},
    )
    await asm.on_full_text_part(
        part_message_id=None,
        part_dict={"id": "p1", "type": "text", "text": "Hello world"},
    )
    asm.flush_pending()
    result = await asm.build_result()
    assert result.text == "Hello world"


@pytest.mark.anyio
async def test_full_text_part_no_id_incremental() -> None:
    asm = OutputAssembler(session_id="s1")
    await asm.on_full_text_part(
        part_message_id=None,
        part_dict={"type": "text", "text": "Hello"},
    )
    await asm.on_full_text_part(
        part_message_id=None,
        part_dict={"type": "text", "text": "Hello world"},
    )
    asm.flush_pending()
    result = await asm.build_result()
    assert result.text == "Hello world"


@pytest.mark.anyio
async def test_role_tracking_filters_user_text() -> None:
    asm = OutputAssembler(session_id="s1")

    msg_id, role = asm.on_register_message_role({"info": {"id": "u1", "role": "user"}})
    asm.on_handle_role_update(msg_id, role)

    await asm.on_text_delta(
        part_message_id="u1",
        delta_text="User prompt",
        part_id="p1",
        part_dict={"type": "text", "text": "User prompt"},
    )

    msg_id2, role2 = asm.on_register_message_role(
        {"info": {"id": "a1", "role": "assistant"}}
    )
    asm.on_handle_role_update(msg_id2, role2)

    await asm.on_text_delta(
        part_message_id="a1",
        delta_text="Assistant reply",
        part_id="p2",
        part_dict={"type": "text", "text": "Assistant reply"},
    )

    result = await asm.build_result()
    assert result.text == "Assistant reply"
    assert "User" not in result.text


@pytest.mark.anyio
async def test_pending_text_flushed_on_role_resolution() -> None:
    asm = OutputAssembler(session_id="s1")

    await asm.on_text_delta(
        part_message_id="m1",
        delta_text="Hello",
        part_id="p1",
        part_dict=None,
    )

    assert asm.has_pending_text

    msg_id, role = asm.on_register_message_role(
        {"info": {"id": "m1", "role": "assistant"}}
    )
    asm.on_handle_role_update(msg_id, role)

    result = await asm.build_result()
    assert result.text == "Hello"


@pytest.mark.anyio
async def test_no_id_pending_flushed_as_assistant() -> None:
    asm = OutputAssembler(session_id="s1")

    await asm.on_text_delta(
        part_message_id=None,
        delta_text="Some text",
        part_id=None,
        part_dict=None,
    )

    assert asm.has_pending_text
    assert not asm.has_text

    asm.flush_pending()

    assert asm.has_text
    result = await asm.build_result()
    assert result.text == "Some text"


@pytest.mark.anyio
async def test_no_id_pending_discarded_after_user_role() -> None:
    asm = OutputAssembler(session_id="s1")

    await asm.on_text_delta(
        part_message_id=None,
        delta_text="Should be discarded",
        part_id=None,
        part_dict=None,
    )

    msg_id, role = asm.on_register_message_role({"info": {"id": "u1", "role": "user"}})
    asm.on_handle_role_update(msg_id, role)

    asm.flush_pending()
    result = await asm.build_result()
    assert result.text == ""


@pytest.mark.anyio
async def test_message_completed_fallback() -> None:
    asm = OutputAssembler(session_id="s1", prompt="What?")

    message_role = asm.on_message_completed(
        {
            "info": {"id": "m1", "role": "assistant"},
            "parts": [{"type": "text", "text": "Answer"}],
        },
        is_primary_session=True,
        event_session_id="s1",
    )

    assert message_role == "assistant"
    result = await asm.build_result()
    assert result.text == "Answer"


@pytest.mark.anyio
async def test_message_completed_roleless_suppresses_echo() -> None:
    asm = OutputAssembler(session_id="s1", prompt="What is the answer?")

    message_role = asm.on_message_completed(
        {
            "info": {"id": "m1"},
            "parts": [{"type": "text", "text": "What is the answer?"}],
        },
        is_primary_session=True,
        event_session_id="s1",
    )

    assert message_role is None
    result = await asm.build_result()
    assert result.text == ""


@pytest.mark.anyio
async def test_message_completed_roleless_non_echo_preserved() -> None:
    asm = OutputAssembler(session_id="s1", prompt="What is the answer?")

    message_role = asm.on_message_completed(
        {"info": {"id": "m1"}, "parts": [{"type": "text", "text": "Final answer"}]},
        is_primary_session=True,
        event_session_id="s1",
    )

    assert message_role is None
    result = await asm.build_result()
    assert result.text == "Final answer"


@pytest.mark.anyio
async def test_message_completed_user_role_ignored() -> None:
    asm = OutputAssembler(session_id="s1")

    message_role = asm.on_message_completed(
        {
            "info": {"id": "u1", "role": "user"},
            "parts": [{"type": "text", "text": "prompt"}],
        },
        is_primary_session=True,
        event_session_id="s1",
    )

    assert message_role == "user"
    result = await asm.build_result()
    assert result.text == ""


@pytest.mark.anyio
async def test_message_completed_non_primary_session_ignored() -> None:
    asm = OutputAssembler(session_id="s1")

    message_role = asm.on_message_completed(
        {
            "info": {"id": "m1", "role": "assistant"},
            "parts": [{"type": "text", "text": "child"}],
        },
        is_primary_session=False,
        event_session_id="child-1",
    )

    assert message_role is None
    result = await asm.build_result()
    assert result.text == ""


@pytest.mark.anyio
async def test_primary_assistant_completion_records_text() -> None:
    asm = OutputAssembler(session_id="s1")

    asm.on_primary_assistant_completion(
        {
            "info": {"id": "m1", "role": "assistant"},
            "parts": [{"type": "text", "text": "completed"}],
        },
        message_role="assistant",
    )

    await asm.on_text_delta(
        part_message_id=None,
        delta_text="stream text",
        part_id=None,
        part_dict=None,
    )

    result = await asm.build_result()
    assert result.text == "completed"


@pytest.mark.anyio
async def test_primary_assistant_completion_commentary_ignored() -> None:
    asm = OutputAssembler(session_id="s1")

    asm.on_primary_assistant_completion(
        {
            "info": {"id": "m1", "role": "assistant"},
            "phase": "commentary",
            "parts": [{"type": "text", "text": "commentary"}],
        },
        message_role="assistant",
    )

    await asm.on_text_delta(
        part_message_id=None,
        delta_text="real answer",
        part_id=None,
        part_dict=None,
    )
    asm.flush_pending()

    result = await asm.build_result()
    assert result.text == "real answer"


@pytest.mark.anyio
async def test_primary_assistant_completion_non_assistant_role_ignored() -> None:
    asm = OutputAssembler(session_id="s1")

    asm.on_primary_assistant_completion(
        {
            "info": {"id": "u1", "role": "user"},
            "parts": [{"type": "text", "text": "prompt"}],
        },
        message_role="user",
    )

    await asm.on_text_delta(
        part_message_id=None,
        delta_text="stream text",
        part_id=None,
        part_dict=None,
    )
    asm.flush_pending()

    result = await asm.build_result()
    assert result.text == "stream text"


@pytest.mark.anyio
async def test_usage_deduplication() -> None:
    seen: list[dict] = []

    async def _handler(part_type, part, delta_text):
        if part_type == "usage":
            seen.append(part)

    asm = OutputAssembler(session_id="s1", part_handler=_handler)

    await asm.emit_usage_update(
        {
            "sessionID": "s1",
            "properties": {"info": {"tokens": {"input": 10, "output": 5}}},
        },
        is_primary_session=True,
    )
    await asm.emit_usage_update(
        {
            "sessionID": "s1",
            "properties": {"info": {"tokens": {"input": 10, "output": 5}}},
        },
        is_primary_session=True,
    )

    assert len(seen) == 1
    result = await asm.build_result()
    assert result.usage is not None
    assert result.usage["totalTokens"] == 15


@pytest.mark.anyio
async def test_usage_skipped_for_non_primary() -> None:
    seen: list[dict] = []

    async def _handler(part_type, part, delta_text):
        if part_type == "usage":
            seen.append(part)

    asm = OutputAssembler(session_id="s1", part_handler=_handler)

    await asm.emit_usage_update(
        {
            "sessionID": "child-1",
            "properties": {"info": {"tokens": {"input": 10, "output": 5}}},
        },
        is_primary_session=False,
    )

    assert len(seen) == 0
    result = await asm.build_result()
    assert result.usage is None


@pytest.mark.anyio
async def test_usage_detail_change_emits_update() -> None:
    seen: list[dict] = []

    async def _handler(part_type, part, delta_text):
        if part_type == "usage":
            seen.append(part)

    asm = OutputAssembler(session_id="s1", part_handler=_handler)

    await asm.emit_usage_update(
        {
            "sessionID": "s1",
            "properties": {
                "part": {"type": "step-finish", "tokens": {"input": 12, "output": 3}}
            },
        },
        is_primary_session=True,
    )
    await asm.emit_usage_update(
        {
            "sessionID": "s1",
            "properties": {
                "info": {"tokens": {"input": 10, "output": 3, "cache": {"read": 2}}}
            },
        },
        is_primary_session=True,
    )

    assert len(seen) == 2
    assert seen[0]["totalTokens"] == 15
    assert "cachedInputTokens" not in seen[0]
    assert seen[1]["totalTokens"] == 15
    assert seen[1]["cachedInputTokens"] == 2


@pytest.mark.anyio
async def test_usage_backfills_context_from_providers() -> None:
    seen: list[dict] = []

    async def _handler(part_type, part, delta_text):
        if part_type == "usage":
            seen.append(part)

    async def _fetch_providers():
        return {
            "providers": [
                {"id": "prov", "models": {"model": {"limit": {"context": 1024}}}}
            ]
        }

    asm = OutputAssembler(
        session_id="s1",
        model_payload={"providerID": "prov", "modelID": "model"},
        provider_fetcher=_fetch_providers,
        part_handler=_handler,
    )

    await asm.emit_usage_update(
        {"sessionID": "s1", "info": {"tokens": {"input": 12, "output": 3}}},
        is_primary_session=True,
    )

    assert len(seen) == 1
    assert seen[0]["modelContextWindow"] == 1024


@pytest.mark.anyio
async def test_messages_fetcher_recovery() -> None:
    async def _fetch_messages():
        return [
            {
                "info": {"id": "u1", "role": "user"},
                "parts": [{"type": "text", "text": "prompt"}],
            },
            {
                "info": {"id": "a1", "role": "assistant"},
                "parts": [{"type": "text", "text": "recovered answer"}],
            },
        ]

    asm = OutputAssembler(
        session_id="s1",
        prompt="prompt",
        messages_fetcher=_fetch_messages,
    )

    result = await asm.build_result()
    assert result.text == "recovered answer"


@pytest.mark.anyio
async def test_messages_fetcher_exception_does_not_crash() -> None:
    async def _fetch_error():
        raise httpx.HTTPStatusError(
            "server error",
            request=httpx.Request("GET", "http://x"),
            response=httpx.Response(500),
        )

    asm = OutputAssembler(
        session_id="s1",
        messages_fetcher=_fetch_error,
    )

    result = await asm.build_result()
    assert result.text == ""
    assert result.error is None


@pytest.mark.anyio
async def test_messages_fetcher_skipped_when_text_present() -> None:
    fetch_calls = 0

    async def _fetch_messages():
        nonlocal fetch_calls
        fetch_calls += 1
        return []

    asm = OutputAssembler(
        session_id="s1",
        messages_fetcher=_fetch_messages,
    )

    await asm.on_text_delta(
        part_message_id=None,
        delta_text="Already have text",
        part_id=None,
        part_dict=None,
    )
    asm.flush_pending()

    result = await asm.build_result()
    assert result.text == "Already have text"
    assert fetch_calls == 0


@pytest.mark.anyio
async def test_error_tracking() -> None:
    asm = OutputAssembler(session_id="s1")
    assert asm.error is None
    asm.error = "something broke"
    assert asm.error == "something broke"
    result = await asm.build_result()
    assert result.error == "something broke"


@pytest.mark.anyio
async def test_message_completed_error_captured() -> None:
    asm = OutputAssembler(session_id="s1")

    asm.on_message_completed(
        {"info": {"id": "m1", "role": "assistant", "error": "bad auth"}},
        is_primary_session=True,
        event_session_id="s1",
    )

    assert asm.error == "bad auth"


@pytest.mark.anyio
async def test_part_type_memory() -> None:
    asm = OutputAssembler(session_id="s1")

    asm.remember_part_type("r1", "reasoning")
    assert asm.lookup_part_type("r1") == "reasoning"
    assert asm.lookup_part_type("unknown") is None


@pytest.mark.anyio
async def test_last_completed_assistant_overrides_stream_text() -> None:
    asm = OutputAssembler(session_id="s1")

    await asm.on_text_delta(
        part_message_id=None,
        delta_text="streaming text",
        part_id=None,
        part_dict=None,
    )

    asm.on_primary_assistant_completion(
        {
            "info": {"id": "m1", "role": "assistant"},
            "parts": [{"type": "text", "text": "completed text"}],
        },
        message_role="assistant",
    )

    result = await asm.build_result()
    assert result.text == "completed text"


@pytest.mark.anyio
async def test_last_completed_assistant_falls_back_to_stream_text() -> None:
    asm = OutputAssembler(session_id="s1")

    await asm.on_text_delta(
        part_message_id=None,
        delta_text="streaming text",
        part_id=None,
        part_dict=None,
    )
    asm.flush_pending()

    asm.on_primary_assistant_completion(
        {"info": {"id": "m1", "role": "assistant"}, "parts": []},
        message_role="assistant",
    )

    result = await asm.build_result()
    assert result.text == "streaming text"


@pytest.mark.anyio
async def test_multi_message_turn_prefers_last_completed() -> None:
    asm = OutputAssembler(session_id="s1")

    msg_id, role = asm.on_register_message_role(
        {"info": {"id": "m1", "role": "assistant"}}
    )
    asm.on_handle_role_update(msg_id, role)

    await asm.on_text_delta(
        part_message_id="m1",
        delta_text="First message.",
        part_id="p1",
        part_dict={"type": "text", "text": "First message."},
    )

    asm.on_primary_assistant_completion(
        {
            "info": {"id": "m1", "role": "assistant"},
            "parts": [{"type": "text", "text": "First message."}],
        },
        message_role="assistant",
    )

    msg_id2, role2 = asm.on_register_message_role(
        {"info": {"id": "m2", "role": "assistant"}}
    )
    asm.on_handle_role_update(msg_id2, role2)

    await asm.on_text_delta(
        part_message_id="m2",
        delta_text="Second message.",
        part_id="p2",
        part_dict={"type": "text", "text": "Second message."},
    )

    asm.on_primary_assistant_completion(
        {
            "info": {"id": "m2", "role": "assistant"},
            "parts": [{"type": "text", "text": "Second message."}],
        },
        message_role="assistant",
    )

    result = await asm.build_result()
    assert result.text == "Second message."


@pytest.mark.anyio
async def test_flush_pending_respects_roles_seen() -> None:
    asm = OutputAssembler(session_id="s1")

    await asm.on_text_delta(
        part_message_id=None,
        delta_text="no-role text",
        part_id=None,
        part_dict=None,
    )

    asm.flush_pending()
    result = await asm.build_result()
    assert result.text == "no-role text"


@pytest.mark.anyio
async def test_has_text_and_has_pending() -> None:
    asm = OutputAssembler(session_id="s1")
    assert not asm.has_text
    assert not asm.has_pending_text

    await asm.on_text_delta(
        part_message_id="m1",
        delta_text="pending",
        part_id=None,
        part_dict=None,
    )
    assert not asm.has_text
    assert asm.has_pending_text

    msg_id, role = asm.on_register_message_role(
        {"info": {"id": "m1", "role": "assistant"}}
    )
    asm.on_handle_role_update(msg_id, role)
    assert asm.has_text
