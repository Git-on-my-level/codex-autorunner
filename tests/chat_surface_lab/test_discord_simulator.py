from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.integrations.discord.errors import (
    DiscordPermanentError,
    DiscordTransientError,
)
from tests.chat_surface_lab.artifact_manifests import ArtifactKind
from tests.chat_surface_lab.discord_simulator import (
    DiscordSimulatorFaults,
    DiscordSurfaceSimulator,
)
from tests.chat_surface_lab.transcript_models import TranscriptEventKind


def _status_interaction_payload(interaction_id: str) -> dict[str, object]:
    return {
        "id": interaction_id,
        "token": f"{interaction_id}-token",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": "car",
            "options": [{"type": 1, "name": "status", "options": []}],
        },
    }


@pytest.mark.anyio
async def test_simulator_records_ack_followup_component_and_transcript(
    tmp_path: Path,
) -> None:
    simulator = DiscordSurfaceSimulator()

    await simulator.create_interaction_response(
        interaction_id="inter-1",
        interaction_token="token-1",
        payload={"type": 5, "data": {"flags": 64}},
    )
    await simulator.edit_original_interaction_response(
        application_id="app-1",
        interaction_token="token-1",
        payload={"content": "Working..."},
    )
    await simulator.create_followup_message(
        application_id="app-1",
        interaction_token="token-1",
        payload={"content": "Done", "flags": 64},
    )
    await simulator.create_interaction_response(
        interaction_id="inter-2",
        interaction_token="token-2",
        payload={"type": 6},
    )
    channel_message = await simulator.create_channel_message(
        channel_id="channel-1",
        payload={"content": "Received. Preparing turn..."},
    )
    await simulator.edit_channel_message(
        channel_id="channel-1",
        message_id=str(channel_message["id"]),
        payload={"content": "Done."},
    )
    await simulator.delete_channel_message(
        channel_id="channel-1",
        message_id=str(channel_message["id"]),
    )

    timeline = simulator.surface_timeline
    assert any(event.get("kind") == "ack" for event in timeline)
    assert any(
        event.get("kind") == "ack"
        and event.get("metadata", {}).get("ack_mode") == "defer_ephemeral"
        for event in timeline
    )
    assert any(
        event.get("kind") == "ack"
        and event.get("metadata", {}).get("ack_mode") == "defer_component_update"
        for event in timeline
    )
    assert any(event.get("kind") == "send" for event in timeline)
    assert any(event.get("kind") == "edit" for event in timeline)
    assert any(event.get("kind") == "delete" for event in timeline)

    transcript = simulator.to_normalized_transcript(scenario_id="discord-basics")
    kinds = [event.kind for event in transcript.events]
    assert TranscriptEventKind.ACK in kinds
    assert TranscriptEventKind.SEND in kinds
    assert TranscriptEventKind.EDIT in kinds
    assert TranscriptEventKind.DELETE in kinds
    first_ack_idx = next(
        i
        for i, event in enumerate(transcript.events)
        if event.kind == TranscriptEventKind.ACK
    )
    first_send_idx = next(
        i
        for i, event in enumerate(transcript.events)
        if event.kind == TranscriptEventKind.SEND
    )
    assert first_ack_idx < first_send_idx

    manifest = simulator.write_artifacts(
        output_dir=tmp_path / "artifacts",
        scenario_id="discord-basics",
        run_id="run-1",
    )
    artifact_kinds = {record.kind for record in manifest.artifacts}
    assert ArtifactKind.SURFACE_TIMELINE_JSON in artifact_kinds
    assert ArtifactKind.NORMALIZED_TRANSCRIPT_JSON in artifact_kinds
    for record in manifest.artifacts:
        assert record.path.exists()


@pytest.mark.anyio
async def test_simulator_injects_retry_after_and_delete_failure() -> None:
    simulator = DiscordSurfaceSimulator(
        faults=DiscordSimulatorFaults(
            retry_after_schedule={
                "create_channel_message": [1],
                "create_followup_message": [2],
            },
            fail_delete_message_ids={"msg-1"},
        )
    )

    with pytest.raises(DiscordTransientError, match="retry after 1"):
        await simulator.create_channel_message(
            channel_id="channel-1",
            payload={"content": "rate limited"},
        )
    message = await simulator.create_channel_message(
        channel_id="channel-1",
        payload={"content": "ok"},
    )
    with pytest.raises(DiscordTransientError, match="retry after 2"):
        await simulator.create_followup_message(
            application_id="app-1",
            interaction_token="token-1",
            payload={"content": "followup"},
        )
    with pytest.raises(RuntimeError, match="delete failed for msg-1"):
        await simulator.delete_channel_message(
            channel_id="channel-1",
            message_id=str(message["id"]),
        )

    timeline = simulator.surface_timeline
    assert any(
        event.get("kind") == "error"
        and event.get("metadata", {}).get("fault") == "retry_after"
        for event in timeline
    )
    assert any(
        event.get("kind") == "error"
        and event.get("metadata", {}).get("fault") == "delete_failed"
        for event in timeline
    )


@pytest.mark.anyio
async def test_simulator_injects_unknown_message_edit_failure() -> None:
    simulator = DiscordSurfaceSimulator(
        faults=DiscordSimulatorFaults(
            fail_unknown_message_edit_ids={"msg-1"},
        )
    )

    await simulator.create_channel_message(
        channel_id="channel-1",
        payload={"content": "preview"},
    )
    with pytest.raises(DiscordPermanentError, match="Unknown Message"):
        await simulator.edit_channel_message(
            channel_id="channel-1",
            message_id="msg-1",
            payload={"content": "updated"},
        )

    assert any(
        event.get("kind") == "error"
        and event.get("metadata", {}).get("fault") == "unknown_message"
        and event.get("metadata", {}).get("operation") == "edit_channel_message"
        for event in simulator.surface_timeline
    )


def test_simulator_can_inject_duplicate_interaction_delivery() -> None:
    simulator = DiscordSurfaceSimulator()
    simulator.enable_duplicate_interaction("inter-dup-1")
    payload = _status_interaction_payload("inter-dup-1")
    delivery = simulator.expand_interaction_delivery(payload)

    assert len(delivery) == 2
    assert delivery[0] is not delivery[1]
    assert delivery[0]["id"] == delivery[1]["id"] == "inter-dup-1"
    assert any(
        event.get("kind") == "duplicate_interaction_injected"
        for event in simulator.surface_timeline
    )
