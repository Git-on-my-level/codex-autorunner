from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

from codex_autorunner.integrations.discord import message_turns as discord_message_turns
from codex_autorunner.integrations.telegram.handlers.commands import (
    execution as telegram_execution,
)

from .harness import (
    DiscordSurfaceHarness,
    FakeDiscordRest,
    FakeTelegramBot,
    HermesFixtureRuntime,
    TelegramSurfaceHarness,
    patch_hermes_runtime,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class SurfaceParityCase:
    case_id: str
    runtime_scenario: str
    prompt: str
    expected_status: str
    expected_completion_source: str
    expected_reply_substrings: dict[str, str]
    expected_progress_state: dict[str, str]
    expect_progress_retired: bool
    expected_detail_substrings: Optional[dict[str, str]] = None
    harness_timeout_seconds: float = 2.0
    use_short_runtime_timeout: bool = False
    approval_mode: Optional[str] = None
    fail_progress_delete: bool = False


@dataclass(frozen=True)
class SurfaceSnapshot:
    surface: str
    terminal_status: str
    execution_status: str
    completion_source: Optional[str]
    final_user_visible_message: str
    progress_retired: bool
    progress_state: str
    thread_target_id: Optional[str]
    execution_id: Optional[str]
    detail: Optional[str]


CASES = (
    SurfaceParityCase(
        case_id="official_completion",
        runtime_scenario="official",
        prompt="echo hello world",
        expected_status="ok",
        expected_completion_source="prompt_return",
        expected_reply_substrings={
            "discord": "fixture reply",
            "telegram": "fixture reply",
        },
        expected_progress_state={
            "discord": "done",
            "telegram": "retired",
        },
        expect_progress_retired=True,
    ),
    SurfaceParityCase(
        case_id="content_parts",
        runtime_scenario="official_content_parts",
        prompt="echo hello world",
        expected_status="ok",
        expected_completion_source="prompt_return",
        expected_reply_substrings={
            "discord": "fixture reply",
            "telegram": "fixture reply",
        },
        expected_progress_state={
            "discord": "done",
            "telegram": "retired",
        },
        expect_progress_retired=True,
    ),
    SurfaceParityCase(
        case_id="terminal_before_return",
        runtime_scenario="official_terminal_before_return",
        prompt="echo hello world",
        expected_status="ok",
        expected_completion_source="prompt_return",
        expected_reply_substrings={
            "discord": "fixture reply",
            "telegram": "fixture reply",
        },
        expected_progress_state={
            "discord": "done",
            "telegram": "retired",
        },
        expect_progress_retired=True,
    ),
    SurfaceParityCase(
        case_id="session_status_idle_before_return",
        runtime_scenario="official_session_status_idle_before_return",
        prompt="echo hello world",
        expected_status="ok",
        expected_completion_source="prompt_return",
        expected_reply_substrings={
            "discord": "fixture reply",
            "telegram": "fixture reply",
        },
        expected_progress_state={
            "discord": "done",
            "telegram": "retired",
        },
        expect_progress_retired=True,
    ),
    SurfaceParityCase(
        case_id="request_return_after_terminal",
        runtime_scenario="official_request_return_after_terminal",
        prompt="echo hello world",
        expected_status="ok",
        expected_completion_source="prompt_return",
        expected_reply_substrings={
            "discord": "fixture reply",
            "telegram": "fixture reply",
        },
        expected_progress_state={
            "discord": "done",
            "telegram": "retired",
        },
        expect_progress_retired=True,
    ),
    SurfaceParityCase(
        case_id="assistant_text_without_request_return",
        runtime_scenario="official_prompt_hang",
        prompt="echo hello world",
        expected_status="error",
        expected_completion_source="timeout",
        expected_reply_substrings={
            "discord": "timed out",
            "telegram": "timed out",
        },
        expected_progress_state={
            "discord": "failed",
            "telegram": "retired",
        },
        expect_progress_retired=True,
        harness_timeout_seconds=8.0,
        use_short_runtime_timeout=True,
    ),
    SurfaceParityCase(
        case_id="interrupted_before_return",
        runtime_scenario="official_cancelled_before_return",
        prompt="echo hello world",
        expected_status="interrupted",
        expected_completion_source="interrupt",
        expected_reply_substrings={
            "discord": "Discord PMA turn interrupted",
            "telegram": "Telegram PMA turn interrupted",
        },
        expected_detail_substrings={
            "discord": "Discord PMA turn interrupted",
            "telegram": "Telegram PMA turn interrupted",
        },
        expected_progress_state={
            "discord": "failed",
            "telegram": "retired",
        },
        expect_progress_retired=True,
    ),
    SurfaceParityCase(
        case_id="failed_before_return",
        runtime_scenario="official_failed_before_return",
        prompt="echo hello world",
        expected_status="error",
        expected_completion_source="prompt_return",
        expected_reply_substrings={
            "discord": "permission denied",
            "telegram": "permission denied",
        },
        expected_detail_substrings={
            "discord": "permission denied",
            "telegram": "permission denied",
        },
        expected_progress_state={
            "discord": "failed",
            "telegram": "retired",
        },
        expect_progress_retired=True,
    ),
    SurfaceParityCase(
        case_id="delivery_cleanup_failure_after_completion",
        runtime_scenario="official",
        prompt="echo hello world",
        expected_status="ok",
        expected_completion_source="prompt_return",
        expected_reply_substrings={
            "discord": "fixture reply",
            "telegram": "fixture reply",
        },
        expected_progress_state={
            "discord": "done",
            "telegram": "visible",
        },
        expect_progress_retired=False,
        fail_progress_delete=True,
    ),
    SurfaceParityCase(
        case_id="permission_request_flow",
        runtime_scenario="official",
        prompt="needs permission",
        expected_status="ok",
        expected_completion_source="prompt_return",
        expected_reply_substrings={
            "discord": "fixture reply",
            "telegram": "fixture reply",
        },
        expected_progress_state={
            "discord": "done",
            "telegram": "retired",
        },
        expect_progress_retired=True,
        harness_timeout_seconds=8.0,
        approval_mode="yolo",
    ),
)


def _latest_log_record(
    log_records: list[dict[str, Any]], event_name: str
) -> dict[str, Any]:
    for record in reversed(log_records):
        if record.get("event") == event_name:
            return record
    raise AssertionError(f"missing log event: {event_name}")


def _build_discord_snapshot(rest: FakeDiscordRest) -> SurfaceSnapshot:
    finalized = _latest_log_record(
        rest.log_records, "chat.managed_thread.turn_finalized"
    )
    final_message = ""
    for op in reversed(rest.message_ops):
        if op["op"] != "send":
            continue
        message_id = str(op.get("message_id") or "")
        if message_id == str(rest.preview_message_id or ""):
            continue
        final_message = str(op["payload"].get("content", ""))
        if final_message:
            break
    progress_state = rest.terminal_progress_label or (
        "retired" if rest.preview_deleted else "visible"
    )
    return SurfaceSnapshot(
        surface="discord",
        terminal_status=str(finalized.get("status") or rest.execution_status or ""),
        execution_status=str(rest.execution_status or ""),
        completion_source=_normalize_optional_text(finalized.get("completion_source")),
        final_user_visible_message=final_message,
        progress_retired=bool(rest.preview_deleted),
        progress_state=progress_state,
        thread_target_id=rest.thread_target_id,
        execution_id=rest.execution_id,
        detail=_normalize_optional_text(
            finalized.get("detail") or rest.execution_error
        ),
    )


def _build_telegram_snapshot(bot: FakeTelegramBot) -> SurfaceSnapshot:
    finalized = _latest_log_record(
        bot.log_records, "chat.managed_thread.turn_finalized"
    )
    final_message = ""
    for item in reversed(bot.messages[1:]):
        text = str(item.get("text") or "")
        if text:
            final_message = text
            break
    progress_state = "retired" if bot.placeholder_deleted else "visible"
    return SurfaceSnapshot(
        surface="telegram",
        terminal_status=str(finalized.get("status") or bot.execution_status or ""),
        execution_status=str(bot.execution_status or ""),
        completion_source=_normalize_optional_text(finalized.get("completion_source")),
        final_user_visible_message=final_message,
        progress_retired=bool(bot.placeholder_deleted),
        progress_state=progress_state,
        thread_target_id=bot.thread_target_id,
        execution_id=bot.execution_id,
        detail=_normalize_optional_text(finalized.get("detail") or bot.execution_error),
    )


def _normalize_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


async def _run_discord_case(
    case: SurfaceParityCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SurfaceSnapshot:
    runtime = HermesFixtureRuntime(case.runtime_scenario)
    patch_hermes_runtime(monkeypatch, runtime)
    if case.use_short_runtime_timeout:
        monkeypatch.setattr(discord_message_turns, "DISCORD_PMA_TIMEOUT_SECONDS", 0.05)
    harness = DiscordSurfaceHarness(
        tmp_path / f"{case.case_id}-discord",
        timeout_seconds=case.harness_timeout_seconds,
    )
    await harness.setup(agent="hermes", approval_mode=case.approval_mode)
    try:
        rest = await harness.run_message(
            case.prompt,
            rest_client=(
                FakeDiscordRest(fail_delete_message_ids={"msg-1"})
                if case.fail_progress_delete
                else None
            ),
        )
        return _build_discord_snapshot(rest)
    finally:
        await harness.close()
        await runtime.close()


async def _run_telegram_case(
    case: SurfaceParityCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SurfaceSnapshot:
    runtime = HermesFixtureRuntime(case.runtime_scenario)
    patch_hermes_runtime(monkeypatch, runtime)
    if case.use_short_runtime_timeout:
        monkeypatch.setattr(
            telegram_execution,
            "TELEGRAM_PMA_TIMEOUT_SECONDS",
            0.05,
        )
    harness = TelegramSurfaceHarness(
        tmp_path / f"{case.case_id}-telegram",
        timeout_seconds=case.harness_timeout_seconds,
    )
    await harness.setup(agent="hermes", approval_mode=case.approval_mode)
    try:
        bot = await harness.run_message(
            case.prompt,
            bot_client=(
                FakeTelegramBot(fail_delete_message_ids={1})
                if case.fail_progress_delete
                else None
            ),
        )
        return _build_telegram_snapshot(bot)
    finally:
        await harness.close()
        await runtime.close()


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
async def test_hermes_official_surface_parity_matrix(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    case: SurfaceParityCase,
) -> None:
    discord = await _run_discord_case(case, tmp_path, monkeypatch)
    telegram = await _run_telegram_case(case, tmp_path, monkeypatch)
    snapshots = {"discord": discord, "telegram": telegram}

    for surface_name, snapshot in snapshots.items():
        assert snapshot.terminal_status == case.expected_status, (
            case.case_id,
            surface_name,
            snapshot,
        )
        assert snapshot.execution_status == case.expected_status, (
            case.case_id,
            surface_name,
            snapshot,
        )
        assert snapshot.completion_source == case.expected_completion_source, (
            case.case_id,
            surface_name,
            snapshot,
        )
        assert (
            case.expected_reply_substrings[surface_name]
            in snapshot.final_user_visible_message
        ), (
            case.case_id,
            surface_name,
            snapshot,
        )
        if case.expected_detail_substrings is not None:
            assert snapshot.detail is not None, (case.case_id, surface_name, snapshot)
            assert case.expected_detail_substrings[surface_name] in snapshot.detail, (
                case.case_id,
                surface_name,
                snapshot,
            )
        assert snapshot.progress_retired is case.expect_progress_retired, (
            case.case_id,
            surface_name,
            snapshot,
        )
        assert snapshot.progress_state == case.expected_progress_state[surface_name], (
            case.case_id,
            surface_name,
            snapshot,
        )
        assert snapshot.thread_target_id, (case.case_id, surface_name, snapshot)
        assert snapshot.execution_id, (case.case_id, surface_name, snapshot)

    assert discord.terminal_status == telegram.terminal_status, (
        case.case_id,
        snapshots,
    )
    assert discord.completion_source == telegram.completion_source, (
        case.case_id,
        snapshots,
    )


@pytest.mark.anyio
async def test_hermes_official_surface_parity_interrupt_and_reuse(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_discord_flow() -> tuple[SurfaceSnapshot, SurfaceSnapshot]:
        runtime = HermesFixtureRuntime("official")
        patch_hermes_runtime(monkeypatch, runtime)
        harness = DiscordSurfaceHarness(
            tmp_path / "interrupt-reuse-discord",
            timeout_seconds=8.0,
        )
        await harness.setup(agent="hermes", approval_mode="yolo")
        try:
            first_task = harness.start_message("cancel me")
            first_thread_target_id, _execution_id = (
                await harness.wait_for_running_execution(timeout_seconds=2.0)
            )
            stop_outcome = await harness.orchestration_service().stop_thread(
                first_thread_target_id
            )
            first_result = await first_task
            first_snapshot = _build_discord_snapshot(first_result)

            second_result = await harness.run_message("echo after interrupt")
            second_snapshot = _build_discord_snapshot(second_result)

            assert bool(getattr(stop_outcome, "interrupted_active", False)) is True
            assert first_snapshot.thread_target_id == first_thread_target_id
            return first_snapshot, second_snapshot
        finally:
            await harness.close()
            await runtime.close()

    async def run_telegram_flow() -> tuple[SurfaceSnapshot, SurfaceSnapshot]:
        runtime = HermesFixtureRuntime("official")
        patch_hermes_runtime(monkeypatch, runtime)
        harness = TelegramSurfaceHarness(
            tmp_path / "interrupt-reuse-telegram",
            timeout_seconds=8.0,
        )
        await harness.setup(agent="hermes", approval_mode="yolo")
        try:
            first_task = harness.start_message("cancel me")
            first_thread_target_id, _execution_id = (
                await harness.wait_for_running_execution(timeout_seconds=2.0)
            )
            stop_outcome = await harness.orchestration_service().stop_thread(
                first_thread_target_id
            )
            first_result = await first_task
            first_snapshot = _build_telegram_snapshot(first_result)

            second_result = await harness.run_message("echo after interrupt")
            second_snapshot = _build_telegram_snapshot(second_result)

            assert bool(getattr(stop_outcome, "interrupted_active", False)) is True
            assert first_snapshot.thread_target_id == first_thread_target_id
            return first_snapshot, second_snapshot
        finally:
            await harness.close()
            await runtime.close()

    discord_first, discord_second = await run_discord_flow()
    telegram_first, telegram_second = await run_telegram_flow()

    for snapshot in (discord_first, telegram_first):
        assert snapshot.terminal_status == "interrupted", snapshot
        assert snapshot.execution_status == "interrupted", snapshot
        assert snapshot.completion_source == "interrupt", snapshot
        assert snapshot.progress_retired is True, snapshot

    for snapshot in (discord_second, telegram_second):
        assert snapshot.terminal_status == "ok", snapshot
        assert snapshot.execution_status == "ok", snapshot
        assert snapshot.completion_source == "prompt_return", snapshot
        assert "fixture reply" in snapshot.final_user_visible_message, snapshot
        assert snapshot.progress_retired is True, snapshot

    assert discord_first.thread_target_id == discord_second.thread_target_id
    assert telegram_first.thread_target_id == telegram_second.thread_target_id
