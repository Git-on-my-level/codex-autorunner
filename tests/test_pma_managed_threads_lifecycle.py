from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.server import create_hub_app
from tests.conftest import write_test_config


def _enable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def test_managed_thread_compact_archive_resume_lifecycle(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeTurnHandle:
        def __init__(self, turn_id: str, assistant_text: str) -> None:
            self.turn_id = turn_id
            self._assistant_text = assistant_text

        async def wait(self, timeout=None):
            _ = timeout
            return type(
                "Result",
                (),
                {
                    "agent_messages": [self._assistant_text],
                    "raw_events": [],
                    "errors": [],
                },
            )()

    class FakeClient:
        def __init__(self) -> None:
            self.resume_calls: list[str] = []
            self.thread_start_calls = 0
            self.turn_start_calls: list[dict[str, str]] = []

        async def thread_resume(self, thread_id: str) -> None:
            self.resume_calls.append(thread_id)

        async def thread_start(self, root: str) -> dict:
            _ = root
            self.thread_start_calls += 1
            return {"id": f"backend-thread-{self.thread_start_calls}"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = approval_policy, sandbox_policy, turn_kwargs
            self.turn_start_calls.append({"thread_id": thread_id, "prompt": prompt})
            idx = len(self.turn_start_calls)
            return FakeTurnHandle(
                turn_id=f"backend-turn-{idx}",
                assistant_text=f"assistant-{idx}",
            )

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    fake_supervisor = FakeSupervisor()
    app.state.app_server_supervisor = fake_supervisor
    app.state.app_server_events = object()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        first_msg = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "first user message"},
        )
        assert first_msg.status_code == 200
        assert first_msg.json()["backend_thread_id"] == "backend-thread-1"

        compact_summary = "A compact summary of earlier turns"
        compact_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/compact",
            json={"summary": compact_summary, "reset_backend": True},
        )
        assert compact_resp.status_code == 200

        store = PmaThreadStore(hub_env.hub_root)
        compacted_thread = store.get_thread(managed_thread_id)
        assert compacted_thread is not None
        assert compacted_thread["backend_thread_id"] is None
        assert compacted_thread["compact_seed"] == compact_summary

        second_message = "second user message"
        second_msg = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": second_message},
        )
        assert second_msg.status_code == 200
        assert second_msg.json()["backend_thread_id"] == "backend-thread-2"

        expected_compacted_prompt = (
            "Context summary (from compaction):\n"
            f"{compact_summary}\n\n"
            "User message:\n"
            f"{second_message}"
        )
        assert (
            fake_supervisor.client.turn_start_calls[1]["prompt"]
            == expected_compacted_prompt
        )

        archive_resp = client.post(f"/hub/pma/threads/{managed_thread_id}/archive")
        assert archive_resp.status_code == 200

        archived_thread = store.get_thread(managed_thread_id)
        assert archived_thread is not None
        assert archived_thread["status"] == "archived"

        blocked = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "blocked while archived"},
        )
        assert blocked.status_code == 409

        resume_backend_id = "backend-thread-manual"
        resume_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/resume",
            json={"backend_thread_id": resume_backend_id},
        )
        assert resume_resp.status_code == 200

        resumed_thread = store.get_thread(managed_thread_id)
        assert resumed_thread is not None
        assert resumed_thread["status"] == "active"
        assert resumed_thread["backend_thread_id"] == resume_backend_id

        resumed_msg = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "message after resume"},
        )
        assert resumed_msg.status_code == 200
        assert resumed_msg.json()["backend_thread_id"] == resume_backend_id
        assert fake_supervisor.client.resume_calls[-1] == resume_backend_id
        assert (
            fake_supervisor.client.turn_start_calls[2]["prompt"]
            == "message after resume"
        )
