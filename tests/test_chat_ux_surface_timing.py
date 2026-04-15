from __future__ import annotations

import json
import logging

import pytest

from codex_autorunner.integrations.chat.chat_ux_telemetry import (
    ChatUxFailureReason,
    ChatUxMilestone,
    ChatUxTimingSnapshot,
    emit_chat_ux_timing,
)


@pytest.fixture
def capture_log_events():
    events: list[dict] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                payload = json.loads(record.getMessage())
                events.append(payload)
            except (json.JSONDecodeError, ValueError):
                pass

    handler = _CaptureHandler()
    logger = logging.getLogger("test.chat_ux.surface")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    yield events, logger

    logger.removeHandler(handler)


class TestDiscordTimingEmission:
    def test_discord_turn_lifecycle_emits_milestones(self, capture_log_events) -> None:
        events, logger = capture_log_events
        snap = ChatUxTimingSnapshot(
            platform="discord",
            channel_id="ch_discord",
            conversation_id="conv1",
            agent="opencode",
        )
        snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
        snap.record(ChatUxMilestone.ACK_FINISHED, now=101.0)
        snap.record(ChatUxMilestone.FIRST_VISIBLE_FEEDBACK, now=102.0)
        snap.record(ChatUxMilestone.FIRST_SEMANTIC_PROGRESS, now=103.0)
        snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=110.0)

        emit_chat_ux_timing(
            logger,
            logging.INFO,
            snap,
            event_suffix="turn_delivery",
            session_key="discord:ch_discord:abc",
            execution_id="exec-123",
        )

        assert len(events) == 1
        event = events[0]
        assert event["event"] == "chat_ux_timing.discord.turn_delivery"
        assert event["chat_ux_platform"] == "discord"
        assert event["chat_ux_channel_id"] == "ch_discord"
        assert event["chat_ux_conversation_id"] == "conv1"
        assert event["chat_ux_agent"] == "opencode"
        assert "chat_ux_delta_ack_ms" in event
        assert "chat_ux_delta_first_visible_ms" in event
        assert "chat_ux_delta_first_progress_ms" in event
        assert "chat_ux_delta_terminal_ms" in event
        assert event["chat_ux_delta_terminal_ms"] == 10000.0

    def test_discord_interrupt_emits_milestone(self, capture_log_events) -> None:
        events, logger = capture_log_events
        snap = ChatUxTimingSnapshot(platform="discord", channel_id="ch_cancel")
        snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
        snap.record(ChatUxMilestone.INTERRUPT_REQUESTED_VISIBLE, now=101.5)

        emit_chat_ux_timing(
            logger,
            logging.INFO,
            snap,
            event_suffix="cancel_acknowledged",
        )

        assert len(events) == 1
        event = events[0]
        assert event["event"] == "chat_ux_timing.discord.cancel_acknowledged"
        assert "chat_ux_delta_interrupt_visible_ms" in event

    def test_discord_submission_timeout_sets_failure_reason(
        self,
    ) -> None:
        snap = ChatUxTimingSnapshot(platform="discord", channel_id="ch_timeout")
        snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
        snap.failure_reason = ChatUxFailureReason.SUBMISSION_TIMEOUT

        fields = snap.to_log_fields()
        assert fields["chat_ux_failure_reason"] == "submission_timeout"


class TestTelegramTimingEmission:
    def test_telegram_managed_turn_lifecycle_emits_milestones(
        self, capture_log_events
    ) -> None:
        events, logger = capture_log_events
        snap = ChatUxTimingSnapshot(
            platform="telegram",
            channel_id="123",
            agent="opencode",
        )
        snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=200.0)
        snap.record(ChatUxMilestone.FIRST_VISIBLE_FEEDBACK, now=201.0)
        snap.record(ChatUxMilestone.FIRST_SEMANTIC_PROGRESS, now=203.0)
        snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=210.0)

        emit_chat_ux_timing(
            logger,
            logging.INFO,
            snap,
            event_suffix="managed_thread_turn",
            topic_key="123:456",
            chat_id=123,
            thread_id=456,
            status="ok",
        )

        assert len(events) == 1
        event = events[0]
        assert event["event"] == "chat_ux_timing.telegram.managed_thread_turn"
        assert event["chat_ux_platform"] == "telegram"
        assert event["chat_ux_delta_first_visible_ms"] == 1000.0
        assert event["chat_ux_delta_first_progress_ms"] == 3000.0
        assert event["chat_ux_delta_terminal_ms"] == 10000.0

    def test_telegram_interrupt_emits_milestone(self, capture_log_events) -> None:
        events, logger = capture_log_events
        snap = ChatUxTimingSnapshot(platform="telegram", channel_id="456")
        snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=300.0)
        snap.record(ChatUxMilestone.INTERRUPT_REQUESTED_VISIBLE, now=301.0)

        fields = snap.to_log_fields()
        assert "chat_ux_delta_interrupt_visible_ms" in fields

    def test_telegram_queue_visible_milestone(self) -> None:
        snap = ChatUxTimingSnapshot(platform="telegram")
        snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
        snap.record(ChatUxMilestone.QUEUE_VISIBLE, now=102.0)

        delta = snap.delta_ms(
            ChatUxMilestone.RAW_EVENT_RECEIVED, ChatUxMilestone.QUEUE_VISIBLE
        )
        assert delta == 2000.0


class TestCrossSurfaceTimingParity:
    def test_both_surfaces_emit_comparable_delta_fields(self) -> None:
        discord_snap = ChatUxTimingSnapshot(platform="discord")
        telegram_snap = ChatUxTimingSnapshot(platform="telegram")

        for snap in (discord_snap, telegram_snap):
            snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
            snap.record(ChatUxMilestone.ACK_FINISHED, now=101.0)
            snap.record(ChatUxMilestone.FIRST_VISIBLE_FEEDBACK, now=102.0)
            snap.record(ChatUxMilestone.QUEUE_VISIBLE, now=103.0)
            snap.record(ChatUxMilestone.FIRST_SEMANTIC_PROGRESS, now=104.0)
            snap.record(ChatUxMilestone.INTERRUPT_REQUESTED_VISIBLE, now=105.0)
            snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=110.0)

        d_fields = discord_snap.to_log_fields()
        t_fields = telegram_snap.to_log_fields()

        timing_keys = [k for k in d_fields if k.startswith("chat_ux_delta_")]
        assert len(timing_keys) >= 4, f"Expected >= 4 delta keys, got {timing_keys}"

        for key in timing_keys:
            assert key in t_fields, f"Telegram missing comparable field: {key}"

    def test_failure_reasons_shared_across_surfaces(self) -> None:
        shared_reasons = {
            ChatUxFailureReason.PLATFORM_ACK_TIMEOUT,
            ChatUxFailureReason.DELIVERY_REPLAY_FAILED,
            ChatUxFailureReason.BACKEND_INTERRUPT_TIMEOUT,
            ChatUxFailureReason.QUEUE_STARVATION,
            ChatUxFailureReason.CALLBACK_ACK_DELAYED,
            ChatUxFailureReason.SUBMISSION_TIMEOUT,
            ChatUxFailureReason.RUNTIME_ERROR,
        }
        for reason in shared_reasons:
            for platform in ("discord", "telegram"):
                snap = ChatUxTimingSnapshot(platform=platform)
                snap.failure_reason = reason
                fields = snap.to_log_fields()
                assert fields["chat_ux_failure_reason"] == reason.value
