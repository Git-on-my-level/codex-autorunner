import asyncio
import sys
import time
from pathlib import Path

import pytest

from codex_autorunner.integrations.app_server import client as app_server_client
from codex_autorunner.integrations.app_server.client import (
    CodexAppServerClient,
    CodexAppServerDisconnected,
    _extract_agent_message_text,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "app_server_fixture.py"


def fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FIXTURE_PATH), "--scenario", scenario]


def test_turn_stall_max_recovery_attempts_defaults_and_disable_override() -> None:
    client_default = CodexAppServerClient(fixture_command("basic"))
    assert client_default._turn_stall_max_recovery_attempts == 8

    client_disabled = CodexAppServerClient(
        fixture_command("basic"),
        turn_stall_max_recovery_attempts=None,
    )
    assert client_disabled._turn_stall_max_recovery_attempts is None


@pytest.mark.anyio
async def test_handshake_and_status(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        status = await client.request("fixture/status")
        assert status["initialized"] is True
        assert status["initializedNotification"] is True
    finally:
        await client.close()


@pytest.mark.anyio
async def test_request_response_out_of_order(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        slow_task = asyncio.create_task(
            client.request("fixture/slow", {"value": "slow"})
        )
        fast_task = asyncio.create_task(
            client.request("fixture/fast", {"value": "fast"})
        )
        assert await fast_task == {"value": "fast"}
        assert await slow_task == {"value": "slow"}
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_completion_and_agent_message(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        result = await handle.wait()
        assert result.status == "completed"
        assert result.final_message == "fixture reply"
        assert result.agent_messages == ["fixture reply"]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_error_notification(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("turn_error_no_agent"), cwd=tmp_path)
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        result = await handle.wait()
        assert result.status == "failed"
        assert result.final_message == ""
        assert result.agent_messages == []
        assert result.errors == ["Auth required"]
    finally:
        await client.close()


def test_extract_agent_message_text_supports_content_list() -> None:
    item = {
        "type": "agentMessage",
        "content": [
            {"type": "output_text", "text": "hello"},
            {"type": "output_text", "text": " world"},
        ],
    }
    assert _extract_agent_message_text(item) == "hello world"


@pytest.mark.anyio
async def test_review_message_dedupes_review_text(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("review_duplicate"), cwd=tmp_path)
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.review_start(thread["id"], target={"type": "custom"})
        result = await handle.wait()
        assert result.status == "completed"
        assert result.final_message == "fixture reply"
        assert result.agent_messages == ["fixture reply"]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_result_defaults_to_last_agent_message(tmp_path: Path) -> None:
    client = CodexAppServerClient(
        fixture_command("multi_agent_messages"),
        cwd=tmp_path,
    )
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        result = await handle.wait()
        assert result.status == "completed"
        assert result.agent_messages == ["draft reply", "final reply"]
        assert result.commentary_messages == []
        assert result.final_message == "final reply"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_result_prefers_phase_marked_final_answer(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        commentary_item = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-1",
            "item": {
                "type": "agentMessage",
                "text": "draft reply",
                "phase": "commentary",
            },
        }
        final_item = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-2",
            "item": {
                "type": "agentMessage",
                "text": "final reply",
                "phase": "final_answer",
            },
        }
        completed = {"turnId": "turn-1", "threadId": "thread-1", "status": "completed"}

        await client._handle_notification_item_completed(
            {"method": "item/completed", "params": commentary_item},
            commentary_item,
        )
        await client._handle_notification_item_completed(
            {"method": "item/completed", "params": final_item},
            final_item,
        )
        await client._handle_notification_turn_completed(
            {"method": "turn/completed", "params": completed},
            completed,
        )

        result = await asyncio.wait_for(asyncio.shield(state.future), timeout=0.5)
        assert result.status == "completed"
        assert result.agent_messages == ["draft reply", "final reply"]
        assert result.commentary_messages == ["draft reply"]
        assert result.final_message == "final reply"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_result_uses_pending_delta_after_completed_message(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        draft_item = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-1",
            "item": {"type": "agentMessage", "text": "draft reply"},
        }
        final_delta = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-2",
            "delta": "real final reply",
        }
        completed = {"turnId": "turn-1", "threadId": "thread-1", "status": "completed"}

        await client._handle_notification_item_completed(
            {"method": "item/completed", "params": draft_item},
            draft_item,
        )
        await client._handle_notification_agent_message_delta(
            {"method": "item/agentMessage/delta", "params": final_delta},
            final_delta,
        )
        await client._handle_notification_turn_completed(
            {"method": "turn/completed", "params": completed},
            completed,
        )

        result = await asyncio.wait_for(asyncio.shield(state.future), timeout=0.5)
        assert result.status == "completed"
        assert result.agent_messages == ["draft reply", "real final reply"]
        assert result.final_message == "real final reply"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_item_completed_with_text_clears_matching_delta(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        partial_delta = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-1",
            "delta": "partial",
        }
        completed_item = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-1",
            "item": {"type": "agentMessage", "text": "final reply"},
        }
        completed = {"turnId": "turn-1", "threadId": "thread-1", "status": "completed"}

        await client._handle_notification_agent_message_delta(
            {"method": "item/agentMessage/delta", "params": partial_delta},
            partial_delta,
        )
        await client._handle_notification_item_completed(
            {"method": "item/completed", "params": completed_item},
            completed_item,
        )
        await client._handle_notification_turn_completed(
            {"method": "turn/completed", "params": completed},
            completed,
        )

        result = await asyncio.wait_for(asyncio.shield(state.future), timeout=0.5)
        assert result.status == "completed"
        assert result.agent_messages == ["final reply"]
        assert result.final_message == "final reply"
        assert state.agent_message_deltas == {}
    finally:
        await client.close()


