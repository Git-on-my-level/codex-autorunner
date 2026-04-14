from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.config_contract import ConfigError
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
                "car_service_count": 0,
                "managed_runtime_count": offset,
                "opencode_count": offset,
                "codex_app_server_count": 0,
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
            "car_service_count": 1,
            "managed_runtime_count": 99,
            "opencode_count": 99,
            "codex_app_server_count": 0,
            "total_count": 100,
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
                "car_service_count": 3,
                "managed_runtime_count": 3,
                "opencode_count": 2,
                "codex_app_server_count": 1,
                "total_count": 6,
            },
            cadence_seconds=120,
            window_seconds=3 * 60 * 60,
        )

    store.record_sample(
        {
            "captured_at": _iso(now + timedelta(minutes=12)),
            "car_service_count": 3,
            "managed_runtime_count": 16,
            "opencode_count": 15,
            "codex_app_server_count": 1,
            "total_count": 19,
        },
        cadence_seconds=120,
        window_seconds=3 * 60 * 60,
    )

    summary = build_process_monitor_summary(root, capture_if_stale=False)

    assert summary["status"] == "warning"
    assert summary["metrics"]["car_services"]["current"] == 3
    managed = summary["metrics"]["managed_runtimes"]
    assert managed["abnormal"] is True
    assert managed["current"] == 16
    opencode = summary["metrics"]["opencode"]
    assert opencode["abnormal"] is True
    assert opencode["current"] == 15
    assert opencode["average"] > 3.0
    assert opencode["p95"] == 15
    assert "absolute alert floor" in (opencode["reason"] or "")


def test_build_process_monitor_summary_skips_lifecycle_when_repo_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    def _fake_collect_processes() -> SimpleNamespace:
        return SimpleNamespace(
            car_service_processes=[],
            opencode_processes=[],
            app_server_processes=[],
            car_service_count=0,
            managed_runtime_count=0,
            opencode_count=0,
            app_server_count=0,
            codex_app_server_count=0,
            total_count=0,
        )

    monkeypatch.setattr(
        "codex_autorunner.core.diagnostics.process_monitor.collect_processes",
        _fake_collect_processes,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.diagnostics.process_monitor.enrich_with_ownership",
        lambda snapshot, _root: snapshot,
    )

    def _raise_missing_config(_root: Path) -> None:
        raise ConfigError("missing hub config")

    monkeypatch.setattr(
        "codex_autorunner.core.diagnostics.process_monitor.summarize_opencode_lifecycle",
        _raise_missing_config,
    )

    summary = build_process_monitor_summary(root)

    assert summary["status"] == "ok"
    assert summary["latest"]["opencode_lifecycle"] == {}


def test_process_monitor_store_discards_prior_schema_version(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    diagnostics = root / ".codex-autorunner" / "diagnostics"
    diagnostics.mkdir(parents=True)
    (diagnostics / "process-monitor.json").write_text(
        '{"version": 1, "samples": [{"captured_at": "2026-01-01T00:00:00Z", "total_count": 5}]}',
        encoding="utf-8",
    )

    store = ProcessMonitorStore(root)

    payload = store.load()

    assert payload["version"] == 2
    assert payload["samples"] == []
