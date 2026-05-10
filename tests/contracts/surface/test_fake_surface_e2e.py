"""
Fake-surface E2E journey test.

Exercises the full PMA web API journey from a non-web surface perspective:
create chat, send message, open memory, link to a ticket.  Uses
``WebPmaSurfaceSimulator`` (backed by a FastAPI ``TestClient``) so no
real browser or agent process is required.

The test catches mismatches between web UI assumptions and surface-port
payloads by verifying every API response conforms to the contracts the
frontend view-model mappers rely on.
"""

from __future__ import annotations

import pytest

from tests.chat_surface_lab.web_pma_simulator import (
    WebPmaSurfaceSimulator,
    build_web_pma_simulator,
    close_web_pma_simulator,
)


@pytest.fixture()
def simulator(tmp_path):
    sim = build_web_pma_simulator(tmp_path)
    yield sim
    close_web_pma_simulator(sim)


class TestFakeSurfaceE2EJourney:
    """Full PMA journey via fake surface harness."""

    def test_create_chat_and_send_message(
        self, simulator: WebPmaSurfaceSimulator
    ) -> None:
        thread_id = simulator.create_thread(agent="hermes", name="E2E journey chat")
        assert thread_id

        result = simulator.send_message(message="Hello from fake surface")
        assert result["status"] == "ok"
        assert result["managed_thread_id"] == thread_id
        assert result["assistant_text"]

    def test_memory_docs_are_available_via_contextspace_route(
        self,
        simulator: WebPmaSurfaceSimulator,
    ) -> None:
        hub_root = simulator.hub.hub_root
        contextspace_dir = (
            hub_root / simulator.hub.repo_id / ".codex-autorunner" / "contextspace"
        )
        contextspace_dir.mkdir(parents=True, exist_ok=True)
        (contextspace_dir / "active_context.md").write_text(
            "# Active\nE2E test context", encoding="utf-8"
        )
        (contextspace_dir / "spec.md").write_text(
            "# Spec\nE2E test spec", encoding="utf-8"
        )
        (contextspace_dir / "decisions.md").write_text(
            "# Decisions\nE2E decisions", encoding="utf-8"
        )

        response = simulator.client.get("/api/contextspace")
        assert response.status_code == 200
        body = response.json()
        assert "active_context" in body
        assert "spec" in body
        assert "decisions" in body
        assert "kinds" in body

    def test_ticket_list_is_available_via_hub_route(
        self,
        simulator: WebPmaSurfaceSimulator,
    ) -> None:
        response = simulator.client.get("/hub/tickets")
        assert response.status_code == 200
        body = response.json()
        assert "tickets" in body

    def test_thread_status_projection_matches_frontend_contract(
        self,
        simulator: WebPmaSurfaceSimulator,
    ) -> None:
        simulator.create_thread(agent="hermes")
        simulator.send_message(message="Check status projection")

        simulator.refresh_api_projection()

        status = simulator.latest_status_payload
        assert "work_status" in status or "latest_turn_status" in status
        assert "managed_thread_id" in status or "thread_target_id" in status

    def test_thread_timeline_projection_matches_frontend_contract(
        self,
        simulator: WebPmaSurfaceSimulator,
    ) -> None:
        simulator.create_thread(agent="hermes")
        simulator.send_message(message="Check timeline projection")

        simulator.refresh_api_projection()

        timeline = simulator.latest_timeline_payload
        assert "items" in timeline
        items = timeline["items"]
        assert isinstance(items, list)
        for item in items:
            assert "kind" in item

    def test_surface_port_payload_fields_match_frontend_mappers(
        self,
        simulator: WebPmaSurfaceSimulator,
    ) -> None:
        simulator.create_thread(agent="hermes", name="Payload check")
        result = simulator.send_message(message="Verify payload shape")

        assert "managed_thread_id" in result
        assert "managed_turn_id" in result
        assert "assistant_text" in result

        simulator.refresh_api_projection()

        status = simulator.latest_status_payload
        timeline = simulator.latest_timeline_payload
        queue = simulator.latest_queue_payload

        assert isinstance(status, dict)
        assert isinstance(timeline, dict)
        assert isinstance(queue, dict)

    def test_full_journey_create_chat_memory_ticket(
        self,
        simulator: WebPmaSurfaceSimulator,
    ) -> None:
        thread_id = simulator.create_thread(agent="hermes", name="Full journey")
        assert thread_id

        send_result = simulator.send_message(message="Run the full journey")
        assert send_result["status"] == "ok"

        hub_root = simulator.hub.hub_root
        contextspace_dir = (
            hub_root / simulator.hub.repo_id / ".codex-autorunner" / "contextspace"
        )
        contextspace_dir.mkdir(parents=True, exist_ok=True)
        (contextspace_dir / "spec.md").write_text(
            "# Spec\nFull journey spec", encoding="utf-8"
        )

        memory_response = simulator.client.get("/api/contextspace")
        assert memory_response.status_code == 200
        memory_body = memory_response.json()
        assert "spec" in memory_body

        tickets_response = simulator.client.get("/hub/tickets")
        assert tickets_response.status_code == 200

        simulator.refresh_api_projection()
        assert simulator.managed_thread_id == thread_id
        assert isinstance(simulator.available_actions, tuple)


class TestFakeSurfacePmaMemoryDocKinds:
    """Verify the frontend memory doc catalog matches what the API returns."""

    EXPECTED_CONTEXTSPACE_DOCS = {"active_context", "spec", "decisions"}

    def test_contextspace_api_returns_all_backend_doc_kinds(
        self,
        simulator: WebPmaSurfaceSimulator,
    ) -> None:
        hub_root = simulator.hub.hub_root
        contextspace_dir = (
            hub_root / simulator.hub.repo_id / ".codex-autorunner" / "contextspace"
        )
        contextspace_dir.mkdir(parents=True, exist_ok=True)
        for name in ["active_context.md", "spec.md", "decisions.md"]:
            (contextspace_dir / name).write_text(f"# {name}", encoding="utf-8")

        response = simulator.client.get("/api/contextspace")
        assert response.status_code == 200
        body = response.json()
        for doc in self.EXPECTED_CONTEXTSPACE_DOCS:
            assert doc in body, f"contextspace API missing {doc}"


class TestFakeSurfaceScopeUrnInApiResponses:
    """Verify scope URNs in API responses match the frontend parser."""

    def test_thread_response_scope_urn_or_owner_fields(
        self,
        simulator: WebPmaSurfaceSimulator,
    ) -> None:
        thread_id = simulator.create_thread(agent="hermes")
        response = simulator.client.get(f"/hub/pma/threads/{thread_id}/status")
        assert response.status_code == 200
        body = response.json()

        thread_obj = body.get("thread", {})
        has_scope_urn = "scope_urn" in thread_obj and thread_obj["scope_urn"]
        has_owner_fields = (
            thread_obj.get("resource_kind")
            or thread_obj.get("resource_id")
            or thread_obj.get("repo_id")
        )

        assert has_scope_urn or has_owner_fields, (
            "API thread status response must include scope_urn or legacy owner fields "
            "(resource_kind, resource_id, repo_id) on the thread object "
            "for the frontend scope resolver to work"
        )
