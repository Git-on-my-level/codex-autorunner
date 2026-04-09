from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.integrations.discord.service_normalization import (
    DiscordAttachmentAdapter,
    SavedDiscordAttachment,
    build_attachment_context_payload,
    build_discord_approval_message,
    build_discord_queue_notice_message,
    format_discord_update_status_message,
    format_hub_flow_overview_line,
)


def test_build_discord_approval_message_shapes_prompt_and_components() -> None:
    message = build_discord_approval_message(
        {
            "method": "item/commandExecution/requestApproval",
            "params": {
                "reason": "Need permission",
                "command": ["/bin/zsh", "-c", "ps -p 123"],
            },
        },
        token="abc123",
    )

    assert message.content == (
        "Approval required\n"
        "Reason: Need permission\n"
        "Command: /bin/zsh -c ps -p 123"
    )
    payload = message.to_payload()
    action_rows = payload["components"]
    assert action_rows[0]["components"][0]["custom_id"] == "approval:abc123:accept"
    assert action_rows[0]["components"][1]["custom_id"] == "approval:abc123:decline"
    assert action_rows[1]["components"][0]["custom_id"] == "approval:abc123:cancel"


def test_build_discord_queue_notice_message_serializes_optional_components() -> None:
    with_buttons = build_discord_queue_notice_message(
        source_message_id="message-1",
    ).to_payload()
    without_buttons = build_discord_queue_notice_message(
        source_message_id=None,
    ).to_payload()

    assert with_buttons["content"] == "Queued (waiting for available worker...)"
    assert with_buttons["components"][0]["components"]
    assert "components" not in without_buttons


def test_discord_attachment_adapter_normalizes_audio_file_naming() -> None:
    adapter = DiscordAttachmentAdapter.from_raw(
        SimpleNamespace(
            file_id="att-1",
            file_name="voice-note",
            mime_type="audio/ogg",
            source_url="https://cdn.discordapp.com/attachments/voice-note",
            kind="audio",
            size_bytes=128,
        )
    )

    saved_name = adapter.build_saved_name(index=1, token="deadbeef")

    assert saved_name == "voice-note-deadbeef.ogg"
    assert (
        adapter.transcription_filename(
            saved_name=saved_name,
            mime_type=adapter.mime_type,
        )
        == saved_name
    )


def test_build_attachment_context_payload_formats_transcripts_and_images() -> None:
    payload = build_attachment_context_payload(
        prompt_text="Please review",
        saved=[
            SavedDiscordAttachment(
                original_name="voice-note.ogg",
                path=Path("/tmp/inbox/voice-note.ogg"),
                mime_type="audio/ogg",
                size_bytes=12,
                is_audio=True,
                is_image=False,
                transcript_text="spoken request",
            ),
            SavedDiscordAttachment(
                original_name="diagram.png",
                path=Path("/tmp/inbox/diagram.png"),
                mime_type="image/png",
                size_bytes=24,
                is_audio=False,
                is_image=True,
            ),
        ],
        failed=1,
        inbox_path=Path("/tmp/inbox"),
        outbox_path=Path("/tmp/outbox"),
        outbox_pending_path=Path("/tmp/outbox/pending"),
        max_message_length=2000,
        voice_provider_name="openai_whisper",
        whisper_transcript_disclaimer="Transcript may be inaccurate.",
    )

    assert payload.saved_count == 2
    assert payload.failed_count == 1
    assert "Please review\n\nInbound Discord attachments:" in payload.prompt_text
    assert "Transcript: spoken request" in payload.prompt_text
    assert "Transcript may be inaccurate." in payload.prompt_text
    assert "Use inbox files as local inputs" in payload.prompt_text
    assert payload.user_visible_transcript == "User:\nspoken request"
    assert payload.native_input_items_payload == [
        {"type": "localImage", "path": "/tmp/inbox/diagram.png"}
    ]


def test_format_discord_update_status_message_adds_ref_and_log() -> None:
    rendered = format_discord_update_status_message(
        {
            "status": "ok",
            "message": "Update completed.",
            "repo_ref": "main",
            "log_path": "/tmp/update.log",
        }
    )

    assert "Update status: ok" in rendered
    assert "Message: Update completed." in rendered
    assert "Ref: main" in rendered
    assert "Log: /tmp/update.log" in rendered


def test_format_hub_flow_overview_line_appends_duration_and_staleness() -> None:
    line = format_hub_flow_overview_line(
        line_label="workspace",
        is_worktree=True,
        status="paused",
        done_count=2,
        total_count=5,
        run_id="run-123",
        duration_label="1h 45m",
        freshness={
            "is_stale": True,
            "status": "lagging",
            "recency_basis": "event",
            "age_seconds": 120,
        },
    )

    assert line.startswith("  -> ")
    assert "workspace" in line
    assert "2/5" in line
    assert "run run-123" in line
    assert "took 1h 45m" in line
    assert "snapshot lagging" in line
