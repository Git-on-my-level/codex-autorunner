from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_autorunner.core.diagnostics.process_monitor import (
    ProcessMonitorStore,
    build_process_monitor_summary,
)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_process_monitor_store_keeps_recent_tail(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    store = ProcessMonitorStore(root)
    now = datetime.now(timezone.utc)

    for offset in range(8, 0, -1):
        captured_at = now - timedelta(minutes=offset * 10)
        store.record_sample(
            {
                "captured_at": _iso(captured_at),
                "opencode_count": offset,
                "app_server_count": 0,
                "total_count": offset,
            },
            cadence_seconds=120,
            window_seconds=3 * 60 * 60,
        )

    samples = store.recent_samples(window_seconds=3 * 60 * 60)

    assert len(samples) == 8

    store.record_sample(
        {
            "captured_at": _iso(now + timedelta(minutes=1)),
            "opencode_count": 99,
            "app_server_count": 0,
            "total_count": 99,
        },
        cadence_seconds=120,
        window_seconds=30 * 60,
    )

    trimmed = store.recent_samples(window_seconds=30 * 60)

    assert len(trimmed) == 3
    assert [entry["opencode_count"] for entry in trimmed] == [2, 1, 99]


def test_build_process_monitor_summary_flags_high_outlier(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    store = ProcessMonitorStore(root)
    now = datetime.now(timezone.utc) - timedelta(minutes=12)

    for step in range(6):
        captured_at = now + timedelta(minutes=step * 2)
        store.record_sample(
            {
                "captured_at": _iso(captured_at),
                "opencode_count": 2,
                "app_server_count": 1,
                "total_count": 3,
            },
            cadence_seconds=120,
            window_seconds=3 * 60 * 60,
        )

    store.record_sample(
        {
            "captured_at": _iso(now + timedelta(minutes=12)),
            "opencode_count": 15,
            "app_server_count": 1,
            "total_count": 16,
        },
        cadence_seconds=120,
        window_seconds=3 * 60 * 60,
    )

    summary = build_process_monitor_summary(root, capture_if_stale=False)

    assert summary["status"] == "warning"
    opencode = summary["metrics"]["opencode"]
    assert opencode["abnormal"] is True
    assert opencode["current"] == 15
    assert opencode["average"] > 3.0
    assert opencode["p95"] == 15
    assert "absolute alert floor" in (opencode["reason"] or "")