@pytest.mark.anyio
async def test_pending_delta_does_not_replace_completed_message(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        completed_item = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-1",
            "item": {"type": "agentMessage", "text": "hello"},
        }
        pending_delta = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-2",
            "delta": "hello world",
        }
        completed = {"turnId": "turn-1", "threadId": "thread-1", "status": "completed"}

        await client._handle_notification_item_completed(
            {"method": "item/completed", "params": completed_item},
            completed_item,
        )
        await client._handle_notification_agent_message_delta(
            {"method": "item/agentMessage/delta", "params": pending_delta},
            pending_delta,
        )
        await client._handle_notification_turn_completed(
            {"method": "turn/completed", "params": completed},
            completed,
        )

        result = await asyncio.wait_for(asyncio.shield(state.future), timeout=0.5)
        assert result.status == "completed"
        assert result.agent_messages == ["hello", "hello world"]
        assert result.final_message == "hello world"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_pending_delta_matching_last_message_is_deduped(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        completed_item = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-1",
            "item": {"type": "agentMessage", "text": "final reply"},
        }
        matching_delta = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-2",
            "delta": "final reply",
        }
        completed = {"turnId": "turn-1", "threadId": "thread-1", "status": "completed"}

        await client._handle_notification_item_completed(
            {"method": "item/completed", "params": completed_item},
            completed_item,
        )
        await client._handle_notification_agent_message_delta(
            {"method": "item/agentMessage/delta", "params": matching_delta},
            matching_delta,
        )
        await client._handle_notification_turn_completed(
            {"method": "turn/completed", "params": completed},
            completed,
        )

        result = await asyncio.wait_for(asyncio.shield(state.future), timeout=0.5)
        assert result.status == "completed"
        assert result.agent_messages == ["final reply"]
        assert result.final_message == "final reply"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_item_completed_without_item_id_prunes_matching_stale_delta(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        partial_delta = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-1",
            "delta": "final",
        }
        completed_item_without_id = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "item": {"type": "agentMessage", "text": "final reply"},
        }
        completed = {"turnId": "turn-1", "threadId": "thread-1", "status": "completed"}

        await client._handle_notification_agent_message_delta(
            {"method": "item/agentMessage/delta", "params": partial_delta},
            partial_delta,
        )
        await client._handle_notification_item_completed(
            {"method": "item/completed", "params": completed_item_without_id},
            completed_item_without_id,
        )
        await client._handle_notification_turn_completed(
            {"method": "turn/completed", "params": completed},
            completed,
        )

        result = await asyncio.wait_for(asyncio.shield(state.future), timeout=0.5)
        assert result.status == "completed"
        assert result.agent_messages == ["final reply"]
        assert result.final_message == "final reply"
        assert state.agent_message_deltas == {}
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_completed_settles_before_returning_final_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_server_client, "_TURN_COMPLETION_SETTLE_SECONDS", 0.02)
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        completed_item = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-1",
            "item": {"type": "agentMessage", "text": "final reply"},
        }
        completed = {"turnId": "turn-1", "threadId": "thread-1", "status": "completed"}

        await client._handle_notification_item_completed(
            {"method": "item/completed", "params": completed_item},
            completed_item,
        )
        await client._handle_notification_turn_completed(
            {"method": "turn/completed", "params": completed},
            completed,
        )

        assert not state.future.done()
        result = await asyncio.wait_for(asyncio.shield(state.future), timeout=0.5)
        assert result.status == "completed"
        assert result.final_message == "final reply"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_late_item_completed_within_settle_updates_final_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_server_client, "_TURN_COMPLETION_SETTLE_SECONDS", 0.05)
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        intermediate_item = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-1",
            "item": {"type": "agentMessage", "text": "intermediate status"},
        }
        late_final_item = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-2",
            "item": {"type": "agentMessage", "text": "true final answer"},
        }
        completed = {"turnId": "turn-1", "threadId": "thread-1", "status": "completed"}

        await client._handle_notification_item_completed(
            {"method": "item/completed", "params": intermediate_item},
            intermediate_item,
        )
        await client._handle_notification_turn_completed(
            {"method": "turn/completed", "params": completed},
            completed,
        )
        await asyncio.sleep(0.01)
        await client._handle_notification_item_completed(
            {"method": "item/completed", "params": late_final_item},
            late_final_item,
        )

        result = await asyncio.wait_for(asyncio.shield(state.future), timeout=0.5)
        assert result.status == "completed"
        assert result.final_message == "true final answer"
        assert result.agent_messages == ["intermediate status", "true final answer"]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_merging_pending_completed_turn_preserves_settle_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_server_client, "_TURN_COMPLETION_SETTLE_SECONDS", 0.02)
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        completed_without_thread = {"turnId": "turn-1", "status": "completed"}
        await client._handle_notification_turn_completed(
            {"method": "turn/completed", "params": completed_without_thread},
            completed_without_thread,
        )

        keyed_item = {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-1",
            "item": {"type": "agentMessage", "text": "thread-scoped final"},
        }
        await client._handle_notification_item_completed(
            {"method": "item/completed", "params": keyed_item},
            keyed_item,
        )

        assert "turn-1" in client._pending_turns
        assert ("thread-1", "turn-1") in client._turns

        state = client._register_turn_state("turn-1", "thread-1")
        assert state is client._turns[("thread-1", "turn-1")]
        assert "turn-1" not in client._pending_turns
        assert state.turn_completed_seen is True

        result = await asyncio.wait_for(asyncio.shield(state.future), timeout=0.5)
        assert result.status == "completed"
        assert result.final_message == "thread-scoped final"
        assert result.agent_messages == ["thread-scoped final"]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_result_can_include_all_agent_messages(tmp_path: Path) -> None:
    client = CodexAppServerClient(
        fixture_command("multi_agent_messages"),
        cwd=tmp_path,
        output_policy="all_agent_messages",
    )
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        result = await handle.wait()
        assert result.status == "completed"
        assert result.agent_messages == ["draft reply", "final reply"]
        assert result.final_message == "draft reply\n\nfinal reply"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_result_accepts_review_exit_as_final_candidate(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("review_exit_only"),
        cwd=tmp_path,
    )
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        result = await handle.wait()
        assert result.status == "completed"
        assert result.agent_messages == ["review verdict"]
        assert result.final_message == "review verdict"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_thread_list_includes_params(tmp_path: Path) -> None:
    client = CodexAppServerClient(
        fixture_command("thread_list_requires_params"), cwd=tmp_path
    )
    try:
        threads = await client.thread_list()
        assert isinstance(threads, list)
        assert threads
    finally:
        await client.close()


