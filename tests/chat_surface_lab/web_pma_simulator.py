from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.browser.runtime import BrowserRuntime
from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
)
from codex_autorunner.core.orchestration.turn_timeline import persist_turn_timeline
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.ports.run_event import ApprovalRequested, RunNotice
from codex_autorunner.manifest import load_manifest, save_manifest
from codex_autorunner.server import create_hub_app
from tests.chat_surface_lab.artifact_manifests import ArtifactManifest
from tests.chat_surface_lab.evidence_artifacts import write_surface_evidence_artifacts
from tests.chat_surface_lab.transcript_models import (
    TranscriptEvent,
    TranscriptEventKind,
    TranscriptParty,
    TranscriptTimeline,
)
from tests.conftest import write_test_config


@dataclass(frozen=True)
class WebPmaTestHub:
    hub_root: Path
    repo_id: str
    repo_root: Path


class WebPmaSurfaceSimulator:
    """PMA web API simulator backed by FastAPI TestClient routes."""

    def __init__(self, *, hub: WebPmaTestHub, app: Any, client: TestClient) -> None:
        self.hub = hub
        self.app = app
        self.client = client
        self._surface_timeline: list[dict[str, Any]] = []
        self._transcript_events: list[dict[str, Any]] = []
        self.log_records: list[dict[str, Any]] = []
        self.execution_status: Optional[str] = None
        self.execution_error: Optional[str] = None
        self.managed_thread_id: Optional[str] = None
        self.managed_turn_id: Optional[str] = None
        self.latest_status_payload: dict[str, Any] = {}
        self.latest_timeline_payload: dict[str, Any] = {}
        self.latest_queue_payload: dict[str, Any] = {}
        self.available_actions: tuple[str, ...] = ()
        self.pending_interactions: tuple[dict[str, Any], ...] = ()
        self.final_delivery_state: dict[str, Any] = {}

    @property
    def surface_timeline(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._surface_timeline)

    @property
    def root(self) -> Path:
        return self.hub.hub_root

    def create_thread(
        self, *, agent: str = "hermes", name: Optional[str] = None
    ) -> str:
        payload: dict[str, Any] = {
            "agent": agent,
            "resource_kind": "repo",
            "resource_id": self.hub.repo_id,
        }
        if name:
            payload["name"] = name
        response = self.client.post("/hub/pma/threads", json=payload)
        self._record_route("create_thread", response.status_code, payload=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        thread = body["thread"]
        self.managed_thread_id = str(thread["managed_thread_id"])
        self._record_event(
            kind="status",
            party=TranscriptParty.PLATFORM,
            text=f"PMA web thread created: {self.managed_thread_id}",
            metadata={"route": "create_thread", "thread": thread},
        )
        return self.managed_thread_id

    def send_message(
        self,
        *,
        message: str,
        wait_for_confirmation: bool = True,
        busy_policy: Optional[str] = None,
    ) -> dict[str, Any]:
        managed_thread_id = self._require_thread()
        _ = wait_for_confirmation, busy_policy
        store = PmaThreadStore(self.hub.hub_root)
        turn = store.create_turn(managed_thread_id, prompt=message)
        self.managed_turn_id = str(turn.get("managed_turn_id") or "")
        assistant_text = "fixture reply"
        assert store.mark_turn_finished(
            self.managed_turn_id,
            status="ok",
            assistant_text=assistant_text,
            backend_turn_id="backend-turn-web-pma-1",
        )
        body = {
            "status": "ok",
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": self.managed_turn_id,
            "backend_thread_id": "backend-thread-web-pma",
            "assistant_text": assistant_text,
            "error": None,
        }
        self._record_route("send_message_seed", 200, payload={"message": message})
        self.execution_status = "ok"
        self.execution_error = None
        self._record_event(
            kind="send",
            party=TranscriptParty.USER,
            text=message,
            metadata={"route": "send_message", "response": body},
        )
        self._record_event(
            kind="send",
            party=TranscriptParty.ASSISTANT,
            text=assistant_text,
            metadata={
                "route": "send_message",
                "managed_turn_id": self.managed_turn_id,
            },
        )
        self.final_delivery_state = {
            "status": self.execution_status,
            "assistant_text": assistant_text,
            "error": self.execution_error,
            "managed_turn_id": self.managed_turn_id,
        }
        return body

    def seed_approval_request(
        self,
        *,
        request_id: str,
        description: str,
        command: str = "pytest -q",
    ) -> None:
        managed_turn_id = self._require_turn()
        persist_turn_timeline(
            self.hub.hub_root,
            execution_id=managed_turn_id,
            target_kind="thread",
            target_id=self._require_thread(),
            events=[
                RunNotice(
                    timestamp="2026-01-01T00:00:00Z",
                    kind="status",
                    message="Preparing approval check",
                ),
                ApprovalRequested(
                    timestamp="2026-01-01T00:00:01Z",
                    request_id=request_id,
                    description=description,
                    context={"command": command},
                ),
            ],
        )

    def refresh_api_projection(self) -> None:
        managed_thread_id = self._require_thread()
        status_response = self.client.get(
            f"/hub/pma/threads/{managed_thread_id}/status"
        )
        self._record_route("status", status_response.status_code)
        assert status_response.status_code == 200, status_response.text
        self.latest_status_payload = status_response.json()

        queue_response = self.client.get(f"/hub/pma/threads/{managed_thread_id}/queue")
        self._record_route("queue", queue_response.status_code)
        assert queue_response.status_code == 200, queue_response.text
        self.latest_queue_payload = queue_response.json()

        timeline_response = self.client.get(
            f"/hub/pma/threads/{managed_thread_id}/timeline"
        )
        self._record_route("timeline", timeline_response.status_code)
        assert timeline_response.status_code == 200, timeline_response.text
        self.latest_timeline_payload = timeline_response.json()

        self.pending_interactions = tuple(
            item
            for item in self.latest_timeline_payload.get("items", [])
            if item.get("kind") == "approval"
        )
        self.available_actions = self._derive_available_actions()
        self._record_projection_artifacts()

    def to_normalized_transcript(
        self,
        *,
        scenario_id: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> TranscriptTimeline:
        events = [
            TranscriptEvent(
                kind=TranscriptEventKind(str(item["kind"])),
                party=TranscriptParty(str(item["party"])),
                timestamp_ms=int(item["timestamp_ms"]),
                surface_kind="web_pma",
                text=str(item.get("text") or ""),
                metadata=dict(item.get("metadata") or {}),
            )
            for item in self._transcript_events
        ]
        merged_metadata = dict(metadata or {})
        merged_metadata.update(
            {
                "surface_kind": "web_pma",
                "event_count": str(len(events)),
                "managed_thread_id": self.managed_thread_id,
                "managed_turn_id": self.managed_turn_id,
                "status_state": self.latest_status_payload.get("work_status"),
                "queue_depth": self.latest_status_payload.get("queue_depth"),
                "available_actions": list(self.available_actions),
                "pending_interaction_count": len(self.pending_interactions),
                "final_delivery_status": self.final_delivery_state.get("status"),
                "timeline_item_kinds": [
                    item.get("kind")
                    for item in self.latest_timeline_payload.get("items", [])
                ],
                "surface_timeline_kinds": [
                    item.get("kind") for item in self._surface_timeline
                ],
            }
        )
        return TranscriptTimeline(
            scenario_id=scenario_id,
            events=tuple(events),
            metadata=merged_metadata,
        )

    def write_artifacts(
        self,
        *,
        output_dir: Path,
        scenario_id: str,
        run_id: str,
        browser_runtime: Optional[BrowserRuntime] = None,
    ) -> ArtifactManifest:
        transcript = self.to_normalized_transcript(scenario_id=scenario_id)
        return write_surface_evidence_artifacts(
            output_dir=output_dir,
            scenario_id=scenario_id,
            run_id=run_id,
            surface_kind="web_pma",
            surface_timeline=self._surface_timeline,
            transcript=transcript,
            log_records=self.log_records,
            browser_runtime=browser_runtime,
        )

    def _derive_available_actions(self) -> tuple[str, ...]:
        status = self.latest_status_payload
        actions = ["send_message", "archive_thread"]
        if status.get("is_alive") or status.get("latest_turn_status") == "running":
            actions.append("interrupt_turn")
        if int(status.get("queue_depth") or 0) > 0:
            actions.extend(["clear_queue", "cancel_queued_turn"])
        if self.pending_interactions:
            actions.extend(["approve_interaction", "reject_interaction"])
        return tuple(actions)

    def _record_projection_artifacts(self) -> None:
        self._surface_timeline.extend(
            [
                {
                    "kind": "canonical_timeline",
                    "payload": self.latest_timeline_payload,
                },
                {"kind": "progress_status", "payload": self.latest_status_payload},
                {
                    "kind": "available_actions",
                    "payload": {"actions": list(self.available_actions)},
                },
                {
                    "kind": "pending_interactions",
                    "payload": {"items": list(self.pending_interactions)},
                },
                {"kind": "queue_state", "payload": self.latest_queue_payload},
                {"kind": "final_delivery_state", "payload": self.final_delivery_state},
            ]
        )
        for interaction in self.pending_interactions:
            self._record_event(
                kind="callback",
                party=TranscriptParty.PLATFORM,
                text=str(interaction.get("payload", {}).get("description") or ""),
                metadata={"interaction": interaction},
            )
        self._record_event(
            kind="status",
            party=TranscriptParty.PLATFORM,
            text=str(self.latest_status_payload.get("work_status") or ""),
            metadata={
                "status": self.latest_status_payload,
                "available_actions": list(self.available_actions),
                "pending_interactions": list(self.pending_interactions),
            },
        )

    def _record_route(
        self, route: str, status_code: int, *, payload: Optional[dict[str, Any]] = None
    ) -> None:
        self.log_records.append(
            {
                "event": "web_pma.route",
                "route": route,
                "status_code": status_code,
                "payload": payload or {},
            }
        )

    def _record_event(
        self,
        *,
        kind: str,
        party: TranscriptParty,
        text: str,
        metadata: dict[str, Any],
    ) -> None:
        self._transcript_events.append(
            {
                "kind": kind,
                "party": party.value,
                "timestamp_ms": int(time.time() * 1000),
                "text": text,
                "metadata": metadata,
            }
        )

    def _require_thread(self) -> str:
        if not self.managed_thread_id:
            raise AssertionError("web_pma action requires an active managed thread")
        return self.managed_thread_id

    def _require_turn(self) -> str:
        if not self.managed_turn_id:
            raise AssertionError("web_pma action requires an active managed turn")
        return self.managed_turn_id


class _FakeTurnHandle:
    def __init__(self, *, turn_id: str, assistant_text: str) -> None:
        self.turn_id = turn_id
        self._assistant_text = assistant_text

    async def wait(self, timeout=None):
        _ = timeout
        return type(
            "Result",
            (),
            {"agent_messages": [self._assistant_text], "raw_events": [], "errors": []},
        )()


class _FakeClient:
    def __init__(self) -> None:
        self._turn_count = 0
        self.thread_start_roots: list[str] = []

    async def thread_resume(self, thread_id: str) -> None:
        _ = thread_id

    async def thread_start(self, root: str) -> dict[str, str]:
        self.thread_start_roots.append(root)
        return {"id": "backend-thread-web-pma"}

    async def turn_start(
        self,
        thread_id: str,
        prompt: str,
        approval_policy: str,
        sandbox_policy: str,
        **turn_kwargs,
    ) -> _FakeTurnHandle:
        _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
        self._turn_count += 1
        return _FakeTurnHandle(
            turn_id=f"backend-turn-web-pma-{self._turn_count}",
            assistant_text=f"web pma assistant output {self._turn_count}",
        )


class _FakeSupervisor:
    def __init__(self) -> None:
        self.client = _FakeClient()

    async def get_client(self, hub_root: Path) -> _FakeClient:
        _ = hub_root
        return self.client


def build_web_pma_simulator(root: Path) -> WebPmaSurfaceSimulator:
    hub = _build_test_hub(root)
    app = create_hub_app(hub.hub_root)
    app.state.app_server_supervisor = _FakeSupervisor()
    app.state.app_server_events = object()
    client = TestClient(app)
    return WebPmaSurfaceSimulator(hub=hub, app=app, client=client)


def close_web_pma_simulator(simulator: WebPmaSurfaceSimulator) -> None:
    simulator.client.close()


def _build_test_hub(root: Path) -> WebPmaTestHub:
    hub_root = (root / "hub").resolve()
    hub_root.mkdir(parents=True, exist_ok=True)
    seed_hub_files(hub_root, force=True)
    repo_id = "repo"
    repo_root = hub_root / "worktrees" / repo_id
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / ".git").mkdir(exist_ok=True)
    seed_repo_files(repo_root, git_required=False)

    config = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    config.setdefault("pma", {})
    config["pma"]["enabled"] = True
    config["pma"]["managed_thread_terminal_followup_default"] = False
    config["pma"]["reactive_enabled"] = False
    write_test_config(hub_root / CONFIG_FILENAME, config)

    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(hub_config.manifest_path, manifest, hub_root)
    PmaThreadStore(hub_root)
    return WebPmaTestHub(hub_root=hub_root, repo_id=repo_id, repo_root=repo_root)
