from __future__ import annotations

import gzip
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_autorunner.core.config import (
    FlowRetentionConfig,
    parse_flow_retention_config,
)
from codex_autorunner.core.flows.flow_housekeeping import (
    DEFAULT_RETENTION_DAYS,
    _retention_cutoff,
    build_plan,
    execute_housekeep,
    gather_stats,
)
from codex_autorunner.core.flows.models import FlowEventType, FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def _make_store(temp_dir: Path) -> FlowStore:
    store = FlowStore(temp_dir / "flows.db")
    store.initialize()
    return store


def _create_terminal_run(
    store: FlowStore,
    run_id: str = "run-terminal-1",
    status: FlowRunStatus = FlowRunStatus.COMPLETED,
    finished_at: str = "2020-01-01T00:00:00Z",
) -> str:
    record = store.create_flow_run(
        run_id=run_id, flow_type="ticket_flow", input_data={}
    )
    store.update_flow_run_status(record.id, status=status, finished_at=finished_at)
    return record.id


def _create_active_run(store: FlowStore, run_id: str = "run-active-1") -> str:
    record = store.create_flow_run(
        run_id=run_id, flow_type="ticket_flow", input_data={}
    )
    store.update_flow_run_status(
        record.id, status=FlowRunStatus.RUNNING, started_at="2025-01-01T00:00:00Z"
    )
    return record.id


def _add_event(
    store: FlowStore,
    run_id: str,
    event_type: FlowEventType,
    data: dict,
    event_id: str | None = None,
) -> None:
    store.create_event(
        event_id=event_id or f"evt-{event_type.value}-{data.get('turn_id', 't1')}",
        run_id=run_id,
        event_type=event_type,
        data=data,
    )


def _add_telemetry(
    store: FlowStore,
    run_id: str,
    event_type: FlowEventType,
    data: dict,
    telemetry_id: str | None = None,
) -> None:
    store.create_telemetry(
        telemetry_id=telemetry_id
        or f"tel-{event_type.value}-{data.get('turn_id', 't1')}",
        run_id=run_id,
        event_type=event_type,
        data=data,
    )


class TestFlowRetentionConfig:
    def test_defaults(self):
        config = FlowRetentionConfig()
        assert config.retention_days == DEFAULT_RETENTION_DAYS
        assert config.retention_days == 7
        assert config.vacuum_after_prune is False

    def test_custom(self):
        config = FlowRetentionConfig(retention_days=14, vacuum_after_prune=True)
        assert config.retention_days == 14
        assert config.vacuum_after_prune is True

    def test_retention_cutoff(self):
        config = FlowRetentionConfig(retention_days=7)
        cutoff = _retention_cutoff(config)
        expected = datetime.now(timezone.utc) - timedelta(days=7)
        delta = abs((cutoff - expected).total_seconds())
        assert delta < 2

    def test_parse_none(self):
        config = parse_flow_retention_config(None)
        assert config.retention_days == 7

    def test_parse_empty_dict(self):
        config = parse_flow_retention_config({})
        assert config.retention_days == 7

    def test_parse_custom(self):
        config = parse_flow_retention_config(
            {"retention_days": 30, "vacuum_after_prune": True}
        )
        assert config.retention_days == 30
        assert config.vacuum_after_prune is True


