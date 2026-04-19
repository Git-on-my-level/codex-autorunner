from __future__ import annotations

import html
import json
from typing import Any

from .transcript_models import TranscriptTimeline


def render_transcript_html(transcript: TranscriptTimeline) -> str:
    """Render a deterministic, diff-friendly transcript artifact page."""
    events = list(transcript.events)
    start_ts = min((event.timestamp_ms for event in events), default=0)
    metadata_block = json.dumps(dict(transcript.metadata), indent=2, sort_keys=True)

    event_rows: list[str] = []
    for index, event in enumerate(events, start=1):
        delta_ms = max(event.timestamp_ms - start_ts, 0) if events else 0
        metadata_json = json.dumps(dict(event.metadata), sort_keys=True)
        event_rows.append(
            "\n".join(
                [
                    f'<article class="event" data-event-index="{index}" '
                    f'data-kind="{event.kind.value}" data-party="{event.party.value}">',
                    "  <header>",
                    f'    <span class="event-index">#{index:03d}</span>',
                    f'    <span class="event-kind">{html.escape(event.kind.value)}</span>',
                    f'    <span class="event-party">{html.escape(event.party.value)}</span>',
                    f'    <span class="event-ts">{event.timestamp_ms}ms</span>',
                    f'    <span class="event-delta">+{delta_ms}ms</span>',
                    "  </header>",
                    f"  <p>{html.escape(event.text)}</p>",
                    f"  <pre>{html.escape(metadata_json)}</pre>",
                    "</article>",
                ]
            )
        )

    rows_block = "\n".join(event_rows) or '<p class="empty">No events captured.</p>'

    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8" />\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"  <title>Chat Surface Transcript · {html.escape(transcript.scenario_id)}</title>\n"
        "  <style>\n"
        "    body { margin: 24px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }\n"
        "    h1 { margin: 0 0 12px 0; font-size: 20px; }\n"
        "    .meta { margin: 0 0 16px 0; }\n"
        "    .event { border: 1px solid #d9d9d9; border-radius: 6px; padding: 10px; margin: 10px 0; }\n"
        "    .event header { display: flex; gap: 8px; flex-wrap: wrap; font-size: 12px; color: #444; }\n"
        "    .event p { margin: 8px 0; white-space: pre-wrap; }\n"
        "    pre { margin: 0; background: #f7f7f7; padding: 8px; overflow-x: auto; }\n"
        "    .empty { border: 1px dashed #bbb; padding: 12px; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        f"  <h1>Scenario: {html.escape(transcript.scenario_id)}</h1>\n"
        '  <section class="meta">\n'
        f"    <div>Total events: {len(events)}</div>\n"
        "    <h2>Metadata</h2>\n"
        f"    <pre>{html.escape(metadata_block)}</pre>\n"
        "  </section>\n"
        "  <section>\n"
        "    <h2>Timeline</h2>\n"
        f"{rows_block}\n"
        "  </section>\n"
        "</body>\n"
        "</html>\n"
    )


def transcript_event_payload(transcript: TranscriptTimeline) -> list[dict[str, Any]]:
    """Serialize events into a deterministic JSON-friendly shape."""
    events = list(transcript.events)
    start_ts = min((event.timestamp_ms for event in events), default=0)
    payload: list[dict[str, Any]] = []
    for index, event in enumerate(events, start=1):
        payload.append(
            {
                "index": index,
                "kind": event.kind.value,
                "party": event.party.value,
                "timestamp_ms": event.timestamp_ms,
                "delta_ms": max(event.timestamp_ms - start_ts, 0),
                "surface_kind": event.surface_kind,
                "text": event.text,
                "metadata": dict(event.metadata),
            }
        )
    return payload