@pytest.mark.anyio
async def test_thread_list_normalizes_data_shape(tmp_path: Path) -> None:
    client = CodexAppServerClient(
        fixture_command("thread_list_data_shape"), cwd=tmp_path
    )
    try:
        threads = await client.thread_list()
        assert isinstance(threads, dict)
        assert isinstance(threads.get("threads"), list)
        assert threads["threads"]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_start_normalizes_sandbox_policy(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("sandbox_policy_check"), cwd=tmp_path)
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(
            thread["id"], "hi", sandbox_policy="danger-full-access"
        )
        result = await handle.wait()
        assert result.status == "completed"
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["thread_id_key", "thread_id_snake"])
async def test_thread_start_accepts_alt_thread_id_keys(
    tmp_path: Path, scenario: str
) -> None:
    client = CodexAppServerClient(fixture_command(scenario), cwd=tmp_path)
    try:
        thread = await client.thread_start(str(tmp_path))
        assert isinstance(thread.get("id"), str)
    finally:
        await client.close()


@pytest.mark.anyio
async def test_approval_flow(tmp_path: Path) -> None:
    approvals: list[dict] = []

    async def approve(request: dict) -> str:
        approvals.append(request)
        return "accept"

    client = CodexAppServerClient(
        fixture_command("approval"),
        cwd=tmp_path,
        approval_handler=approve,
    )
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        result = await handle.wait()
        assert approvals
        assert result.status == "completed"
        assert any(
            event.get("method") == "turn/completed"
            and event.get("params", {}).get("approvalDecision") == "accept"
            for event in result.raw_events
        )
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_interrupt(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("interrupt"), cwd=tmp_path)
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        await client.turn_interrupt(handle.turn_id, thread_id=handle.thread_id)
        result = await handle.wait()
        assert result.status == "interrupted"
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.slow
async def test_turn_completed_via_resume_when_completion_missing(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("missing_turn_completed"),
        cwd=tmp_path,
        turn_stall_timeout_seconds=0.5,
        turn_stall_poll_interval_seconds=0.1,
        turn_stall_recovery_min_interval_seconds=0.0,
    )
    try:
        thread = await client.thread_start(str(tmp_path))
        handle = await client.turn_start(thread["id"], "hi")
        result = await handle.wait(timeout=5)
        assert result.status == "completed"
        assert result.final_message == "recovered reply"
        assert result.agent_messages == ["recovered reply"]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_wait_for_turn_stall_recovery_preserves_agent_message_phase(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        turn_stall_timeout_seconds=0.01,
        turn_stall_poll_interval_seconds=0.02,
        turn_stall_recovery_min_interval_seconds=0.0,
    )
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        state.last_event_at -= 1.0

        async def _resume(thread_id: str, **kwargs: object) -> dict[str, object]:
            _ = kwargs
            return {
                "thread": {
                    "id": thread_id,
                    "turns": [
                        {
                            "id": "turn-1",
                            "status": "completed",
                            "items": [
                                {
                                    "type": "agentMessage",
                                    "text": "draft reply",
                                    "phase": "commentary",
                                },
                                {
                                    "type": "agentMessage",
                                    "text": "final reply",
                                    "phase": "final_answer",
                                },
                            ],
                        }
                    ],
                }
            }

        client.thread_resume = _resume  # type: ignore[method-assign]

        result = await client.wait_for_turn("turn-1", thread_id="thread-1", timeout=1.0)
        assert result.status == "completed"
        assert result.agent_messages == ["draft reply", "final reply"]
        assert result.commentary_messages == ["draft reply"]
        assert result.final_message == "final reply"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_wait_for_turn_stall_recovery_clears_stale_final_answer_state(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        turn_stall_timeout_seconds=0.01,
        turn_stall_poll_interval_seconds=0.02,
        turn_stall_recovery_min_interval_seconds=0.0,
    )
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        state.agent_messages = ["stale commentary", "stale final"]
        state.commentary_messages = ["stale commentary"]
        state.final_answer_messages = ["stale final"]
        state.last_event_at -= 1.0

        async def _resume(thread_id: str, **kwargs: object) -> dict[str, object]:
            _ = kwargs
            return {
                "thread": {
                    "id": thread_id,
                    "turns": [
                        {
                            "id": "turn-1",
                            "status": "completed",
                            "items": [
                                {
                                    "type": "agentMessage",
                                    "text": "recovered final reply",
                                }
                            ],
                        }
                    ],
                }
            }

        client.thread_resume = _resume  # type: ignore[method-assign]

        result = await client.wait_for_turn("turn-1", thread_id="thread-1", timeout=1.0)
        assert result.status == "completed"
        assert result.agent_messages == ["recovered final reply"]
        assert result.commentary_messages == []
        assert result.final_message == "recovered final reply"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_wait_for_turn_times_out_when_resume_stays_non_terminal(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        turn_stall_timeout_seconds=0.01,
        turn_stall_poll_interval_seconds=0.05,
        turn_stall_recovery_min_interval_seconds=0.0,
    )
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        state.last_event_at -= 1.0
        resume_calls = 0

        async def _resume(thread_id: str, **kwargs: object) -> dict[str, object]:
            nonlocal resume_calls
            _ = kwargs
            resume_calls += 1
            return {
                "thread": {
                    "id": thread_id,
                    "turns": [{"id": "turn-1", "status": "running"}],
                }
            }

        client.thread_resume = _resume  # type: ignore[method-assign]

        with pytest.raises(asyncio.TimeoutError):
            await client.wait_for_turn("turn-1", thread_id="thread-1", timeout=0.2)

        assert resume_calls >= 1
        assert state.status == "running"
        assert not state.future.done()
    finally:
        await client.close()


