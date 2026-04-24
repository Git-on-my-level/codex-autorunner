from __future__ import annotations

from datetime import datetime, timedelta, timezone

from codex_autorunner.integrations.github.polling_discovery import (
    is_recent_terminal_thread_candidate,
    thread_has_pr_open_hint,
)


def _parse_optional_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def test_is_recent_terminal_thread_candidate_reads_status_reason_code() -> None:
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=1)
    thread = {
        "status_terminal": True,
        "status_updated_at": now.isoformat().replace("+00:00", "Z"),
        "status_reason_code": "managed_turn_completed",
        "metadata": {"head_branch": "feature/foo"},
    }
    assert is_recent_terminal_thread_candidate(
        thread, cutoff=cutoff, parse_optional_iso=_parse_optional_iso
    )


def test_is_recent_terminal_thread_candidate_legacy_status_reason_fallback() -> None:
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=1)
    thread = {
        "status_terminal": True,
        "status_updated_at": now.isoformat().replace("+00:00", "Z"),
        "status_reason": "managed_turn_completed",
        "metadata": {"head_branch": "feature/foo"},
    }
    assert is_recent_terminal_thread_candidate(
        thread, cutoff=cutoff, parse_optional_iso=_parse_optional_iso
    )


def test_thread_has_pr_open_hint_scans_status_reason_code() -> None:
    thread = {
        "status_reason_code": "see https://github.com/org/repo/pull/42 for details",
    }
    assert thread_has_pr_open_hint(thread) is True
