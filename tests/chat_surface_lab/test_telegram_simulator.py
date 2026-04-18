from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.integrations.telegram.adapter import (
    TelegramAPIError,
    TelegramMessage,
    TelegramUpdate,
)
from tests.chat_surface_lab.artifact_manifests import ArtifactKind
from tests.chat_surface_lab.telegram_simulator import (
    TelegramSimulatorFaults,
    TelegramSurfaceSimulator,
)
from tests.chat_surface_lab.transcript_models import TranscriptEventKind


def _build_update(update_id: int, *, thread_id: int | None) -> TelegramUpdate:
    return TelegramUpdate(
        update_id=update_id,
        message=TelegramMessage(
            update_id=update_id,
            message_id=update_id,
            chat_id=123,
            thread_id=thread_id,
            from_user_id=456,
            text="hello",
            date=0,
            is_topic_message=thread_id is not None,
        ),
        callback=None,
    )


@pytest.mark.anyio
async def test_simulator_records_chunking_and_topic_root_routing(
    tmp_path: Path,
) -> None:
    simulator = TelegramSurfaceSimulator()
    chunked_text = "x" * 9000
    responses = await simulator.send_message_chunks(
        123,
        chunked_text,
        message_thread_id=55,
        reply_to_message_id=9,
        max_len=4096,
    )
    await simulator.send_message(123, "root message", message_thread_id=None)

    assert len(responses) == 3
    assert [msg["thread_id"] for msg in simulator.messages[:3]] == [55, 55, 55]
    assert simulator.messages[0]["reply_to"] == 9
    assert simulator.messages[1]["reply_to"] is None
    assert simulator.messages[2]["reply_to"] is None
    assert simulator.messages[-1]["thread_id"] is None

    timeline = simulator.surface_timeline
    topic_keys = {
        str(event.get("metadata", {}).get("topic_key"))
        for event in timeline
        if isinstance(event.get("metadata"), dict)
    }
    assert "123:55" in topic_keys
    assert "123:root" in topic_keys
    assert any(event.get("kind") == "chunking" for event in timeline)

    transcript = simulator.to_normalized_transcript(scenario_id="chunking-routing")
    kinds = [event.kind for event in transcript.events]
    assert TranscriptEventKind.SEND in kinds
    assert TranscriptEventKind.STATUS in kinds

    manifest = simulator.write_artifacts(
        output_dir=tmp_path / "artifacts",
        scenario_id="chunking-routing",
        run_id="run-1",
    )
    artifact_kinds = {record.kind for record in manifest.artifacts}
    assert ArtifactKind.SURFACE_TIMELINE_JSON in artifact_kinds
    assert ArtifactKind.NORMALIZED_TRANSCRIPT_JSON in artifact_kinds
    for record in manifest.artifacts:
        assert record.path.exists()


@pytest.mark.anyio
async def test_simulator_injects_retry_after_and_delete_failure() -> None:
    simulator = TelegramSurfaceSimulator(
        faults=TelegramSimulatorFaults(
            retry_after_schedule={"send_message": [1]},
            fail_delete_message_ids={2},
        )
    )

    with pytest.raises(TelegramAPIError, match="retry after 1"):
        await simulator.send_message(123, "retry me")

    await simulator.send_message(123, "ok-1")
    await simulator.send_message(123, "ok-2")
    with pytest.raises(RuntimeError, match="delete failed for 2"):
        await simulator.delete_message(123, 2, message_thread_id=55)

    timeline = simulator.surface_timeline
    retry_errors = [
        event
        for event in timeline
        if event.get("kind") == "error"
        and event.get("metadata", {}).get("fault") == "retry_after"
    ]
    delete_errors = [
        event
        for event in timeline
        if event.get("kind") == "error"
        and event.get("metadata", {}).get("operation") == "delete_message"
    ]
    assert retry_errors
    assert delete_errors


@pytest.mark.anyio
async def test_simulator_models_parse_mode_and_callback_ack() -> None:
    simulator = TelegramSurfaceSimulator(
        faults=TelegramSimulatorFaults(
            parse_mode_rejections={"MarkdownV2": ("__",)},
        )
    )

    with pytest.raises(TelegramAPIError, match="can't parse entities"):
        await simulator.send_message(
            123,
            "this __breaks__ markdown",
            parse_mode="MarkdownV2",
        )

    await simulator.answer_callback_query(
        "cb-1",
        chat_id=123,
        thread_id=55,
        message_id=7,
        text="Stopping...",
        show_alert=False,
    )
    assert simulator.callback_answers[-1]["text"] == "Stopping..."
    assert any(event.get("kind") == "callback" for event in simulator.surface_timeline)


def test_simulator_can_inject_duplicate_update_delivery() -> None:
    simulator = TelegramSurfaceSimulator()
    simulator.enable_duplicate_update(42)
    update = _build_update(42, thread_id=55)
    delivery = simulator.expand_update_delivery(update)

    assert len(delivery) == 2
    assert delivery[0].update_id == delivery[1].update_id == 42
    assert any(
        event.get("kind") == "duplicate_update_injected"
        for event in simulator.surface_timeline
    )