@pytest.mark.anyio
async def test_wait_for_turn_fails_when_recovery_attempts_exhausted(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        turn_stall_timeout_seconds=0.01,
        turn_stall_poll_interval_seconds=0.02,
        turn_stall_recovery_min_interval_seconds=0.0,
        turn_stall_max_recovery_attempts=2,
    )
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        state.last_event_at -= 1.0
        resume_calls = 0

        async def _resume(thread_id: str, **kwargs: object) -> dict[str, object]:
            nonlocal resume_calls
            _ = kwargs
            resume_calls += 1
            return {
                "thread": {
                    "id": thread_id,
                    "turns": [{"id": "turn-1", "status": "running"}],
                }
            }

        client.thread_resume = _resume  # type: ignore[method-assign]

        result = await client.wait_for_turn("turn-1", thread_id="thread-1", timeout=1.0)
        assert result.status == "failed"
        assert any("recovery exhausted" in error for error in result.errors)
        assert resume_calls == 2
        assert state.future.done()
    finally:
        await client.close()


@pytest.mark.anyio
async def test_wait_for_turn_reconciles_completion_gap_without_idle_stall(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        turn_stall_timeout_seconds=10.0,
        turn_stall_poll_interval_seconds=0.02,
        turn_stall_recovery_min_interval_seconds=0.0,
        turn_completion_gap_timeout_seconds=0.01,
    )
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        state.status = "running"
        state.item_completed_count = 3
        state.completion_gap_started_at = time.monotonic() - 1.0
        resume_calls = 0

        async def _resume(thread_id: str, **kwargs: object) -> dict[str, object]:
            nonlocal resume_calls
            _ = kwargs
            resume_calls += 1
            return {
                "thread": {
                    "id": thread_id,
                    "turns": [
                        {
                            "id": "turn-1",
                            "status": "completed",
                            "items": [
                                {
                                    "type": "agentMessage",
                                    "text": "recovered reply",
                                }
                            ],
                        }
                    ],
                }
            }

        client.thread_resume = _resume  # type: ignore[method-assign]

        result = await client.wait_for_turn("turn-1", thread_id="thread-1", timeout=1.0)
        assert result.status == "completed"
        assert result.final_message == "recovered reply"
        assert resume_calls == 1
    finally:
        await client.close()


