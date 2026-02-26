from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.server import create_hub_app
from tests.conftest import write_test_config


def _enable_pma(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def test_managed_thread_turns_list_and_get(hub_env) -> None:
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
            self._thread_seq = 0
            self.turn_start_calls = 0

        async def thread_resume(self, thread_id: str) -> None:
            self.resume_calls.append(thread_id)

        async def thread_start(self, root: str) -> dict:
            _ = root
            self._thread_seq += 1
            return {"id": f"backend-thread-{self._thread_seq}"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            self.turn_start_calls += 1
            idx = self.turn_start_calls
            return FakeTurnHandle(
                turn_id=f"backend-turn-{idx}",
                assistant_text=f"assistant full output {idx}",
            )

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    app.state.app_server_supervisor = FakeSupervisor()
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
            json={"message": "first prompt content"},
        )
        assert first_msg.status_code == 200
        first_turn_id = first_msg.json()["managed_turn_id"]

        second_msg = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "second prompt content"},
        )
        assert second_msg.status_code == 200
        second_turn_id = second_msg.json()["managed_turn_id"]

        list_resp = client.get(f"/hub/pma/threads/{managed_thread_id}/turns")
        assert list_resp.status_code == 200
        turns = list_resp.json()["turns"]
        assert len(turns) == 2
        assert turns[0]["managed_turn_id"] == second_turn_id
        assert turns[1]["managed_turn_id"] == first_turn_id
        assert turns[0]["status"] == "ok"
        assert turns[0]["prompt_preview"] == "second prompt content"
        assert turns[0]["assistant_preview"] == "assistant full output 2"
        assert turns[0]["started_at"]
        assert turns[0]["finished_at"]

        get_resp = client.get(
            f"/hub/pma/threads/{managed_thread_id}/turns/{first_turn_id}"
        )
        assert get_resp.status_code == 200
        turn = get_resp.json()["turn"]
        assert turn["managed_turn_id"] == first_turn_id
        assert turn["prompt"] == "first prompt content"
        assert turn["assistant_text"] == "assistant full output 1"


def test_managed_thread_turns_not_found_for_thread(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        resp = client.get("/hub/pma/threads/missing-thread/turns")

    assert resp.status_code == 404
    assert "managed thread not found" in (resp.json().get("detail") or "").lower()


def test_managed_thread_turn_not_found_for_existing_thread(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        resp = client.get(f"/hub/pma/threads/{managed_thread_id}/turns/missing-turn-id")

    assert resp.status_code == 404
    assert "managed turn not found" in (resp.json().get("detail") or "").lower()