class TestGatherStats:
    def test_empty_db(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        config = FlowRetentionConfig()
        stats = gather_stats(store, db_path, config)
        assert stats.runs_total == 0
        assert stats.events_total == 0
        assert stats.db_size_bytes > 0

    def test_mixed_runs(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        _create_active_run(store, "run-active")
        _create_terminal_run(store, "run-terminal", finished_at="2020-01-01T00:00:00Z")

        config = FlowRetentionConfig(retention_days=7)
        stats = gather_stats(store, db_path, config)
        assert stats.runs_total == 2
        assert stats.runs_active == 1
        assert stats.runs_terminal == 1
        assert stats.runs_expired == 1

    def test_counts_events(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        run_id = _create_terminal_run(store)
        _add_event(
            store,
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            {
                "message": {"method": "message.part.updated", "params": {}},
                "turn_id": "t1",
            },
            event_id="evt-1",
        )
        _add_event(
            store,
            run_id,
            FlowEventType.AGENT_STREAM_DELTA,
            {"delta": "text", "turn_id": "t2"},
            event_id="evt-2",
        )
        _add_telemetry(
            store,
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            {
                "message": {"method": "message.part.updated", "params": {}},
                "turn_id": "t3",
            },
            telemetry_id="tel-1",
        )

        config = FlowRetentionConfig()
        stats = gather_stats(store, db_path, config)
        assert len(stats.run_details) == 1
        r = stats.run_details[0]
        assert r.events_total == 2
        assert r.telemetry_total == 1
        assert r.wire_events == 3


class TestBuildPlan:
    def test_plan_skips_active(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        _create_active_run(store, "run-active")
        config = FlowRetentionConfig(retention_days=7)
        plan = build_plan(store, db_path, config)
        assert plan.runs_skipped_active == 1
        assert len(plan.runs_to_process) == 0

    def test_plan_skips_recent_terminal(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _create_terminal_run(store, "run-recent", finished_at=recent)
        config = FlowRetentionConfig(retention_days=7)
        plan = build_plan(store, db_path, config)
        assert plan.runs_skipped_not_expired == 1
        assert len(plan.runs_to_process) == 0

    def test_plan_includes_expired_terminal(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        _create_terminal_run(store, "run-old", finished_at="2020-01-01T00:00:00Z")
        _add_event(
            store,
            "run-old",
            FlowEventType.APP_SERVER_EVENT,
            {
                "message": {"method": "message.part.updated", "params": {}},
                "turn_id": "t1",
            },
            event_id="evt-old-1",
        )
        config = FlowRetentionConfig(retention_days=7)
        plan = build_plan(store, db_path, config)
        assert len(plan.runs_to_process) == 1
        assert plan.runs_to_process[0].run_id == "run-old"

    def test_plan_filters_by_run_ids(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        _create_terminal_run(store, "run-a", finished_at="2020-01-01T00:00:00Z")
        _create_terminal_run(store, "run-b", finished_at="2020-01-01T00:00:00Z")
        config = FlowRetentionConfig(retention_days=7)
        plan = build_plan(store, db_path, config, run_ids=["run-a"])
        assert len(plan.runs_to_process) == 1
        assert plan.runs_to_process[0].run_id == "run-a"


class TestExecuteHousekeep:
    def test_dry_run_no_mutation(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        run_id = _create_terminal_run(
            store, "run-old", finished_at="2020-01-01T00:00:00Z"
        )
        _add_event(
            store,
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            {
                "message": {"method": "message.part.updated", "params": {}},
                "turn_id": "t1",
            },
            event_id="evt-old-1",
        )
        config = FlowRetentionConfig(retention_days=7)
        result = execute_housekeep(temp_dir, store, db_path, config, dry_run=True)
        assert result.runs_processed == 0
        assert result.db_size_before_bytes == result.db_size_after_bytes
        events_after = store.get_events(run_id)
        assert len(events_after) == 1

    def test_execute_exports_and_prunes(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        run_id = _create_terminal_run(
            store, "run-old", finished_at="2020-01-01T00:00:00Z"
        )
        _add_event(
            store,
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            {
                "message": {"method": "message.part.updated", "params": {}},
                "turn_id": "t1",
            },
            event_id="evt-old-1",
        )
        _add_event(
            store,
            run_id,
            FlowEventType.AGENT_STREAM_DELTA,
            {"delta": "chunk", "turn_id": "t1"},
            event_id="evt-delta-1",
        )
        config = FlowRetentionConfig(retention_days=7)
        result = execute_housekeep(temp_dir, store, db_path, config, dry_run=False)
        assert result.runs_processed == 1
        assert result.events_exported == 2
        assert result.events_pruned == 2
        assert len(result.archive_files) == 1
        assert result.errors == []
        assert result.db_size_after_bytes > 0

        archive_path = Path(result.archive_files[0])
        assert archive_path.exists()
        with gzip.open(archive_path, "rb") as f:
            lines = f.read().decode("utf-8").strip().split("\n")
        assert len(lines) == 2

    def test_execute_skips_active(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        _create_active_run(store, "run-active")
        _add_event(
            store,
            "run-active",
            FlowEventType.APP_SERVER_EVENT,
            {
                "message": {"method": "message.part.updated", "params": {}},
                "turn_id": "t1",
            },
            event_id="evt-active-1",
        )
        config = FlowRetentionConfig(retention_days=7)
        result = execute_housekeep(temp_dir, store, db_path, config, dry_run=False)
        assert result.runs_processed == 0
        assert result.events_exported == 0

    def test_execute_with_vacuum(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        run_id = _create_terminal_run(
            store, "run-old", finished_at="2020-01-01T00:00:00Z"
        )
        for i in range(50):
            _add_event(
                store,
                run_id,
                FlowEventType.APP_SERVER_EVENT,
                {
                    "message": {"method": "message.part.updated", "params": {}},
                    "turn_id": f"t{i}",
                },
                event_id=f"evt-old-{i}",
            )
        config = FlowRetentionConfig(retention_days=7)
        result = execute_housekeep(
            temp_dir, store, db_path, config, dry_run=False, vacuum=True
        )
        assert result.vacuum_performed is True
        assert result.runs_processed == 1

    def test_execute_specific_run_id(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        _create_terminal_run(store, "run-a", finished_at="2020-01-01T00:00:00Z")
        _create_terminal_run(store, "run-b", finished_at="2020-01-01T00:00:00Z")
        _add_event(
            store,
            "run-a",
            FlowEventType.APP_SERVER_EVENT,
            {
                "message": {"method": "message.part.updated", "params": {}},
                "turn_id": "t1",
            },
            event_id="evt-a-1",
        )
        _add_event(
            store,
            "run-b",
            FlowEventType.APP_SERVER_EVENT,
            {
                "message": {"method": "message.part.updated", "params": {}},
                "turn_id": "t1",
            },
            event_id="evt-b-1",
        )
        config = FlowRetentionConfig(retention_days=7)
        result = execute_housekeep(temp_dir, store, db_path, config, run_ids=["run-a"])
        assert result.runs_processed == 1

    def test_to_dict(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        run_id = _create_terminal_run(
            store, "run-old", finished_at="2020-01-01T00:00:00Z"
        )
        _add_event(
            store,
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            {
                "message": {"method": "message.part.updated", "params": {}},
                "turn_id": "t1",
            },
            event_id="evt-1",
        )
        config = FlowRetentionConfig(retention_days=7)
        result = execute_housekeep(temp_dir, store, db_path, config, dry_run=False)
        d = result.to_dict()
        assert "runs_processed" in d
        assert "events_exported" in d
        assert "errors" in d
        assert "db_size_before_bytes" in d
        assert "db_size_after_bytes" in d
        assert "run_details" in d
        assert len(d["run_details"]) == 1
        assert d["run_details"][0]["run_id"] == "run-old"

    def test_result_empty_db(self, temp_dir):
        store = _make_store(temp_dir)
        db_path = temp_dir / "flows.db"
        config = FlowRetentionConfig(retention_days=7)
        result = execute_housekeep(temp_dir, store, db_path, config, dry_run=False)
        assert result.runs_processed == 0
        assert result.events_exported == 0
        assert result.errors == []