@pytest.mark.anyio
async def test_completion_gap_recovery_emits_synthetic_turn_completed_notification(
    tmp_path: Path,
) -> None:
    notifications: list[dict[str, object]] = []
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def on_notification(message: dict[str, object]) -> None:
        notifications.append(message)
        handler_started.set()
        await release_handler.wait()

    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        turn_stall_timeout_seconds=10.0,
        turn_stall_poll_interval_seconds=0.02,
        turn_stall_recovery_min_interval_seconds=0.0,
        turn_completion_gap_timeout_seconds=0.01,
        notification_handler=on_notification,
    )
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        state.status = "running"
        state.item_completed_count = 3
        state.completion_gap_started_at = time.monotonic() - 1.0

        async def _resume(thread_id: str, **kwargs: object) -> dict[str, object]:
            _ = kwargs
            return {
                "thread": {
                    "id": thread_id,
                    "turns": [
                        {
                            "id": "turn-1",
                            "status": "completed",
                            "items": [
                                {
                                    "type": "agentMessage",
                                    "text": "recovered reply",
                                }
                            ],
                        }
                    ],
                }
            }

        client.thread_resume = _resume  # type: ignore[method-assign]

        result = await asyncio.wait_for(
            client.wait_for_turn("turn-1", thread_id="thread-1", timeout=1.0),
            timeout=0.2,
        )
        await asyncio.wait_for(handler_started.wait(), timeout=0.5)

        assert result.status == "completed"
        assert state.turn_completed_seen is True
        assert any(
            event.get("method") == "turn/completed"
            and event.get("params", {}).get("turnId") == "turn-1"
            and event.get("params", {}).get("threadId") == "thread-1"
            and event.get("params", {}).get("status") == "completed"
            and event.get("params", {}).get("synthetic") is True
            and event.get("params", {}).get("recoveredVia") == "thread/resume"
            for event in notifications
        )
        assert any(
            event.get("method") == "turn/completed"
            and event.get("params", {}).get("synthetic") is True
            for event in result.raw_events
        )
    finally:
        release_handler.set()
        await client.close()


