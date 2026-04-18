from __future__ import annotations

from tests.chat_surface_lab.transcript_models import (
    TranscriptEvent,
    TranscriptEventKind,
    TranscriptParty,
    TranscriptTimeline,
)
from tests.chat_surface_lab.transcript_renderer import (
    render_transcript_html,
    transcript_event_payload,
)


def test_renderer_is_deterministic_and_escapes_text() -> None:
    transcript = TranscriptTimeline(
        scenario_id="renderer-determinism",
        metadata={"z_key": "tail", "a_key": "head"},
        events=(
            TranscriptEvent(
                kind=TranscriptEventKind.ACK,
                party=TranscriptParty.PLATFORM,
                timestamp_ms=1000,
                surface_kind="discord",
                text="ack <ok>",
                metadata={"operation": "create_interaction_response"},
            ),
            TranscriptEvent(
                kind=TranscriptEventKind.SEND,
                party=TranscriptParty.ASSISTANT,
                timestamp_ms=1250,
                surface_kind="discord",
                text="Hello & welcome",
                metadata={"message_id": "m-1"},
            ),
        ),
    )

    first_html = render_transcript_html(transcript)
    second_html = render_transcript_html(transcript)

    assert first_html == second_html
    assert "&lt;ok&gt;" in first_html
    assert "Hello &amp; welcome" in first_html
    assert "&quot;a_key&quot;: &quot;head&quot;" in first_html
    assert "&quot;z_key&quot;: &quot;tail&quot;" in first_html
    assert 'data-event-index="1"' in first_html
    assert 'data-event-index="2"' in first_html


def test_transcript_event_payload_has_stable_index_and_delta_fields() -> None:
    transcript = TranscriptTimeline(
        scenario_id="renderer-payload",
        events=(
            TranscriptEvent(
                kind=TranscriptEventKind.STATUS,
                party=TranscriptParty.PLATFORM,
                timestamp_ms=200,
                surface_kind="telegram",
                text="typing",
            ),
            TranscriptEvent(
                kind=TranscriptEventKind.SEND,
                party=TranscriptParty.ASSISTANT,
                timestamp_ms=350,
                surface_kind="telegram",
                text="done",
            ),
        ),
    )

    payload = transcript_event_payload(transcript)
    assert payload[0]["index"] == 1
    assert payload[0]["delta_ms"] == 0
    assert payload[1]["index"] == 2
    assert payload[1]["delta_ms"] == 150
