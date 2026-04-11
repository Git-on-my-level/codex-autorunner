from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.conftest import write_test_config

from codex_autorunner.core.orchestration.cold_trace_store import (
    ColdTraceStore,
    ColdTraceWriter,
)
from codex_autorunner.core.orchestration.execution_history import ExecutionCheckpoint
from codex_autorunner.server import create_hub_app


def _init_orchestration_db(hub_root: Path) -> None:
    from codex_autorunner.core.orchestration.sqlite import (
        initialize_orchestration_sqlite,
    )

    initialize_orchestration_sqlite(hub_root)


def _seed_trace_and_checkpoint(hub_root: Path, execution_id: str) -> None:
    _init_orchestration_db(hub_root)
    store = ColdTraceStore(hub_root)
    with ColdTraceWriter(hub_root=hub_root, execution_id=execution_id) as writer:
        writer.append(
            event_family="tool_call",
            event_type="ToolCall",
            payload={
                "tool_name": "shell",
                "tool_input": {"cmd": "echo hello"},
            },
        )
        writer.append(
            event_family="output_delta",
            event_type="OutputDelta",
            payload={
                "content": "thinking about things",
                "delta_type": "assistant_stream",
            },
        )
        writer.append(
            event_family="tool_result",
            event_type="ToolResult",
            payload={"tool_name": "shell", "status": "ok", "result": "hello"},
        )
        writer.append(
            event_family="terminal",
            event_type="Completed",
            payload={"final_message": "done"},
        )
        manifest = writer.finalize()

    store.save_checkpoint(
        ExecutionCheckpoint(
            status="ok",
            execution_id=execution_id,
            assistant_text_preview="done",
            assistant_char_count=4,
            trace_manifest_id=manifest.trace_id,
        )
    )