@pytest.mark.anyio
async def test_wait_for_turn_fails_when_completion_gap_recovery_exhausted(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        turn_stall_timeout_seconds=10.0,
        turn_stall_poll_interval_seconds=0.02,
        turn_stall_recovery_min_interval_seconds=0.0,
        turn_stall_max_recovery_attempts=2,
        turn_completion_gap_timeout_seconds=0.01,
    )
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        state.status = "running"
        state.item_completed_count = 1
        state.completion_gap_started_at = time.monotonic() - 1.0
        resume_calls = 0

        async def _resume(thread_id: str, **kwargs: object) -> dict[str, object]:
            nonlocal resume_calls
            _ = kwargs
            resume_calls += 1
            return {
                "thread": {
                    "id": thread_id,
                    "turns": [{"id": "turn-1", "status": "running"}],
                }
            }

        client.thread_resume = _resume  # type: ignore[method-assign]

        result = await client.wait_for_turn("turn-1", thread_id="thread-1", timeout=1.0)
        assert result.status == "failed"
        assert any(
            "completion-gap recovery exhausted" in error for error in result.errors
        )
        assert resume_calls == 2
        assert state.future.done()
    finally:
        await client.close()


@pytest.mark.anyio
async def test_live_progress_refreshes_completion_gap_window(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        turn_stall_timeout_seconds=10.0,
        turn_stall_poll_interval_seconds=0.02,
        turn_stall_recovery_min_interval_seconds=0.0,
        turn_completion_gap_timeout_seconds=0.05,
    )
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        state.status = "running"
        state.item_completed_count = 3
        state.completion_gap_started_at = time.monotonic() - 1.0
        stale_started_at = state.completion_gap_started_at
        resume_calls = 0

        async def _resume(thread_id: str, **kwargs: object) -> dict[str, object]:
            nonlocal resume_calls
            _ = thread_id, kwargs
            resume_calls += 1
            return {}

        client.thread_resume = _resume  # type: ignore[method-assign]

        client._mark_notification_event(state=state, method="item/agentMessage/delta")
        assert state.completion_gap_started_at is not None
        assert state.completion_gap_started_at > stale_started_at

        await client._maybe_reconcile_turn_completion_gap(
            state,
            turn_id="turn-1",
            thread_id="thread-1",
        )

        assert resume_calls == 0
    finally:
        await client.close()


