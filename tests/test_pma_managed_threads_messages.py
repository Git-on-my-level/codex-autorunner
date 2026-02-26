from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.pma_thread_store import (
    ManagedThreadNotActiveError,
    PmaThreadStore,
)
from codex_autorunner.core.pma_transcripts import PmaTranscriptStore
from codex_autorunner.server import create_hub_app
from tests.conftest import write_test_config


def _enable_pma(
    hub_root: Path,
    *,
    model: str | None = None,
    reasoning: str | None = None,
    max_text_chars: int | None = None,
) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    if model is not None:
        cfg["pma"]["model"] = model
    if reasoning is not None:
        cfg["pma"]["reasoning"] = reasoning
    if max_text_chars is not None:
        cfg["pma"]["max_text_chars"] = max_text_chars
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def test_send_message_persists_turns_and_reuses_backend_thread(hub_env) -> None:
    _enable_pma(hub_env.hub_root, model="model-default", reasoning="high")
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
            self.thread_start_roots: list[str] = []
            self.turn_start_calls: list[dict[str, object]] = []
            self._thread_seq = 0

        async def thread_resume(self, thread_id: str) -> None:
            self.resume_calls.append(thread_id)

        async def thread_start(self, root: str) -> dict:
            self._thread_seq += 1
            thread_id = f"backend-thread-{self._thread_seq}"
            self.thread_start_roots.append(root)
            return {"id": thread_id}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            self.turn_start_calls.append(
                {
                    "thread_id": thread_id,
                    "prompt": prompt,
                    "approval_policy": approval_policy,
                    "sandbox_policy": sandbox_policy,
                    "turn_kwargs": dict(turn_kwargs),
                }
            )
            index = len(self.turn_start_calls)
            return FakeTurnHandle(
                turn_id=f"backend-turn-{index}",
                assistant_text=f"assistant-output-{index}",
            )

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()
            self.workspace_roots: list[Path] = []

        async def get_client(self, hub_root: Path):
            self.workspace_roots.append(hub_root)
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

        first_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "first prompt"},
        )
        assert first_resp.status_code == 200
        first_payload = first_resp.json()
        assert first_payload["status"] == "ok"
        assert first_payload["backend_thread_id"] == "backend-thread-1"
        assert first_payload["assistant_text"] == "assistant-output-1"
        assert first_payload["error"] is None

        second_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "second prompt"},
        )
        assert second_resp.status_code == 200
        second_payload = second_resp.json()
        assert second_payload["status"] == "ok"
        assert second_payload["backend_thread_id"] == "backend-thread-1"
        assert second_payload["assistant_text"] == "assistant-output-2"
        assert second_payload["error"] is None

    # First message creates the backend thread; second reuses it via resume.
    assert fake_supervisor.client.thread_start_roots == [
        str(hub_env.repo_root.resolve())
    ]
    assert fake_supervisor.client.resume_calls == ["backend-thread-1"]
    assert all(
        root == hub_env.repo_root.resolve() for root in fake_supervisor.workspace_roots
    )
    assert len(fake_supervisor.client.turn_start_calls) == 2
    assert fake_supervisor.client.turn_start_calls[0]["turn_kwargs"] == {
        "model": "model-default",
        "effort": "high",
    }

    store = PmaThreadStore(hub_env.hub_root)
    thread = store.get_thread(managed_thread_id)
    assert thread is not None
    assert thread["backend_thread_id"] == "backend-thread-1"
    assert thread["last_turn_id"] == second_payload["managed_turn_id"]
    assert thread["last_message_preview"] == "second prompt"

    turns = store.list_turns(managed_thread_id, limit=10)
    assert len(turns) == 2
    by_id = {turn["managed_turn_id"]: turn for turn in turns}

    first_turn = by_id[first_payload["managed_turn_id"]]
    assert first_turn["status"] == "ok"
    assert first_turn["assistant_text"] == "assistant-output-1"
    assert first_turn["backend_turn_id"] == "backend-turn-1"
    assert first_turn["transcript_turn_id"] == first_payload["managed_turn_id"]

    second_turn = by_id[second_payload["managed_turn_id"]]
    assert second_turn["status"] == "ok"
    assert second_turn["assistant_text"] == "assistant-output-2"
    assert second_turn["backend_turn_id"] == "backend-turn-2"
    assert second_turn["transcript_turn_id"] == second_payload["managed_turn_id"]

    transcripts = PmaTranscriptStore(hub_env.hub_root)
    transcript = transcripts.read_transcript(first_payload["managed_turn_id"])
    assert transcript is not None
    metadata = transcript["metadata"]
    assert metadata["managed_thread_id"] == managed_thread_id
    assert metadata["managed_turn_id"] == first_payload["managed_turn_id"]
    assert metadata["repo_id"] == hub_env.repo_id
    assert metadata["workspace_root"] == str(hub_env.repo_root.resolve())
    assert metadata["agent"] == "codex"
    assert metadata["backend_thread_id"] == "backend-thread-1"
    assert metadata["backend_turn_id"] == "backend-turn-1"
    assert metadata["model"] == "model-default"
    assert metadata["reasoning"] == "high"
    assert transcript["content"].strip() == "assistant-output-1"


def test_send_message_rejects_archived_thread(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

    store = PmaThreadStore(hub_env.hub_root)
    store.archive_thread(managed_thread_id)

    with TestClient(app) as client:
        resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "should fail"},
        )

    assert resp.status_code == 409
    assert "archived" in (resp.json().get("detail") or "").lower()


