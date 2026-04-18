from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.conftest import write_test_config

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.server import create_hub_app

pytestmark = pytest.mark.slow


def _enable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


class TestManagedThreadStatusShape:
    def test_status_endpoint_returns_required_fields_for_idle_thread(
        self, hub_env
    ) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = PmaThreadStore(hub_env.hub_root)
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
        assert "latest_turn_id" in payload
        assert "latest_turn_status" in payload
        assert "latest_assistant_text" in payload
        assert "latest_output_excerpt" in payload
        assert "stream_available" in payload
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

    def test_status_endpoint_returns_404_for_missing_thread(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)

        with TestClient(app) as client:
            response = client.get("/hub/pma/threads/nonexistent-thread/status")

        assert response.status_code == 404

    def test_status_idle_thread_has_no_fabricated_turn(self, hub_env) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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


class TestStatusDoesNotSynthesizeState:
    def test_status_does_not_fabricate_running_state_for_idle_thread(
        self, hub_env
    ) -> None:
        _enable_pma(hub_env.hub_root)
        app = create_hub_app(hub_env.hub_root)
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
        store = PmaThreadStore(hub_env.hub_root)
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