@pytest.mark.anyio
async def test_active_item_started_blocks_completion_gap_recovery(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        turn_stall_timeout_seconds=10.0,
        turn_stall_poll_interval_seconds=0.02,
        turn_stall_recovery_min_interval_seconds=0.0,
        turn_completion_gap_timeout_seconds=0.05,
    )
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        state.status = "running"
        state.item_completed_count = 3
        state.completion_gap_started_at = time.monotonic() - 1.0
        resume_calls = 0

        async def _resume(thread_id: str, **kwargs: object) -> dict[str, object]:
            nonlocal resume_calls
            _ = thread_id, kwargs
            resume_calls += 1
            return {}

        client.thread_resume = _resume  # type: ignore[method-assign]

        await client._handle_notification_item_started(
            {
                "method": "item/started",
                "params": {
                    "turnId": "turn-1",
                    "threadId": "thread-1",
                    "itemId": "tool-1",
                    "item": {"type": "commandExecution"},
                },
            },
            {
                "turnId": "turn-1",
                "threadId": "thread-1",
                "itemId": "tool-1",
                "item": {"type": "commandExecution"},
            },
        )

        assert state.active_item_ids == {"tool-1"}
        state.last_event_at = time.monotonic() - 1.0
        state.completion_gap_started_at = time.monotonic() - 1.0

        await client._maybe_reconcile_turn_completion_gap(
            state,
            turn_id="turn-1",
            thread_id="thread-1",
        )

        assert resume_calls == 0
    finally:
        await client.close()


@pytest.mark.anyio
async def test_turn_hint_progress_refreshes_completion_gap_window(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        turn_stall_timeout_seconds=10.0,
        turn_stall_poll_interval_seconds=0.02,
        turn_stall_recovery_min_interval_seconds=0.0,
        turn_completion_gap_timeout_seconds=0.05,
    )
    try:
        state = client._ensure_turn_state("turn-1", "thread-1")
        state.status = "running"
        state.item_completed_count = 3
        state.completion_gap_started_at = time.monotonic() - 1.0
        stale_started_at = state.completion_gap_started_at
        resume_calls = 0

        async def _resume(thread_id: str, **kwargs: object) -> dict[str, object]:
            nonlocal resume_calls
            _ = thread_id, kwargs
            resume_calls += 1
            return {}

        client.thread_resume = _resume  # type: ignore[method-assign]

        await client._mark_notification_turn_hint(
            method="item/commandExecution/outputDelta",
            params={"turnId": "turn-1", "threadId": "thread-1"},
        )
        assert state.completion_gap_started_at is not None
        assert state.completion_gap_started_at > stale_started_at

        await client._maybe_reconcile_turn_completion_gap(
            state,
            turn_id="turn-1",
            thread_id="thread-1",
        )

        assert resume_calls == 0
    finally:
        await client.close()


@pytest.mark.anyio
async def test_merge_turn_state_keeps_latest_event_timestamp_and_active_items(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
    )
    try:
        target = client._ensure_turn_state("turn-1", "thread-1")
        source = client._ensure_pending_turn_state("turn-1")
        target.last_event_at = time.monotonic() - 5.0
        target.last_method = "item/completed"
        target.active_item_ids.add("item-1")
        source.last_event_at = time.monotonic() - 1.0
        source.last_method = "item/started"
        source.active_item_ids.add("item-2")

        client._merge_turn_state(target, source)

        assert target.last_event_at == source.last_event_at
        assert target.last_method == "item/started"
        assert target.active_item_ids == {"item-1", "item-2"}
    finally:
        await client.close()


@pytest.mark.anyio
async def test_merge_turn_state_keeps_latest_completion_gap_timestamp(
    tmp_path: Path,
) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
    )
    try:
        target = client._ensure_turn_state("turn-1", "thread-1")
        source = client._ensure_pending_turn_state("turn-1")
        target.completion_gap_started_at = time.monotonic() - 5.0
        source.completion_gap_started_at = time.monotonic() - 1.0

        client._merge_turn_state(target, source)

        assert target.completion_gap_started_at == source.completion_gap_started_at
    finally:
        await client.close()