def _make_hub_app(hub_root: Path) -> TestClient:
    from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG

    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    cfg["pma"]["managed_thread_terminal_followup_default"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    app = create_hub_app(hub_root)
    return TestClient(app)


def _create_thread(client: TestClient, hub_env) -> str:
    resp = client.post(
        "/hub/pma/threads",
        json={
            "agent": "codex",
            "resource_kind": "repo",
            "resource_id": hub_env.repo_id,
            "name": "Trace test thread",
        },
    )
    assert resp.status_code == 200
    return resp.json()["thread"]["managed_thread_id"]


pytestmark = pytest.mark.slow


class TestTraceManifestEndpoints:
    def test_get_trace_manifest_found(self, hub_env) -> None:
        execution_id = "exec-trace-001"
        _seed_trace_and_checkpoint(hub_env.hub_root, execution_id)

        with _make_hub_app(hub_env.hub_root) as client:
            resp = client.get(f"/hub/pma/traces/manifests/{execution_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["execution_id"] == execution_id
        assert body["storage_layer"] == "cold_trace"
        manifest = body["manifest"]
        assert manifest["execution_id"] == execution_id
        assert manifest["event_count"] == 4
        assert manifest["status"] == "finalized"
        assert "tool_call" in manifest["includes_families"]
        assert "output_delta" in manifest["includes_families"]

    def test_get_trace_manifest_not_found(self, hub_env) -> None:
        _init_orchestration_db(hub_env.hub_root)

        with _make_hub_app(hub_env.hub_root) as client:
            resp = client.get("/hub/pma/traces/manifests/nonexistent")

        assert resp.status_code == 404

    def test_list_trace_manifests(self, hub_env) -> None:
        _seed_trace_and_checkpoint(hub_env.hub_root, "exec-a")
        _seed_trace_and_checkpoint(hub_env.hub_root, "exec-b")

        with _make_hub_app(hub_env.hub_root) as client:
            resp = client.get("/hub/pma/traces/manifests")

        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["storage_layer"] == "cold_trace"
        execution_ids = {m["execution_id"] for m in body["manifests"]}
        assert "exec-a" in execution_ids
        assert "exec-b" in execution_ids

    def test_list_trace_manifests_with_status_filter(self, hub_env) -> None:
        _seed_trace_and_checkpoint(hub_env.hub_root, "exec-filtered")

        with _make_hub_app(hub_env.hub_root) as client:
            resp_ok = client.get("/hub/pma/traces/manifests?status=finalized")
            resp_empty = client.get("/hub/pma/traces/manifests?status=archived")

        assert resp_ok.status_code == 200
        assert resp_ok.json()["count"] == 1
        assert resp_empty.status_code == 200
        assert resp_empty.json()["count"] == 0

    def test_list_trace_manifests_invalid_limit(self, hub_env) -> None:
        with _make_hub_app(hub_env.hub_root) as client:
            resp = client.get("/hub/pma/traces/manifests?limit=0")

        assert resp.status_code == 400


class TestTraceEventsEndpoint:
    def test_get_trace_events(self, hub_env) -> None:
        execution_id = "exec-events-001"
        _seed_trace_and_checkpoint(hub_env.hub_root, execution_id)

        with _make_hub_app(hub_env.hub_root) as client:
            resp = client.get(f"/hub/pma/traces/events/{execution_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["execution_id"] == execution_id
        assert body["storage_layer"] == "cold_trace"
        assert body["manifest_available"] is True
        assert body["total_count"] == 4
        assert body["has_more"] is False
        assert len(body["events"]) == 4

        families = [e["event_family"] for e in body["events"]]
        assert "tool_call" in families
        assert "output_delta" in families
        assert "tool_result" in families
        assert "terminal" in families

    def test_get_trace_events_paginated(self, hub_env) -> None:
        execution_id = "exec-page-001"
        _seed_trace_and_checkpoint(hub_env.hub_root, execution_id)

        with _make_hub_app(hub_env.hub_root) as client:
            page1 = client.get(f"/hub/pma/traces/events/{execution_id}?limit=2")
            page2 = client.get(
                f"/hub/pma/traces/events/{execution_id}?offset=2&limit=2"
            )

        assert page1.status_code == 200
        body1 = page1.json()
        assert len(body1["events"]) == 2
        assert body1["total_count"] == 4
        assert body1["has_more"] is True

        assert page2.status_code == 200
        body2 = page2.json()
        assert len(body2["events"]) == 2
        assert body2["has_more"] is False

    def test_get_trace_events_uses_store_pagination_for_unfiltered_pages(
        self, hub_env, monkeypatch
    ) -> None:
        execution_id = "exec-page-store-001"
        _seed_trace_and_checkpoint(hub_env.hub_root, execution_id)
        captured: dict[str, int | None] = {}
        original = ColdTraceStore.read_events

        def _capture(
            self, execution_id_arg: str, *, offset: int = 0, limit: int | None = None
        ):
            captured["offset"] = offset
            captured["limit"] = limit
            return original(self, execution_id_arg, offset=offset, limit=limit)

        monkeypatch.setattr(ColdTraceStore, "read_events", _capture)

        with _make_hub_app(hub_env.hub_root) as client:
            resp = client.get(f"/hub/pma/traces/events/{execution_id}?offset=1&limit=2")

        assert resp.status_code == 200
        assert captured == {"offset": 1, "limit": 2}
        shutil.rmtree(hub_env.hub_root, ignore_errors=True)

    def test_get_trace_events_family_filter(self, hub_env) -> None:
        execution_id = "exec-filter-001"
        _seed_trace_and_checkpoint(hub_env.hub_root, execution_id)

        with _make_hub_app(hub_env.hub_root) as client:
            resp = client.get(f"/hub/pma/traces/events/{execution_id}?family=tool_call")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert len(body["events"]) == 1
        assert body["events"][0]["event_family"] == "tool_call"

    def test_get_trace_events_no_manifest(self, hub_env) -> None:
        _init_orchestration_db(hub_env.hub_root)

        with _make_hub_app(hub_env.hub_root) as client:
            resp = client.get("/hub/pma/traces/events/nonexistent")

        assert resp.status_code == 200
        body = resp.json()
        assert body["events"] == []
        assert body["manifest_available"] is False
        assert body["total_count"] == 0

    def test_get_trace_events_invalid_offset(self, hub_env) -> None:
        with _make_hub_app(hub_env.hub_root) as client:
            resp = client.get("/hub/pma/traces/events/x?offset=-1")

        assert resp.status_code == 400

    def test_get_trace_events_invalid_limit(self, hub_env) -> None:
        with _make_hub_app(hub_env.hub_root) as client:
            resp = client.get("/hub/pma/traces/events/x?limit=0")

        assert resp.status_code == 400


class TestTraceCheckpointEndpoint:
    def test_get_trace_checkpoint_found(self, hub_env) -> None:
        execution_id = "exec-chkpt-001"
        _seed_trace_and_checkpoint(hub_env.hub_root, execution_id)

        with _make_hub_app(hub_env.hub_root) as client:
            resp = client.get(f"/hub/pma/traces/checkpoint/{execution_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["execution_id"] == execution_id
        assert body["storage_layer"] == "compact_checkpoint"
        assert body["cold_trace_available"] is True
        checkpoint = body["checkpoint"]
        assert checkpoint["status"] == "ok"
        assert checkpoint["assistant_text_preview"] == "done"
        trace_manifest = body["trace_manifest"]
        assert trace_manifest["event_count"] == 4
        assert trace_manifest["byte_count"] > 0
        assert trace_manifest["checksum"].startswith("sha256:")
        assert "tool_call" in trace_manifest["includes_families"]

    def test_get_trace_checkpoint_not_found(self, hub_env) -> None:
        _init_orchestration_db(hub_env.hub_root)

        with _make_hub_app(hub_env.hub_root) as client:
            resp = client.get("/hub/pma/traces/checkpoint/nonexistent")

        assert resp.status_code == 404


class TestTurnDetailTraceMetadata:
    def test_turn_detail_includes_trace_metadata(self, hub_env) -> None:
        from codex_autorunner.core.pma_thread_store import PmaThreadStore

        hub_root = hub_env.hub_root
        _init_orchestration_db(hub_root)

        with _make_hub_app(hub_root) as client:
            thread_id = _create_thread(client, hub_env)
            store = PmaThreadStore(hub_root)
            turn = store.create_turn(thread_id, prompt="test prompt")
            managed_turn_id = turn["managed_turn_id"]

            turn_resp = client.get(
                f"/hub/pma/threads/{thread_id}/turns/{managed_turn_id}"
            )
            assert turn_resp.status_code == 200
            body = turn_resp.json()
            assert body["turn"]["managed_turn_id"] == managed_turn_id
            assert "trace_metadata" in body
            meta = body["trace_metadata"]
            assert isinstance(meta["hot_timeline_entries"], int)
            assert isinstance(meta["hot_preview_truncated"], bool)
            assert isinstance(meta["hot_preview_only"], bool)
            assert meta["cold_trace_available"] is False
            assert meta["checkpoint_available"] is False

    def test_turn_detail_with_cold_trace(self, hub_env) -> None:
        from codex_autorunner.core.pma_thread_store import PmaThreadStore

        hub_root = hub_env.hub_root
        _init_orchestration_db(hub_root)

        with _make_hub_app(hub_root) as client:
            thread_id = _create_thread(client, hub_env)
            store = PmaThreadStore(hub_root)
            turn = store.create_turn(thread_id, prompt="test cold trace")
            managed_turn_id = turn["managed_turn_id"]

            _seed_trace_and_checkpoint(hub_root, managed_turn_id)

            turn_resp = client.get(
                f"/hub/pma/threads/{thread_id}/turns/{managed_turn_id}"
            )
            assert turn_resp.status_code == 200
            body = turn_resp.json()
            meta = body["trace_metadata"]
            assert meta["cold_trace_available"] is True
            assert meta["checkpoint_available"] is True
            assert "cold_trace" in meta
            assert meta["cold_trace"]["event_count"] == 4