def test_send_message_rejects_when_running_turn_exists(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

    store = PmaThreadStore(hub_env.hub_root)
    _ = store.create_turn(managed_thread_id, prompt="still running")

    with TestClient(app) as client:
        resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "blocked"},
        )

    assert resp.status_code == 409
    assert "running turn" in (resp.json().get("detail") or "").lower()


def test_send_message_handles_not_active_race(hub_env, monkeypatch) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

    def _raise_not_active(
        self,
        managed_thread_id: str,
        *,
        prompt: str,
        model: str | None = None,
        reasoning: str | None = None,
        client_turn_id: str | None = None,
    ):
        _ = self, prompt, model, reasoning, client_turn_id
        raise ManagedThreadNotActiveError(managed_thread_id, "archived")

    monkeypatch.setattr(PmaThreadStore, "create_turn", _raise_not_active)

    with TestClient(app) as client:
        resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "should fail"},
        )

    assert resp.status_code == 409
    assert resp.json().get("detail") == "Managed thread is archived and read-only"


def test_send_message_rejects_oversize_message(hub_env) -> None:
    _enable_pma(hub_env.hub_root, max_text_chars=5)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "toolong"},
        )

    assert resp.status_code == 400
    assert "max_text_chars" in (resp.json().get("detail") or "")

    store = PmaThreadStore(hub_env.hub_root)
    assert store.list_turns(managed_thread_id, limit=10) == []


def test_send_message_finalizes_turn_when_transcript_write_fails(
    hub_env, monkeypatch
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeTurnHandle:
        turn_id = "backend-turn-1"

        async def wait(self, timeout=None):
            _ = timeout
            return type(
                "Result",
                (),
                {
                    "agent_messages": ["assistant-output"],
                    "raw_events": [],
                    "errors": [],
                },
            )()

    class FakeClient:
        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "backend-thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        async def get_client(self, hub_root: Path):
            _ = hub_root
            return FakeClient()

    def _raise_transcript_write(*args, **kwargs):
        _ = args, kwargs
        raise RuntimeError("disk-full-secret")

    monkeypatch.setattr(PmaTranscriptStore, "write_transcript", _raise_transcript_write)
    app.state.app_server_supervisor = FakeSupervisor()
    app.state.app_server_events = object()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        first_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "first"},
        )
        assert first_resp.status_code == 200
        first_payload = first_resp.json()
        assert first_payload["status"] == "ok"

        second_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "second"},
        )
        assert second_resp.status_code == 200
        assert second_resp.json()["status"] == "ok"

    store = PmaThreadStore(hub_env.hub_root)
    assert not store.has_running_turn(managed_thread_id)
    first_turn = store.get_turn(managed_thread_id, first_payload["managed_turn_id"])
    assert first_turn is not None
    assert first_turn["status"] == "ok"
    assert first_turn["transcript_turn_id"] is None


def test_send_message_does_not_report_ok_when_turn_already_interrupted(
    hub_env, monkeypatch
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeTurnHandle:
        turn_id = "backend-turn-1"

        async def wait(self, timeout=None):
            _ = timeout
            return type(
                "Result",
                (),
                {
                    "agent_messages": ["assistant-output"],
                    "raw_events": [],
                    "errors": [],
                },
            )()

    class FakeClient:
        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "backend-thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            return FakeTurnHandle()

    class FakeSupervisor:
        async def get_client(self, hub_root: Path):
            _ = hub_root
            return FakeClient()

    original_mark_turn_finished = PmaThreadStore.mark_turn_finished

    def _interrupt_before_success_finalize(
        self,
        managed_turn_id: str,
        *,
        status: str,
        assistant_text=None,
        error=None,
        backend_turn_id=None,
        transcript_turn_id=None,
    ) -> None:
        if status == "ok":
            self.mark_turn_interrupted(managed_turn_id)
        original_mark_turn_finished(
            self,
            managed_turn_id,
            status=status,
            assistant_text=assistant_text,
            error=error,
            backend_turn_id=backend_turn_id,
            transcript_turn_id=transcript_turn_id,
        )

    monkeypatch.setattr(
        PmaThreadStore,
        "mark_turn_finished",
        _interrupt_before_success_finalize,
    )
    app.state.app_server_supervisor = FakeSupervisor()
    app.state.app_server_events = object()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        message_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "first"},
        )

    assert message_resp.status_code == 200
    payload = message_resp.json()
    assert payload["status"] == "interrupted"
    assert payload["error"] == "PMA chat interrupted"

    store = PmaThreadStore(hub_env.hub_root)
    turn = store.get_turn(managed_thread_id, payload["managed_turn_id"])
    assert turn is not None
    assert turn["status"] == "interrupted"
    thread = store.get_thread(managed_thread_id)
    assert thread is not None
    assert thread["last_turn_id"] is None
    assert thread["last_message_preview"] is None


def test_send_message_sanitizes_unexpected_execution_errors(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    class FakeClient:
        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "backend-thread-1"}

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, prompt, approval_policy, sandbox_policy, turn_kwargs
            raise RuntimeError("sensitive-backend-message")

    class FakeSupervisor:
        async def get_client(self, hub_root: Path):
            _ = hub_root
            return FakeClient()

    app.state.app_server_supervisor = FakeSupervisor()
    app.state.app_server_events = object()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", "repo_id": hub_env.repo_id},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        message_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "trigger failure"},
        )

    assert message_resp.status_code == 200
    payload = message_resp.json()
    assert payload["status"] == "error"
    assert payload["error"] == "Managed thread execution failed"
    assert "sensitive-backend-message" not in payload["error"]

    store = PmaThreadStore(hub_env.hub_root)
    turn = store.get_turn(managed_thread_id, payload["managed_turn_id"])
    assert turn is not None
    assert turn["status"] == "error"
    assert turn["error"] == "Managed thread execution failed"
