from __future__ import annotations

import logging
from unittest.mock import MagicMock

from codex_autorunner.integrations.chat.chat_ux_telemetry import (
    ChatUxFailureReason,
    ChatUxMilestone,
    ChatUxTimingSnapshot,
    emit_chat_ux_timing,
    format_chat_ux_summary,
)


def test_milestone_enum_has_required_values() -> None:
    expected = {
        "raw_event_received",
        "ack_finished",
        "first_visible_feedback",
        "queue_visible",
        "first_semantic_progress",
        "interrupt_requested_visible",
        "terminal_delivery",
    }
    actual = {m.value for m in ChatUxMilestone}
    assert expected.issubset(actual), f"missing: {expected - actual}"


def test_failure_reason_enum_has_required_values() -> None:
    expected = {
        "platform_ack_timeout",
        "delivery_replay_failed",
        "backend_interrupt_timeout",
        "queue_starvation",
        "callback_ack_delayed",
    }
    actual = {r.value for r in ChatUxFailureReason}
    assert expected.issubset(actual), f"missing: {expected - actual}"


def test_snapshot_record_milestone() -> None:
    snap = ChatUxTimingSnapshot(platform="discord")
    assert not snap.has_milestone(ChatUxMilestone.RAW_EVENT_RECEIVED)
    snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
    assert snap.has_milestone(ChatUxMilestone.RAW_EVENT_RECEIVED)
    assert snap.milestones[ChatUxMilestone.RAW_EVENT_RECEIVED] == 100.0


def test_snapshot_record_first_only() -> None:
    snap = ChatUxTimingSnapshot(platform="discord")
    snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
    snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=200.0)
    assert snap.milestones[ChatUxMilestone.RAW_EVENT_RECEIVED] == 100.0


def test_snapshot_delta_ms() -> None:
    snap = ChatUxTimingSnapshot(platform="discord")
    snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
    snap.record(ChatUxMilestone.ACK_FINISHED, now=101.5)
    delta = snap.delta_ms(
        ChatUxMilestone.RAW_EVENT_RECEIVED, ChatUxMilestone.ACK_FINISHED
    )
    assert delta == 1500.0


def test_snapshot_delta_ms_missing_start() -> None:
    snap = ChatUxTimingSnapshot(platform="discord")
    snap.record(ChatUxMilestone.ACK_FINISHED, now=101.0)
    delta = snap.delta_ms(
        ChatUxMilestone.RAW_EVENT_RECEIVED, ChatUxMilestone.ACK_FINISHED
    )
    assert delta is None


def test_snapshot_to_log_fields_basic() -> None:
    snap = ChatUxTimingSnapshot(platform="telegram", channel_id="123", agent="opencode")
    snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
    snap.record(ChatUxMilestone.ACK_FINISHED, now=101.0)
    snap.record(ChatUxMilestone.FIRST_VISIBLE_FEEDBACK, now=102.0)
    snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=105.0)
    fields = snap.to_log_fields()
    assert fields["chat_ux_platform"] == "telegram"
    assert fields["chat_ux_channel_id"] == "123"
    assert fields["chat_ux_agent"] == "opencode"
    assert "chat_ux_delta_ack_ms" in fields
    assert "chat_ux_delta_first_visible_ms" in fields
    assert "chat_ux_delta_terminal_ms" in fields


def test_snapshot_to_log_fields_with_failure() -> None:
    snap = ChatUxTimingSnapshot(platform="discord")
    snap.failure_reason = ChatUxFailureReason.PLATFORM_ACK_TIMEOUT
    fields = snap.to_log_fields()
    assert fields["chat_ux_failure_reason"] == "platform_ack_timeout"


def test_snapshot_no_conversation_id_omitted() -> None:
    snap = ChatUxTimingSnapshot(platform="discord")
    fields = snap.to_log_fields()
    assert "chat_ux_conversation_id" not in fields


def test_emit_chat_ux_timing_logs_event() -> None:
    logger = MagicMock(spec=logging.Logger)
    snap = ChatUxTimingSnapshot(platform="discord")
    snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
    snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=105.0)
    emit_chat_ux_timing(logger, logging.INFO, snap, event_suffix="test_done")
    logger.log.assert_called_once()
    args, _kwargs = logger.log.call_args
    assert args[0] == logging.INFO
    msg = args[1]
    assert "chat_ux_timing.discord.test_done" in msg
    assert "chat_ux_platform" in msg


def test_format_chat_ux_summary_basic() -> None:
    snap = ChatUxTimingSnapshot(platform="discord")
    snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
    snap.record(ChatUxMilestone.ACK_FINISHED, now=101.0)
    snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=105.0)
    summary = format_chat_ux_summary(snap)
    assert "[discord]" in summary
    assert "ack=" in summary
    assert "terminal=" in summary


def test_format_chat_ux_summary_with_failure() -> None:
    snap = ChatUxTimingSnapshot(platform="telegram")
    snap.failure_reason = ChatUxFailureReason.QUEUE_STARVATION
    snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
    summary = format_chat_ux_summary(snap)
    assert "fail=queue_starvation" in summary


def test_full_lifecycle_snapshots_match() -> None:
    discord_snap = ChatUxTimingSnapshot(platform="discord", channel_id="ch1")
    telegram_snap = ChatUxTimingSnapshot(platform="telegram", channel_id="123")

    for snap in (discord_snap, telegram_snap):
        snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
        snap.record(ChatUxMilestone.ACK_FINISHED, now=101.0)
        snap.record(ChatUxMilestone.FIRST_VISIBLE_FEEDBACK, now=102.0)
        snap.record(ChatUxMilestone.FIRST_SEMANTIC_PROGRESS, now=103.0)
        snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=110.0)

    d_fields = discord_snap.to_log_fields()
    t_fields = telegram_snap.to_log_fields()

    shared_delta_keys = [
        "chat_ux_delta_ack_ms",
        "chat_ux_delta_first_visible_ms",
        "chat_ux_delta_first_progress_ms",
        "chat_ux_delta_terminal_ms",
    ]
    for key in shared_delta_keys:
        assert key in d_fields, f"Discord missing {key}"
        assert key in t_fields, f"Telegram missing {key}"
        assert d_fields[key] == t_fields[key], f"Mismatch for {key}"
