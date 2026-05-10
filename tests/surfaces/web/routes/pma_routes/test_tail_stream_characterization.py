from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from tests.pma_support import _enable_pma

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes.pma_routes import tail_stream

pytestmark = pytest.mark.slow


class TestManagedThreadStatusShape:
    def test_status_endpoint_returns_required_fields_for_idle_thread(
        self, hub_env
    ) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(f"/hub/pma/threads/{managed_thread_id}/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["managed_thread_id"] == managed_thread_id
        assert payload["thread"] is not None
        assert "is_alive" in payload
        assert "status" in payload
        assert "operator_status" in payload
        assert "is_reusable" in payload
        assert "status_reason" in payload
        assert "status_changed_at" in payload
        assert "status_terminal" in payload
        assert "queue_depth" in payload
        assert "queued_turns" in payload
        assert "recent_progress" in payload
        assert "token_usage" in payload
        assert "latest_turn_id" in payload
        assert "latest_turn_status" in payload
        assert "latest_assistant_text" in payload
        assert "latest_output_excerpt" in payload
        assert "stream_available" in payload
        assert "work_status" in payload
        assert "terminal" in payload
        assert "stream_should_close" in payload
        assert "stream_close_reason" in payload
        assert payload["stream_lifecycle"] == {
            "work_status": payload["work_status"],
            "operator_status": payload["operator_status"],
            "terminal": payload["terminal"],
            "stream_should_close": payload["stream_should_close"],
            "stream_close_reason": payload["stream_close_reason"],
            "stream_available": payload["stream_available"],
        }
        assert "active_turn_diagnostics" in payload

        turn = payload["turn"]
        assert "managed_turn_id" in turn
        assert "status" in turn
        assert "activity" in turn
        assert "phase" in turn
        assert "phase_source" in turn
        assert "guidance" in turn
        assert "last_tool" in turn
        assert "elapsed_seconds" in turn
        assert "idle_seconds" in turn
        assert "started_at" in turn
        assert "finished_at" in turn
        assert "lifecycle_events" in turn
        assert "token_usage" in turn

    def test_status_endpoint_returns_404_for_missing_thread(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)

        with TestClient(app) as client:
            response = client.get("/hub/pma/threads/nonexistent-thread/status")

        assert response.status_code == 404

    def test_status_idle_thread_has_no_fabricated_turn(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(f"/hub/pma/threads/{managed_thread_id}/status")

        assert response.status_code == 200
        payload = response.json()
        turn = payload["turn"]
        assert turn["managed_turn_id"] is None
        assert turn["status"] is None
        assert payload["latest_turn_id"] is None
        assert payload["latest_turn_status"] is None
        assert not payload["latest_assistant_text"]
        assert payload["queue_depth"] == 0

    def test_status_idle_thread_activity_is_idle(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(f"/hub/pma/threads/{managed_thread_id}/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["turn"]["activity"] == "idle"

    def test_status_endpoint_exposes_queue_depth_field(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(f"/hub/pma/threads/{managed_thread_id}/status")

        assert response.status_code == 200
        payload = response.json()
        assert isinstance(payload["queue_depth"], int)
        assert isinstance(payload["queued_turns"], list)

    def test_status_rejects_zero_limit(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(
                f"/hub/pma/threads/{managed_thread_id}/status",
                params={"limit": 0},
            )

        assert response.status_code == 400

    def test_status_rejects_invalid_level(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(
                f"/hub/pma/threads/{managed_thread_id}/status",
                params={"level": "verbose"},
            )

        assert response.status_code == 400


class TestManagedThreadTailShape:
    def test_tail_endpoint_returns_snapshot_with_required_fields(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")

        assert response.status_code == 200
        payload = response.json()
        assert payload["managed_thread_id"] == managed_thread_id
        assert payload["agent"] == "codex"
        assert payload["turn_status"] is None
        assert "activity" in payload
        assert "events" in payload
        assert "last_event_id" in payload
        assert "lifecycle_events" in payload
        assert payload["work_status"] == "idle"
        assert payload["terminal"] is False
        assert payload["stream_should_close"] is True
        assert payload["stream_close_reason"] == "no_running_turn"
        assert payload["stream_lifecycle"] == {
            "work_status": payload["work_status"],
            "operator_status": payload["operator_status"],
            "terminal": payload["terminal"],
            "stream_should_close": payload["stream_should_close"],
            "stream_close_reason": payload["stream_close_reason"],
            "stream_available": payload["stream_available"],
        }

    def test_tail_endpoint_returns_404_for_missing_thread(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)

        with TestClient(app) as client:
            response = client.get("/hub/pma/threads/nonexistent-thread/tail")

        assert response.status_code == 404

    def test_tail_events_endpoint_returns_404_for_missing_thread(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)

        with TestClient(app) as client:
            response = client.get("/hub/pma/threads/nonexistent-thread/tail/events")

        assert response.status_code == 404

    def test_tail_endpoint_rejects_negative_since_event_id(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(
                f"/hub/pma/threads/{managed_thread_id}/tail",
                params={"since_event_id": -1},
            )

        assert response.status_code == 400

    def test_tail_endpoint_rejects_zero_limit(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(
                f"/hub/pma/threads/{managed_thread_id}/tail",
                params={"limit": 0},
            )

        assert response.status_code == 400

    def test_tail_endpoint_rejects_invalid_since_duration(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(
                f"/hub/pma/threads/{managed_thread_id}/tail",
                params={"since": "bad"},
            )

        assert response.status_code == 400

    def test_tail_endpoint_idle_thread_activity_is_idle(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")

        assert response.status_code == 200
        assert response.json()["activity"] == "idle"
        assert response.json()["turn_status"] is None


class TestTailSnapshotEnumContracts:
    def test_tail_event_types_are_from_known_set(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")

        assert response.status_code == 200
        payload = response.json()
        valid_event_types = {
            "progress",
            "assistant_update",
            "tool_started",
            "tool_completed",
            "tool_failed",
            "turn_completed",
            "turn_failed",
            "turn_interrupted",
        }
        for event in payload["events"]:
            assert event["event_type"] in valid_event_types
            assert isinstance(event["event_id"], int)
            assert isinstance(event["received_at"], str)
            assert "summary" in event

    def test_tail_activity_values_are_from_known_set(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")

        assert response.status_code == 200
        valid_activities = {
            "idle",
            "running",
            "stalled",
            "completed",
            "interrupted",
            "failed",
        }
        assert response.json()["activity"] in valid_activities

    def test_tail_phase_is_present_when_turn_exists(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")

        assert response.status_code == 200
        payload = response.json()
        valid_phases = {
            "completed",
            "interrupted",
            "failed",
            "waiting_on_tool_call",
            "model_running",
            "likely_hung",
            "no_stream_available",
            "booting_runtime",
        }
        if payload["turn_status"] is not None:
            assert payload["phase"] in valid_phases

    async def test_tail_snapshot_captures_token_usage_from_runtime_fallback(
        self, monkeypatch, hub_env
    ) -> None:
        class Harness:
            def supports(self, capability: str) -> bool:
                return capability == "event_streaming"

            def allows_parallel_event_stream(self) -> bool:
                return True

            async def list_progress_events(
                self,
                _backend_thread_id: str,
                _backend_turn_id: str,
                *,
                after_id: int,
                limit: int,
            ) -> list[dict[str, object]]:
                assert after_id == 0
                assert limit == 50
                return [
                    {
                        "id": 1,
                        "received_at": 1_762_000_000_000,
                        "message": {
                            "method": "token/usage",
                            "params": {
                                "usage": {
                                    "totalTokens": 123,
                                    "inputTokens": 100,
                                    "outputTokens": 23,
                                    "modelContextWindow": 1000,
                                }
                            },
                        },
                    }
                ]

        monkeypatch.setattr(
            tail_stream,
            "get_pma_request_context",
            lambda _request: SimpleNamespace(
                hub_root=hub_env.hub_root,
                thread_store=lambda: object(),
            ),
        )
        monkeypatch.setattr(
            tail_stream,
            "_load_managed_thread_tail_store_state",
            lambda **_kwargs: (
                SimpleNamespace(
                    agent_id="codex",
                    backend_thread_id="backend-thread-1",
                    status="running",
                    lifecycle_status="active",
                ),
                SimpleNamespace(
                    execution_id="turn-1",
                    status="running",
                    started_at="2026-05-10T17:00:00+00:00",
                    finished_at=None,
                    backend_id="backend-turn-1",
                ),
                [],
                None,
            ),
        )

        payload = await tail_stream._build_managed_thread_tail_snapshot(
            request=object(),
            service=object(),
            managed_thread_id="thread-1",
            harness=Harness(),
            limit=50,
            level="info",
            since_ms=None,
            resume_after=0,
        )

        assert payload["events"] == []
        assert payload["token_usage"] == {
            "totalTokens": 123,
            "inputTokens": 100,
            "outputTokens": 23,
            "modelContextWindow": 1000,
        }


class TestStatusDoesNotSynthesizeState:
    def test_status_does_not_fabricate_running_state_for_idle_thread(
        self, hub_env
    ) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(f"/hub/pma/threads/{managed_thread_id}/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["turn"]["managed_turn_id"] is None
        assert payload["turn"]["status"] is None
        assert payload["latest_turn_id"] is None
        assert payload["latest_turn_status"] is None
        assert not payload["latest_assistant_text"]
        assert payload["queue_depth"] == 0

    def test_status_does_not_fabricate_terminal_state_from_idle_thread(
        self, hub_env
    ) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            status = client.get(f"/hub/pma/threads/{managed_thread_id}/status")

        assert status.status_code == 200
        payload = status.json()
        turn = payload["turn"]
        assert turn["activity"] == "idle"
        assert turn["lifecycle_events"] == []
        assert turn["elapsed_seconds"] is None

    def test_tail_does_not_fabricate_events_for_idle_thread(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = ManagedThreadStore(hub_env.hub_root)
        created = store.create_thread(
            "codex",
            hub_env.repo_root.resolve(),
            repo_id=hub_env.repo_id,
        )
        managed_thread_id = str(created["managed_thread_id"])

        with TestClient(app) as client:
            response = client.get(f"/hub/pma/threads/{managed_thread_id}/tail")

        assert response.status_code == 200
        payload = response.json()
        assert payload["events"] == []
        assert payload["lifecycle_events"] == []
        assert payload["turn_status"] is None
