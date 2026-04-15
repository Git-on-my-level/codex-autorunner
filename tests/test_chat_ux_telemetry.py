from __future__ import annotations

import logging
from unittest.mock import MagicMock

from codex_autorunner.integrations.chat.chat_ux_telemetry import (
    ChatUxFailureReason,
    ChatUxMilestone,
    ChatUxTimingAccumulator,
    ChatUxTimingSnapshot,
    emit_chat_ux_timing,
    format_chat_ux_summary,
    get_global_accumulator,
    reset_global_accumulator,
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


class TestAccumulator:
    def setup_method(self) -> None:
        reset_global_accumulator()

    def teardown_method(self) -> None:
        reset_global_accumulator()

    def test_accumulator_records_and_counts(self) -> None:
        acc = ChatUxTimingAccumulator(max_snapshots=10)
        assert acc.snapshot_count == 0
        snap = ChatUxTimingSnapshot(platform="discord")
        acc.record_snapshot(snap)
        assert acc.snapshot_count == 1

    def test_accumulator_respects_max(self) -> None:
        acc = ChatUxTimingAccumulator(max_snapshots=3)
        for i in range(5):
            acc.record_snapshot(
                ChatUxTimingSnapshot(platform="discord", channel_id=str(i))
            )
        assert acc.snapshot_count == 3

    def test_platform_summaries_basic(self) -> None:
        acc = ChatUxTimingAccumulator()
        snap = ChatUxTimingSnapshot(platform="discord")
        snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
        snap.record(ChatUxMilestone.ACK_FINISHED, now=101.0)
        snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=105.0)
        acc.record_snapshot(snap)

        summaries = acc.platform_summaries()
        assert len(summaries) == 1
        assert summaries[0].platform == "discord"
        assert summaries[0].total_snapshots == 1
        assert any(ds.label == "ack" for ds in summaries[0].deltas)
        assert any(ds.label == "terminal" for ds in summaries[0].deltas)

    def test_platform_summaries_separates_platforms(self) -> None:
        acc = ChatUxTimingAccumulator()
        for _ in range(3):
            s = ChatUxTimingSnapshot(platform="discord")
            s.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
            s.record(ChatUxMilestone.TERMINAL_DELIVERY, now=105.0)
            acc.record_snapshot(s)
        for _ in range(2):
            s = ChatUxTimingSnapshot(platform="telegram")
            s.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
            s.record(ChatUxMilestone.TERMINAL_DELIVERY, now=106.0)
            acc.record_snapshot(s)

        summaries = acc.platform_summaries()
        assert len(summaries) == 2
        platforms = {ps.platform: ps for ps in summaries}
        assert platforms["discord"].total_snapshots == 3
        assert platforms["telegram"].total_snapshots == 2

    def test_failure_counting(self) -> None:
        acc = ChatUxTimingAccumulator()
        s1 = ChatUxTimingSnapshot(platform="discord")
        s1.failure_reason = ChatUxFailureReason.QUEUE_STARVATION
        s1.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
        acc.record_snapshot(s1)

        s2 = ChatUxTimingSnapshot(platform="discord")
        s2.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
        acc.record_snapshot(s2)

        summaries = acc.platform_summaries()
        assert summaries[0].failure_count == 1
        assert summaries[0].failure_reasons == (("queue_starvation", 1),)

    def test_format_diagnostic_lines_empty(self) -> None:
        acc = ChatUxTimingAccumulator()
        lines = acc.format_diagnostic_lines()
        assert lines == ["Chat UX timing: no data collected yet."]

    def test_format_diagnostic_lines_with_data(self) -> None:
        acc = ChatUxTimingAccumulator()
        snap = ChatUxTimingSnapshot(platform="telegram")
        snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
        snap.record(ChatUxMilestone.ACK_FINISHED, now=101.0)
        snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=110.0)
        acc.record_snapshot(snap)

        lines = acc.format_diagnostic_lines()
        assert any("telegram" in line for line in lines)
        assert any("ack" in line for line in lines)
        assert any("terminal" in line for line in lines)

    def test_global_accumulator_singleton(self) -> None:
        reset_global_accumulator()
        a1 = get_global_accumulator()
        a2 = get_global_accumulator()
        assert a1 is a2
        reset_global_accumulator()

    def test_emit_chat_ux_timing_feeds_accumulator(self) -> None:
        reset_global_accumulator()
        logger = MagicMock(spec=logging.Logger)
        snap = ChatUxTimingSnapshot(platform="discord")
        snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
        snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=105.0)
        emit_chat_ux_timing(logger, logging.INFO, snap)

        acc = get_global_accumulator()
        assert acc.snapshot_count >= 1
        reset_global_accumulator()


class TestDoctorDiagnostics:
    def setup_method(self) -> None:
        reset_global_accumulator()

    def teardown_method(self) -> None:
        reset_global_accumulator()

    def test_empty_accumulator_returns_info_check(self) -> None:
        from codex_autorunner.integrations.chat.doctor import (
            chat_ux_timing_diagnostic_checks,
        )

        checks = chat_ux_timing_diagnostic_checks()
        assert len(checks) == 1
        assert checks[0].passed
        assert "no data" in checks[0].message

    def test_normal_timing_returns_passing_check(self) -> None:
        from codex_autorunner.integrations.chat.doctor import (
            chat_ux_timing_diagnostic_checks,
        )

        acc = get_global_accumulator()
        snap = ChatUxTimingSnapshot(platform="discord")
        snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
        snap.record(ChatUxMilestone.ACK_FINISHED, now=100.1)
        snap.record(ChatUxMilestone.FIRST_VISIBLE_FEEDBACK, now=100.2)
        snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=100.5)
        acc.record_snapshot(snap)

        checks = chat_ux_timing_diagnostic_checks()
        assert any(c.name == "Chat UX timing accumulator" and c.passed for c in checks)
        assert not any("slow path" in c.name for c in checks)

    def test_slow_p95_triggers_failure_check(self) -> None:
        from codex_autorunner.integrations.chat.doctor import (
            chat_ux_timing_diagnostic_checks,
        )

        acc = get_global_accumulator()
        for _i in range(5):
            snap = ChatUxTimingSnapshot(platform="telegram")
            snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
            snap.record(ChatUxMilestone.ACK_FINISHED, now=100.1)
            snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=100.5)
            acc.record_snapshot(snap)
        slow_snap = ChatUxTimingSnapshot(platform="telegram")
        slow_snap.record(ChatUxMilestone.RAW_EVENT_RECEIVED, now=100.0)
        slow_snap.record(ChatUxMilestone.ACK_FINISHED, now=105.0)
        slow_snap.record(ChatUxMilestone.TERMINAL_DELIVERY, now=106.0)
        acc.record_snapshot(slow_snap)

        checks = chat_ux_timing_diagnostic_checks()
        slow_checks = [c for c in checks if "slow path" in c.name]
        assert len(slow_checks) == 1
        assert not slow_checks[0].passed
        assert "telegram" in slow_checks[0].name