@pytest.mark.anyio
async def test_disconnect_with_autorestart_preserves_active_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS", raising=False)
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        auto_restart=True,
    )
    scheduled: list[bool] = []
    client._schedule_restart = lambda: scheduled.append(True)  # type: ignore[method-assign]
    turn_state = None
    try:
        loop = asyncio.get_running_loop()
        request_future = loop.create_future()
        client._pending["request-1"] = request_future
        turn_state = client._ensure_turn_state("turn-1", "thread-1")

        await client._handle_disconnect()

        assert scheduled == [True]
        assert request_future.done()
        with pytest.raises(CodexAppServerDisconnected):
            request_future.result()
        assert "request-1" not in client._pending
        assert not turn_state.future.done()
        assert ("thread-1", "turn-1") in client._turns
    finally:
        await client.close()
        if turn_state is not None and turn_state.future.done():
            _ = turn_state.future.exception()


@pytest.mark.anyio
async def test_disconnect_when_closed_fails_active_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS", raising=False)
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        auto_restart=True,
    )
    try:
        turn_state = client._ensure_turn_state("turn-1", "thread-1")
        client._closed = True

        await client._handle_disconnect()

        assert turn_state.future.done()
        with pytest.raises(CodexAppServerDisconnected):
            turn_state.future.result()
        assert not client._turns
    finally:
        await client.close()


@pytest.mark.anyio
async def test_disconnect_without_stall_recovery_fails_active_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS", raising=False)
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        auto_restart=True,
        turn_stall_timeout_seconds=None,
    )
    scheduled: list[bool] = []
    client._schedule_restart = lambda: scheduled.append(True)  # type: ignore[method-assign]
    try:
        turn_state = client._ensure_turn_state("turn-1", "thread-1")

        await client._handle_disconnect()

        assert scheduled == [True]
        assert turn_state.future.done()
        with pytest.raises(CodexAppServerDisconnected):
            turn_state.future.result()
        assert not client._turns
    finally:
        await client.close()


@pytest.mark.anyio
async def test_restart_after_crash(tmp_path: Path) -> None:
    client = CodexAppServerClient(
        fixture_command("crash"), cwd=tmp_path, auto_restart=True
    )
    try:
        await client.request("fixture/crash")
        await client.wait_for_disconnect(timeout=1)
        result = await client.request("fixture/echo", {"value": 42})
        assert result["value"] == 42
    finally:
        await client.close()


@pytest.mark.anyio
async def test_large_response_line(tmp_path: Path) -> None:
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        large_value = "x" * (256 * 1024)
        result = await client.request("fixture/echo", {"value": large_value})
        assert result["value"] == large_value
    finally:
        await client.close()


@pytest.mark.anyio
async def test_response_without_trailing_newline(tmp_path: Path) -> None:
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        auto_restart=False,
    )
    try:
        result = await client.request("fixture/echo_no_newline", {"value": "final"})
        assert result["value"] == "final"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_oversize_line_drops_and_preserves_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_server_client, "_MAX_MESSAGE_BYTES", 128)
    client = CodexAppServerClient(fixture_command("basic"), cwd=tmp_path)
    try:
        result = await client.request("fixture/oversize_drop", {"value": "ok"})
        assert result["value"] == "ok"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_small_line_is_not_dropped_before_oversize_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifications: list[dict] = []

    async def on_notification(message: dict) -> None:
        notifications.append(message)

    monkeypatch.setattr(app_server_client, "_MAX_MESSAGE_BYTES", 80)
    client = CodexAppServerClient(
        fixture_command("basic"),
        cwd=tmp_path,
        notification_handler=on_notification,
    )
    try:
        result = await client.request(
            "fixture/notification_then_response", {"value": "ok"}
        )
        assert result["value"] == "ok"
        assert any(
            event.get("method") == "fixture/ping"
            and event.get("params", {}).get("value") == "ok"
            for event in notifications
        )
    finally:
        await client.close()
