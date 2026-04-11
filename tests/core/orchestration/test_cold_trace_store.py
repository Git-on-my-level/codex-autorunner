from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from codex_autorunner.core.orchestration.cold_trace_store import (
    ColdTraceReader,
    ColdTraceStore,
    ColdTraceWriter,
)
from codex_autorunner.core.orchestration.execution_history import (
    ExecutionCheckpoint,
    ExecutionCheckpointSignal,
    ExecutionTraceManifest,
)
from codex_autorunner.core.state_roots import resolve_hub_traces_root


@pytest.fixture()
def store(tmp_path: Path) -> ColdTraceStore:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    (hub_root / ".codex-autorunner").mkdir()
    return ColdTraceStore(hub_root)


@pytest.fixture()
def hub_root(tmp_path: Path) -> Path:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    (hub_root / ".codex-autorunner").mkdir()
    return hub_root


def _init_orchestration_db(hub_root: Path) -> None:
    from codex_autorunner.core.orchestration.sqlite import (
        initialize_orchestration_sqlite,
    )

    initialize_orchestration_sqlite(hub_root)


class TestColdTraceWriter:
    def test_append_and_finalize(self, hub_root: Path) -> None:
        _init_orchestration_db(hub_root)
        execution_id = "exec-001"

        with ColdTraceWriter(
            hub_root=hub_root,
            execution_id=execution_id,
        ) as writer:
            seq1 = writer.append(
                event_family="tool_call",
                event_type="ToolCall",
                payload={"tool_name": "shell", "tool_input": {"cmd": "ls"}},
            )
            assert seq1 == 1
            seq2 = writer.append(
                event_family="output_delta",
                event_type="OutputDelta",
                payload={"content": "thinking", "delta_type": "assistant_stream"},
            )
            assert seq2 == 2
            manifest = writer.finalize()

        assert manifest.execution_id == execution_id
        assert manifest.event_count == 2
        assert manifest.status == "finalized"
        assert manifest.trace_format == "gzipped_jsonl"
        assert manifest.schema_version == 1
        assert "tool_call" in manifest.includes_families
        assert "output_delta" in manifest.includes_families
        assert manifest.byte_count > 0
        assert manifest.checksum.startswith("sha256:")

        traces_root = resolve_hub_traces_root(hub_root)
        artifact_path = traces_root / manifest.artifact_relpath
        assert artifact_path.exists()

    def test_append_requires_open(self, hub_root: Path) -> None:
        writer = ColdTraceWriter(hub_root=hub_root, execution_id="exec-x")
        with pytest.raises(RuntimeError, match="not open"):
            writer.append(
                event_family="tool_call",
                event_type="ToolCall",
                payload={},
            )

    def test_context_manager_creates_gzipped_jsonl(self, hub_root: Path) -> None:
        _init_orchestration_db(hub_root)
        with ColdTraceWriter(hub_root=hub_root, execution_id="exec-ctx") as writer:
            writer.append(
                event_family="tool_call",
                event_type="ToolCall",
                payload={"tool_name": "read"},
            )
            manifest = writer.finalize()

        traces_root = resolve_hub_traces_root(hub_root)
        artifact_path = traces_root / manifest.artifact_relpath
        with gzip.open(artifact_path, "rt", encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["seq"] == 1
        assert event["event_family"] == "tool_call"
        assert event["payload"]["tool_name"] == "read"

    def test_trace_id_is_stable(self, hub_root: Path) -> None:
        _init_orchestration_db(hub_root)
        with ColdTraceWriter(
            hub_root=hub_root,
            execution_id="exec-stable",
            trace_id="custom-trace-123",
        ) as writer:
            assert writer.trace_id == "custom-trace-123"
            writer.append(
                event_family="run_notice",
                event_type="RunNotice",
                payload={"kind": "info", "message": "started"},
            )
            manifest = writer.finalize()
        assert manifest.trace_id == "custom-trace-123"


class TestColdTraceReader:
    def test_read_events_with_offset_and_limit(self, hub_root: Path) -> None:
        _init_orchestration_db(hub_root)
        with ColdTraceWriter(hub_root=hub_root, execution_id="exec-read") as writer:
            for i in range(5):
                writer.append(
                    event_family="output_delta",
                    event_type="OutputDelta",
                    payload={"content": f"chunk-{i}", "delta_type": "assistant_stream"},
                )
            manifest = writer.finalize()

        events = ColdTraceReader.read_events(hub_root, manifest, offset=2, limit=2)
        assert len(events) == 2
        assert events[0]["seq"] == 3
        assert events[1]["seq"] == 4

    def test_read_events_all(self, hub_root: Path) -> None:
        _init_orchestration_db(hub_root)
        with ColdTraceWriter(hub_root=hub_root, execution_id="exec-readall") as writer:
            for i in range(3):
                writer.append(
                    event_family="output_delta",
                    event_type="OutputDelta",
                    payload={"content": f"chunk-{i}"},
                )
            manifest = writer.finalize()

        events = ColdTraceReader.read_events(hub_root, manifest)
        assert len(events) == 3

    def test_read_events_missing_file(self, hub_root: Path) -> None:
        manifest = ExecutionTraceManifest(
            trace_id="ghost",
            execution_id="exec-ghost",
            artifact_relpath="nonexistent/file.trace.jsonl.gz",
            trace_format="gzipped_jsonl",
            event_count=0,
            status="finalized",
        )
        events = ColdTraceReader.read_events(hub_root, manifest)
        assert events == []

    def test_iter_events(self, hub_root: Path) -> None:
        _init_orchestration_db(hub_root)
        with ColdTraceWriter(hub_root=hub_root, execution_id="exec-iter") as writer:
            for i in range(3):
                writer.append(
                    event_family="output_delta",
                    event_type="OutputDelta",
                    payload={"content": f"chunk-{i}"},
                )
            manifest = writer.finalize()

        events = list(ColdTraceReader.iter_events(hub_root, manifest))
        assert len(events) == 3
        assert events[0]["seq"] == 1
        assert events[2]["seq"] == 3


class TestColdTraceStore:
    def test_write_read_roundtrip(self, store: ColdTraceStore) -> None:
        _init_orchestration_db(store._hub_root)
        execution_id = "exec-roundtrip"

        with store.open_writer(execution_id=execution_id) as writer:
            writer.append(
                event_family="tool_call",
                event_type="ToolCall",
                payload={"tool_name": "bash"},
            )
            writer.append(
                event_family="tool_result",
                event_type="ToolResult",
                payload={"tool_name": "bash", "status": "ok"},
            )
            writer.finalize()

        events = store.read_events(execution_id)
        assert len(events) == 2
        assert events[0]["event_family"] == "tool_call"
        assert events[1]["event_family"] == "tool_result"

    def test_get_manifest(self, store: ColdTraceStore) -> None:
        _init_orchestration_db(store._hub_root)
        with store.open_writer(execution_id="exec-manifest") as writer:
            writer.append(
                event_family="output_delta",
                event_type="OutputDelta",
                payload={"content": "hello"},
            )
            writer.finalize()

        manifest = store.get_manifest("exec-manifest")
        assert manifest is not None
        assert manifest.execution_id == "exec-manifest"
        assert manifest.event_count == 1
        assert manifest.status == "finalized"

    def test_get_manifest_missing(self, store: ColdTraceStore) -> None:
        _init_orchestration_db(store._hub_root)
        assert store.get_manifest("no-such-exec") is None

    def test_list_manifests(self, store: ColdTraceStore) -> None:
        _init_orchestration_db(store._hub_root)
        for exec_id in ("exec-a", "exec-b", "exec-c"):
            with store.open_writer(execution_id=exec_id) as writer:
                writer.append(
                    event_family="output_delta",
                    event_type="OutputDelta",
                    payload={"content": exec_id},
                )
                writer.finalize()

        all_manifests = store.list_manifests()
        assert len(all_manifests) == 3

        finalized = store.list_manifests(status="finalized")
        assert len(finalized) == 3

    def test_archive_trace(self, store: ColdTraceStore) -> None:
        _init_orchestration_db(store._hub_root)
        with store.open_writer(
            execution_id="exec-archive", trace_id="trace-archive-1"
        ) as writer:
            writer.append(
                event_family="output_delta",
                event_type="OutputDelta",
                payload={"content": "data"},
            )
            writer.finalize()

        archived = store.archive_trace("trace-archive-1")
        assert archived is not None
        assert archived.status == "archived"

        manifest_by_trace = store.get_manifest_by_trace_id("trace-archive-1")
        assert manifest_by_trace is not None
        assert manifest_by_trace.status == "archived"


class TestExecutionCheckpoint:
    def test_save_and_load_checkpoint(self, store: ColdTraceStore) -> None:
        _init_orchestration_db(store._hub_root)
        checkpoint = ExecutionCheckpoint(
            status="running",
            execution_id="exec-chk",
            thread_target_id="thread-1",
            assistant_text_preview="hello world",
            assistant_char_count=11,
            raw_event_count=5,
            projection_event_cursor=3,
        )
        store.save_checkpoint(checkpoint)

        loaded = store.load_checkpoint("exec-chk")
        assert loaded is not None
        assert loaded.status == "running"
        assert loaded.execution_id == "exec-chk"
        assert loaded.thread_target_id == "thread-1"
        assert loaded.assistant_text_preview == "hello world"
        assert loaded.assistant_char_count == 11
        assert loaded.raw_event_count == 5
        assert loaded.projection_event_cursor == 3

    def test_save_checkpoint_requires_execution_id(self, store: ColdTraceStore) -> None:
        checkpoint = ExecutionCheckpoint(status="running")
        with pytest.raises(ValueError, match="execution_id"):
            store.save_checkpoint(checkpoint)

    def test_update_checkpoint_upserts(self, store: ColdTraceStore) -> None:
        _init_orchestration_db(store._hub_root)
        cp1 = ExecutionCheckpoint(
            status="running",
            execution_id="exec-upsert",
            raw_event_count=10,
        )
        store.save_checkpoint(cp1)

        cp2 = ExecutionCheckpoint(
            status="completed",
            execution_id="exec-upsert",
            raw_event_count=20,
            assistant_text_preview="done",
        )
        store.save_checkpoint(cp2)

        loaded = store.load_checkpoint("exec-upsert")
        assert loaded is not None
        assert loaded.status == "completed"
        assert loaded.raw_event_count == 20
        assert loaded.assistant_text_preview == "done"

    def test_checkpoint_with_terminal_signals(self, store: ColdTraceStore) -> None:
        _init_orchestration_db(store._hub_root)
        signals = (
            ExecutionCheckpointSignal(
                source="provider", status="ok", timestamp="2026-04-12T00:00:00Z"
            ),
            ExecutionCheckpointSignal(
                source="runtime", status="error", timestamp="2026-04-12T00:01:00Z"
            ),
        )
        checkpoint = ExecutionCheckpoint(
            status="failed",
            execution_id="exec-signals",
            failure_cause="timeout",
            terminal_signals=signals,
        )
        store.save_checkpoint(checkpoint)

        loaded = store.load_checkpoint("exec-signals")
        assert loaded is not None
        assert loaded.status == "failed"
        assert loaded.failure_cause == "timeout"
        assert len(loaded.terminal_signals) == 2
        assert loaded.terminal_signals[0].source == "provider"
        assert loaded.terminal_signals[1].status == "error"

    def test_checkpoint_with_token_usage(self, store: ColdTraceStore) -> None:
        _init_orchestration_db(store._hub_root)
        checkpoint = ExecutionCheckpoint(
            status="completed",
            execution_id="exec-tokens",
            token_usage={"input": 100, "output": 200, "total": 300},
        )
        store.save_checkpoint(checkpoint)

        loaded = store.load_checkpoint("exec-tokens")
        assert loaded is not None
        assert loaded.token_usage == {"input": 100, "output": 200, "total": 300}

    def test_load_latest_checkpoint_for_thread(self, store: ColdTraceStore) -> None:
        _init_orchestration_db(store._hub_root)
        store.save_checkpoint(
            ExecutionCheckpoint(
                status="completed",
                execution_id="exec-t1",
                thread_target_id="thread-latest",
            )
        )
        store.save_checkpoint(
            ExecutionCheckpoint(
                status="running",
                execution_id="exec-t2",
                thread_target_id="thread-latest",
            )
        )

        loaded = store.load_latest_checkpoint_for_thread("thread-latest")
        assert loaded is not None
        assert loaded.execution_id == "exec-t2"

    def test_list_checkpoints(self, store: ColdTraceStore) -> None:
        _init_orchestration_db(store._hub_root)
        store.save_checkpoint(
            ExecutionCheckpoint(
                status="running",
                execution_id="exec-lc1",
                thread_target_id="thread-a",
            )
        )
        store.save_checkpoint(
            ExecutionCheckpoint(
                status="completed",
                execution_id="exec-lc2",
                thread_target_id="thread-b",
            )
        )

        all_cps = store.list_checkpoints()
        assert len(all_cps) == 2

        thread_a = store.list_checkpoints(thread_target_id="thread-a")
        assert len(thread_a) == 1
        assert thread_a[0].execution_id == "exec-lc1"

        completed = store.list_checkpoints(status="completed")
        assert len(completed) == 1
        assert completed[0].execution_id == "exec-lc2"

    def test_load_missing_checkpoint(self, store: ColdTraceStore) -> None:
        _init_orchestration_db(store._hub_root)
        assert store.load_checkpoint("no-such-exec") is None


class TestTraceAndCheckpointIntegration:
    def test_writer_produces_manifest_linked_to_checkpoint(
        self, store: ColdTraceStore
    ) -> None:
        _init_orchestration_db(store._hub_root)
        execution_id = "exec-integration"

        with store.open_writer(execution_id=execution_id) as writer:
            writer.append(
                event_family="tool_call",
                event_type="ToolCall",
                payload={"tool_name": "shell", "tool_input": {"cmd": "echo hi"}},
            )
            writer.append(
                event_family="output_delta",
                event_type="OutputDelta",
                payload={"content": "hi\n"},
            )
            manifest = writer.finalize()

        checkpoint = ExecutionCheckpoint(
            status="completed",
            execution_id=execution_id,
            trace_manifest_id=manifest.trace_id,
            assistant_text_preview="hi",
            assistant_char_count=3,
            raw_event_count=2,
        )
        store.save_checkpoint(checkpoint)

        loaded_cp = store.load_checkpoint(execution_id)
        assert loaded_cp is not None
        assert loaded_cp.trace_manifest_id == manifest.trace_id

        loaded_manifest = store.get_manifest(execution_id)
        assert loaded_manifest is not None
        assert loaded_manifest.trace_id == manifest.trace_id

        events = store.read_events(execution_id)
        assert len(events) == 2
        assert events[0]["payload"]["tool_name"] == "shell"
