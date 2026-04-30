from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration.runtime_bindings import (
    clear_runtime_thread_binding,
)
from codex_autorunner.core.pma_thread_store import (
    ManagedThreadNotActiveError,
    PmaThreadStore,
)
from codex_autorunner.core.pma_transcripts import PmaTranscriptStore
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.telegram.state import TelegramStateStore
from codex_autorunner.server import create_hub_app
from tests.conftest import write_test_config
from tests.pma_support import (
    _bind_thread_to_discord,
    _bind_thread_to_telegram,
    _enable_pma,
    _repo_owner,
)
from tests.pma_support.managed_threads import (
    FakeAutomationStore,
    FakeClient,
    FakeSupervisor,
    install_fake_supervisor,
)

pytestmark = pytest.mark.slow


def test_send_message_persists_turns_and_reuses_backend_thread(hub_env) -> None:
    _enable_pma(
        hub_env.hub_root,
        model="model-default",
        reasoning="high",
        reactive_enabled=False,
        managed_thread_terminal_followup_default=False,
    )
    app = create_hub_app(hub_env.hub_root)

    fake_client = FakeClient(sequential=True)
    fake_supervisor = FakeSupervisor(fake_client)

    with TestClient(app) as client:
        app.state.app_server_supervisor = fake_supervisor
        app.state.app_server_events = object()
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
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
    repo_root = str(hub_env.repo_root.resolve())
    assert [
        root for root in fake_supervisor.client.thread_start_roots if root == repo_root
    ] == [repo_root]
    assert fake_supervisor.client.resume_calls == ["backend-thread-1"]
    assert [
        root
        for root in fake_supervisor.workspace_roots
        if root == hub_env.repo_root.resolve()
    ]
    assert len(fake_supervisor.client.turn_start_calls) == 2
    assert fake_supervisor.client.turn_start_calls[0]["turn_kwargs"] == {
        "model": "model-default",
        "effort": "high",
    }
    first_prompt = str(fake_supervisor.client.turn_start_calls[0]["prompt"])
    second_prompt = str(fake_supervisor.client.turn_start_calls[1]["prompt"])
    assert "Ops guide: `.codex-autorunner/pma/docs/ABOUT_CAR.md`." in first_prompt
    assert "<pma_workspace_docs>" in first_prompt
    assert "<user_message>" in first_prompt
    assert "first prompt" in first_prompt
    assert "second prompt" in second_prompt

    store = PmaThreadStore(hub_env.hub_root)
    thread = store.get_thread(managed_thread_id)
    assert thread is not None
    runtime_binding = store.get_thread_runtime_binding(managed_thread_id)
    assert runtime_binding is not None
    assert runtime_binding.backend_thread_id == "backend-thread-1"
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
    assert metadata["resource_kind"] == "repo"
    assert metadata["resource_id"] == hub_env.repo_id
    assert metadata["workspace_root"] == str(hub_env.repo_root.resolve())
    assert metadata["agent"] == "codex"
    assert metadata["backend_thread_id"] == "backend-thread-1"
    assert metadata["backend_turn_id"] == "backend-turn-1"
    assert metadata["model"] == "model-default"
    assert metadata["reasoning"] == "high"
    assert transcript["content"].strip() == "assistant-output-1"


def test_send_message_resolves_alias_backed_hermes_profile_runtime(
    hub_env, monkeypatch
) -> None:
    _enable_pma(hub_env.hub_root)
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["enabled"] = True
    cfg.setdefault("agents", {})
    cfg["agents"]["hermes"] = {"binary": "hermes"}
    cfg["agents"]["hermes-m4-pma"] = {
        "backend": "hermes",
        "binary": "hermes-m4-pma",
    }
    write_test_config(hub_env.hub_root / CONFIG_FILENAME, cfg)

    observed_supervisor_args: list[tuple[str, str | None]] = []

    class _FakeHermesSupervisor:
        async def ensure_ready(self, workspace_root: Path) -> None:
            _ = workspace_root

        async def create_session(self, workspace_root: Path, title: str | None = None):
            _ = workspace_root, title
            return SimpleNamespace(session_id="hermes-session-1")

        async def resume_session(self, workspace_root: Path, conversation_id: str):
            _ = workspace_root
            return SimpleNamespace(session_id=conversation_id)

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            *,
            model: str | None = None,
            approval_mode: str | None = None,
        ) -> str:
            _ = workspace_root, conversation_id, prompt, model, approval_mode
            return "hermes-turn-1"

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
            *,
            timeout: float | None = None,
        ):
            _ = workspace_root, conversation_id, turn_id, timeout
            return SimpleNamespace(
                status="ok",
                assistant_text="hermes-output",
                raw_events=[],
                errors=[],
            )

        async def interrupt_turn(
            self, workspace_root: Path, conversation_id: str, turn_id: str | None
        ) -> None:
            _ = workspace_root, conversation_id, turn_id

        async def stream_turn_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield {}

        async def list_turn_events_snapshot(
            self, turn_id: str
        ) -> list[dict[str, object]]:
            _ = turn_id
            return []

    def _fake_build_supervisor(
        _config,
        *,
        agent_id: str = "hermes",
        profile: str | None = None,
        **_kwargs,
    ):
        observed_supervisor_args.append((agent_id, profile))
        return _FakeHermesSupervisor()

    monkeypatch.setattr(
        "codex_autorunner.agents.registry.build_hermes_supervisor_from_config",
        _fake_build_supervisor,
    )

    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "agent": "hermes",
                "profile": "m4-pma",
                **_repo_owner(hub_env),
            },
        )
        assert create_resp.status_code == 200
        thread = create_resp.json()["thread"]
        assert thread["agent"] == "hermes"
        assert thread["agent_profile"] == "m4-pma"
        managed_thread_id = thread["managed_thread_id"]

        message_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "hello hermes"},
        )

    assert message_resp.status_code == 200
    payload = message_resp.json()
    assert payload["status"] == "ok"
    assert payload["assistant_text"] == "hermes-output"
    assert payload["backend_thread_id"] == "hermes-session-1"
    assert observed_supervisor_args == [("hermes", "m4-pma")]


@pytest.mark.anyio
async def test_send_message_enqueues_assistant_output_to_bound_chat_outboxes(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root, managed_thread_terminal_followup_default=False)
    app = create_hub_app(hub_env.hub_root)
    install_fake_supervisor(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        create_resp = await client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        await _bind_thread_to_discord(
            hub_env,
            managed_thread_id=managed_thread_id,
            channel_id="discord-123",
        )
        await _bind_thread_to_telegram(
            hub_env,
            managed_thread_id=managed_thread_id,
            chat_id=1001,
            thread_id=2002,
        )

        message_resp = await client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "cross-surface prompt"},
        )

    assert message_resp.status_code == 200
    payload = message_resp.json()
    assert payload["status"] == "ok"
    assert payload["assistant_text"] == "assistant-output"

    discord_store = DiscordStateStore(
        hub_env.hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    telegram_store = TelegramStateStore(
        hub_env.hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        discord_outbox = await discord_store.list_outbox()
        telegram_outbox = await telegram_store.list_outbox()
    finally:
        await discord_store.close()
        await telegram_store.close()

    assert any(
        record.channel_id == "discord-123"
        and record.payload_json.get("content") == "assistant-output"
        for record in discord_outbox
    )
    assert any(
        record.chat_id == 1001
        and record.thread_id == 2002
        and record.text == "assistant-output"
        for record in telegram_outbox
    )


@pytest.mark.anyio
async def test_send_message_continues_bound_chat_delivery_after_one_surface_fails(
    hub_env,
    monkeypatch,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    async def _fail_discord_enqueue(self, record):
        _ = self, record
        raise RuntimeError("discord enqueue failed")

    install_fake_supervisor(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        create_resp = await client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        await _bind_thread_to_discord(
            hub_env,
            managed_thread_id=managed_thread_id,
            channel_id="discord-123",
        )
        await _bind_thread_to_telegram(
            hub_env,
            managed_thread_id=managed_thread_id,
            chat_id=1001,
            thread_id=2002,
        )
        monkeypatch.setattr(
            DiscordStateStore,
            "enqueue_outbox",
            _fail_discord_enqueue,
        )

        message_resp = await client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "cross-surface prompt"},
        )

    assert message_resp.status_code == 200
    assert message_resp.json()["status"] == "ok"

    telegram_store = TelegramStateStore(
        hub_env.hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        telegram_outbox = await telegram_store.list_outbox()
    finally:
        await telegram_store.close()

    assert any(
        record.chat_id == 1001
        and record.thread_id == 2002
        and record.text == "assistant-output"
        for record in telegram_outbox
    )


def test_send_message_rejects_archived_thread(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
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


def test_send_message_rejects_legacy_background_alias(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "legacy flag", "background": True},
        )

    assert resp.status_code == 422
    assert "background" in str(resp.json())


def test_send_message_compact_seed_used_only_before_backend_thread_exists(
    hub_env,
) -> None:
    _enable_pma(
        hub_env.hub_root,
        reactive_enabled=False,
        managed_thread_terminal_followup_default=False,
    )
    app = create_hub_app(hub_env.hub_root)

    fake_client = FakeClient()
    fake_supervisor = FakeSupervisor(fake_client)

    with TestClient(app) as client:
        app.state.app_server_supervisor = fake_supervisor
        app.state.app_server_events = object()
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        compact_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/compact",
            json={"summary": "summary seed"},
        )
        assert compact_resp.status_code == 200

        first_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "first message"},
        )
        assert first_resp.status_code == 200
        assert first_resp.json()["status"] == "ok"

        second_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "second message"},
        )
        assert second_resp.status_code == 200
        assert second_resp.json()["status"] == "ok"

    relevant_prompts = [
        call["prompt"]
        for call in fake_supervisor.client.turn_start_calls
        if "first message" in call["prompt"] or "second message" in call["prompt"]
    ]
    assert len(relevant_prompts) == 2
    first_prompt, second_prompt = relevant_prompts
    assert "Ops guide: `.codex-autorunner/pma/docs/ABOUT_CAR.md`." in first_prompt
    assert "<user_message>" in first_prompt
    assert "Context summary (from compaction):" in first_prompt
    assert "summary seed" in first_prompt
    assert "User message:\nfirst message" in first_prompt
    assert "Context summary (from compaction):" not in second_prompt
    assert "second message" in second_prompt


def test_send_message_after_restart_does_not_duplicate_compact_seed(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    fake_client = FakeClient(sequential=True)
    fake_supervisor = FakeSupervisor(fake_client)
    app.state.app_server_supervisor = fake_supervisor
    app.state.app_server_events = object()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        compact_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/compact",
            json={"summary": "summary seed"},
        )
        assert compact_resp.status_code == 200

        clear_runtime_thread_binding(hub_env.hub_root, managed_thread_id)

        resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "message after restart"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    prompt = fake_supervisor.client.turn_start_calls[0]["prompt"]
    assert prompt.count("Context summary (from compaction):") == 1
    assert "Compacted context summary:" not in prompt
    assert "message after restart" in prompt


def test_send_message_queues_when_running_turn_exists(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]
        running_turn = store.create_turn(managed_thread_id, prompt="still running")
        resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "blocked"},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["send_state"] == "queued"
    assert payload["execution_state"] == "queued"
    assert payload["active_managed_turn_id"] == running_turn["managed_turn_id"]
    assert payload["queue_depth"] == 1

    queued_turn = store.get_turn(managed_thread_id, payload["managed_turn_id"])
    assert queued_turn is not None
    assert queued_turn["status"] == "queued"
    queued_items = store.list_pending_turn_queue_items(managed_thread_id)
    assert len(queued_items) == 1
    assert queued_items[0]["managed_turn_id"] == payload["managed_turn_id"]


def test_send_message_rejects_when_running_turn_exists_if_busy_reject(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]
        running_turn = store.create_turn(managed_thread_id, prompt="still running")
        resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "blocked", "busy_policy": "reject"},
        )

    assert resp.status_code == 409
    assert "running turn" in (resp.json().get("detail") or "").lower()
    assert resp.json().get("send_state") == "already_in_flight"
    assert resp.json().get("managed_turn_id") == running_turn["managed_turn_id"]


def test_send_message_reports_interrupt_failure_without_marking_turn_failed(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    install_fake_supervisor(app)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]
        store = PmaThreadStore(hub_env.hub_root)
        running_turn = store.create_turn(managed_thread_id, prompt="still running")
        store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
        store.set_turn_backend_turn_id(
            running_turn["managed_turn_id"],
            "backend-turn-1",
        )
        resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "blocked", "busy_policy": "interrupt"},
        )

    assert resp.status_code == 409
    payload = resp.json()
    assert payload["status"] == "error"
    assert payload["send_state"] == "rejected"
    assert payload["interrupt_state"] == "failed"
    assert payload["active_turn_status"] == "running"
    assert payload["active_managed_turn_id"] == running_turn["managed_turn_id"]
    assert "still running" in payload["detail"].lower()

    updated_turn = store.get_turn(managed_thread_id, running_turn["managed_turn_id"])
    assert updated_turn is not None
    assert updated_turn["status"] == "running"
    assert updated_turn["finished_at"] is None


def test_send_message_handles_not_active_race(hub_env, monkeypatch) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

    def _raise_not_active(
        self,
        managed_thread_id: str,
        *,
        prompt: str,
        request_kind: str = "message",
        busy_policy: str = "reject",
        model: str | None = None,
        reasoning: str | None = None,
        client_turn_id: str | None = None,
        queue_payload: dict[str, object] | None = None,
    ):
        _ = (
            self,
            prompt,
            request_kind,
            busy_policy,
            model,
            reasoning,
            client_turn_id,
            queue_payload,
        )
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
            json={"agent": "codex", **_repo_owner(hub_env)},
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

    def _raise_transcript_write(*args, **kwargs):
        _ = args, kwargs
        raise RuntimeError("disk-full-secret")

    monkeypatch.setattr(PmaTranscriptStore, "write_transcript", _raise_transcript_write)
    install_fake_supervisor(app)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
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
    install_fake_supervisor(app)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
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
    assert thread["last_turn_id"] == payload["managed_turn_id"]
    assert thread["last_message_preview"] == "first"


def test_send_message_sanitizes_unexpected_execution_errors(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    install_fake_supervisor(
        app, client=FakeClient(turn_error=RuntimeError("sensitive-backend-message"))
    )

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
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


def test_send_message_notifies_automation_on_completion(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    fake_store = FakeAutomationStore()
    install_fake_supervisor(app)
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        message_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "trigger completion"},
        )
        assert message_resp.status_code == 200
        assert message_resp.json()["status"] == "ok"

    assert len(fake_store.transitions) == 1
    transition = fake_store.transitions[0]
    assert transition["thread_id"] == managed_thread_id
    assert transition["repo_id"] == hub_env.repo_id
    assert transition["resource_kind"] == "repo"
    assert transition["resource_id"] == hub_env.repo_id
    assert transition["from_state"] == "running"
    assert transition["to_state"] == "completed"
    assert transition["reason"] == "managed_turn_completed"
    assert isinstance(transition.get("timestamp"), str)
    assert str(transition.get("timestamp"))


def test_send_message_notifies_automation_on_failure(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)

    fake_store = FakeAutomationStore()
    install_fake_supervisor(
        app, client=FakeClient(turn_error=RuntimeError("sensitive-backend-message"))
    )
    app.state.hub_supervisor.get_pma_automation_store = lambda: fake_store

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        message_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "trigger failure"},
        )
        assert message_resp.status_code == 200
        assert message_resp.json()["status"] == "error"

    assert len(fake_store.transitions) == 1
    transition = fake_store.transitions[0]
    assert transition["thread_id"] == managed_thread_id
    assert transition["repo_id"] == hub_env.repo_id
    assert transition["resource_kind"] == "repo"
    assert transition["resource_id"] == hub_env.repo_id
    assert transition["from_state"] == "running"
    assert transition["to_state"] == "failed"
    assert transition["reason"] == "Managed thread execution failed"
    assert isinstance(transition.get("timestamp"), str)
    assert str(transition.get("timestamp"))


def test_send_message_defaults_agent_from_agent_workspace_runtime(hub_env) -> None:
    _enable_pma(
        hub_env.hub_root,
        reactive_enabled=False,
        managed_thread_terminal_followup_default=False,
    )
    app = create_hub_app(hub_env.hub_root)
    workspace = app.state.hub_supervisor.create_agent_workspace(
        workspace_id="zc-main",
        runtime="zeroclaw",
        display_name="ZeroClaw Main",
    )

    class FakeZeroClawSupervisor:
        def __init__(self) -> None:
            self.create_calls: list[tuple[Path, str | None]] = []
            self.attach_calls: list[tuple[Path, str]] = []
            self.turn_calls: list[dict[str, object]] = []

        async def create_session(
            self, workspace_root: Path, title: str | None = None
        ) -> str:
            self.create_calls.append((workspace_root, title))
            return "zeroclaw-session-1"

        async def attach_session(self, workspace_root: Path, session_id: str) -> str:
            self.attach_calls.append((workspace_root, session_id))
            return session_id

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            *,
            model: str | None = None,
        ) -> str:
            self.turn_calls.append(
                {
                    "workspace_root": workspace_root,
                    "conversation_id": conversation_id,
                    "prompt": prompt,
                    "model": model,
                }
            )
            return f"zeroclaw-turn-{len(self.turn_calls)}"

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
            *,
            timeout: float | None = None,
        ):
            _ = workspace_root, conversation_id, turn_id, timeout
            return type(
                "Result",
                (),
                {
                    "status": "ok",
                    "assistant_text": f"zeroclaw-output-{len(self.turn_calls)}",
                    "raw_events": [],
                    "errors": [],
                },
            )()

        async def stream_turn_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield None

    app.state.zeroclaw_supervisor = FakeZeroClawSupervisor()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "resource_kind": "agent_workspace",
                "resource_id": workspace.id,
                "name": "ZeroClaw thread",
            },
        )
        assert create_resp.status_code == 200
        created_thread = create_resp.json()["thread"]
        assert created_thread["agent"] == "zeroclaw"
        managed_thread_id = created_thread["managed_thread_id"]

        first_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "first zeroclaw prompt"},
        )
        assert first_resp.status_code == 200
        first_payload = first_resp.json()
        assert first_payload["status"] == "ok"
        assert first_payload["backend_thread_id"] == "zeroclaw-session-1"

        second_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "second zeroclaw prompt"},
        )
        assert second_resp.status_code == 200
        second_payload = second_resp.json()
        assert second_payload["status"] == "ok"
        assert second_payload["backend_thread_id"] == "zeroclaw-session-1"

    fake_supervisor = app.state.zeroclaw_supervisor
    assert fake_supervisor.create_calls == [
        (workspace.path.resolve(), "ZeroClaw thread")
    ]
    assert fake_supervisor.attach_calls == [
        (workspace.path.resolve(), "zeroclaw-session-1")
    ]
    assert len(fake_supervisor.turn_calls) == 2
    assert fake_supervisor.turn_calls[0]["conversation_id"] == "zeroclaw-session-1"
    first_prompt = str(fake_supervisor.turn_calls[0]["prompt"])
    second_prompt = str(fake_supervisor.turn_calls[1]["prompt"])
    assert first_prompt == "first zeroclaw prompt"
    assert second_prompt == "second zeroclaw prompt"
    assert "Ops guide: `.codex-autorunner/pma/docs/ABOUT_CAR.md`." not in first_prompt
    assert "Ops guide: `.codex-autorunner/pma/docs/ABOUT_CAR.md`." not in second_prompt
    assert "<pma_workspace_docs>" not in first_prompt
    assert "<user_message>" not in first_prompt

    store = PmaThreadStore(hub_env.hub_root)
    thread = store.get_thread(managed_thread_id)
    assert thread is not None
    assert thread["agent"] == "zeroclaw"
    assert thread["resource_kind"] == "agent_workspace"
    assert thread["resource_id"] == workspace.id
    runtime_binding = store.get_thread_runtime_binding(managed_thread_id)
    assert runtime_binding is not None
    assert runtime_binding.backend_thread_id == "zeroclaw-session-1"
    assert thread["last_turn_id"] == second_payload["managed_turn_id"]

    transcript = PmaTranscriptStore(hub_env.hub_root).read_transcript(
        first_payload["managed_turn_id"]
    )
    assert transcript is not None
    metadata = transcript["metadata"]
    assert metadata["agent"] == "zeroclaw"
    assert metadata.get("repo_id") is None
    assert metadata["resource_kind"] == "agent_workspace"
    assert metadata["resource_id"] == workspace.id
    assert metadata["workspace_root"] == str(workspace.path.resolve())
    assert metadata["backend_thread_id"] == "zeroclaw-session-1"


def test_zeroclaw_managed_threads_keep_compaction_seed_without_pma_discoverability(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    workspace = app.state.hub_supervisor.create_agent_workspace(
        workspace_id="zc-main",
        runtime="zeroclaw",
        display_name="ZeroClaw Main",
    )

    class FakeZeroClawSupervisor:
        def __init__(self) -> None:
            self.turn_calls: list[dict[str, object]] = []

        async def create_session(
            self, workspace_root: Path, title: str | None = None
        ) -> str:
            _ = workspace_root, title
            return "zeroclaw-session-1"

        async def attach_session(self, workspace_root: Path, session_id: str) -> str:
            _ = workspace_root
            return session_id

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            *,
            model: str | None = None,
        ) -> str:
            self.turn_calls.append(
                {
                    "workspace_root": workspace_root,
                    "conversation_id": conversation_id,
                    "prompt": prompt,
                    "model": model,
                }
            )
            return f"zeroclaw-turn-{len(self.turn_calls)}"

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
            *,
            timeout: float | None = None,
        ):
            _ = workspace_root, conversation_id, turn_id, timeout
            return type(
                "Result",
                (),
                {
                    "status": "ok",
                    "assistant_text": f"zeroclaw-output-{len(self.turn_calls)}",
                    "raw_events": [],
                    "errors": [],
                },
            )()

        async def stream_turn_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield None

    app.state.zeroclaw_supervisor = FakeZeroClawSupervisor()

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={
                "resource_kind": "agent_workspace",
                "resource_id": workspace.id,
                "name": "ZeroClaw thread",
            },
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        compact_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/compact",
            json={"summary": "summary seed"},
        )
        assert compact_resp.status_code == 200

        first_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "first zeroclaw prompt"},
        )
        assert first_resp.status_code == 200
        assert first_resp.json()["status"] == "ok"

    first_prompt = str(app.state.zeroclaw_supervisor.turn_calls[0]["prompt"])
    assert "Context summary (from compaction):" in first_prompt
    assert "summary seed" in first_prompt
    assert "User message:\nfirst zeroclaw prompt" in first_prompt
    assert "Ops guide: `.codex-autorunner/pma/docs/ABOUT_CAR.md`." not in first_prompt
    assert "<pma_workspace_docs>" not in first_prompt
    assert "<user_message>" not in first_prompt


@pytest.mark.anyio
async def test_agent_workspace_threads_run_in_parallel_without_workspace_wide_queueing(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    workspace = app.state.hub_supervisor.create_agent_workspace(
        workspace_id="zc-main",
        runtime="zeroclaw",
        display_name="ZeroClaw Main",
    )

    class FakeZeroClawSupervisor:
        def __init__(self) -> None:
            self.create_calls: list[tuple[Path, str | None]] = []
            self.turn_calls: list[dict[str, object]] = []
            self.wait_started: list[str] = []
            self._session_seq = 0
            self._session_events: dict[str, asyncio.Event] = {}

        async def create_session(
            self, workspace_root: Path, title: str | None = None
        ) -> str:
            self.create_calls.append((workspace_root, title))
            self._session_seq += 1
            session_id = f"zeroclaw-session-{self._session_seq}"
            self._session_events[session_id] = asyncio.Event()
            return session_id

        async def attach_session(self, workspace_root: Path, session_id: str) -> str:
            _ = workspace_root
            return session_id

        async def start_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            prompt: str,
            *,
            model: str | None = None,
        ) -> str:
            self.turn_calls.append(
                {
                    "workspace_root": workspace_root,
                    "conversation_id": conversation_id,
                    "prompt": prompt,
                    "model": model,
                }
            )
            return f"{conversation_id}-turn-{len(self.turn_calls)}"

        async def wait_for_turn(
            self,
            workspace_root: Path,
            conversation_id: str,
            turn_id: str,
            *,
            timeout: float | None = None,
        ):
            _ = workspace_root, turn_id, timeout
            self.wait_started.append(conversation_id)
            await self._session_events[conversation_id].wait()
            return type(
                "Result",
                (),
                {
                    "status": "ok",
                    "assistant_text": f"zeroclaw-output:{conversation_id}",
                    "raw_events": [],
                    "errors": [],
                },
            )()

        async def stream_turn_events(
            self, workspace_root: Path, conversation_id: str, turn_id: str
        ):
            _ = workspace_root, conversation_id, turn_id
            if False:
                yield None

    fake_supervisor = FakeZeroClawSupervisor()
    app.state.zeroclaw_supervisor = fake_supervisor

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        create_a = await client.post(
            "/hub/pma/threads",
            json={
                "resource_kind": "agent_workspace",
                "resource_id": workspace.id,
                "name": "ZeroClaw thread A",
            },
        )
        create_b = await client.post(
            "/hub/pma/threads",
            json={
                "resource_kind": "agent_workspace",
                "resource_id": workspace.id,
                "name": "ZeroClaw thread B",
            },
        )
        assert create_a.status_code == 200
        assert create_b.status_code == 200
        thread_a = create_a.json()["thread"]["managed_thread_id"]
        thread_b = create_b.json()["thread"]["managed_thread_id"]

        first_resp = await client.post(
            f"/hub/pma/threads/{thread_a}/messages",
            json={"message": "first zeroclaw thread", "defer_execution": True},
        )
        second_resp = await client.post(
            f"/hub/pma/threads/{thread_b}/messages",
            json={"message": "second zeroclaw thread", "defer_execution": True},
        )
        assert first_resp.status_code == 200
        assert second_resp.status_code == 200

        first_payload = first_resp.json()
        second_payload = second_resp.json()
        assert first_payload["send_state"] == "accepted"
        assert second_payload["send_state"] == "accepted"
        assert first_payload["execution_state"] == "running"
        assert second_payload["execution_state"] == "running"
        assert first_payload["backend_thread_id"] != second_payload["backend_thread_id"]

        store = PmaThreadStore(hub_env.hub_root)
        with anyio.fail_after(2):
            while len(fake_supervisor.wait_started) < 2:
                await anyio.sleep(0.05)

        first_turn = store.get_turn(thread_a, first_payload["managed_turn_id"])
        second_turn = store.get_turn(thread_b, second_payload["managed_turn_id"])
        assert first_turn is not None
        assert second_turn is not None
        assert first_turn["status"] == "running"
        assert second_turn["status"] == "running"
        assert store.get_queue_depth(thread_a) == 0
        assert store.get_queue_depth(thread_b) == 0
        assert fake_supervisor.create_calls == [
            (workspace.path.resolve(), "ZeroClaw thread A"),
            (workspace.path.resolve(), "ZeroClaw thread B"),
        ]

        fake_supervisor._session_events[first_payload["backend_thread_id"]].set()
        fake_supervisor._session_events[second_payload["backend_thread_id"]].set()

        with anyio.fail_after(2):
            while True:
                first_turn = store.get_turn(thread_a, first_payload["managed_turn_id"])
                second_turn = store.get_turn(
                    thread_b, second_payload["managed_turn_id"]
                )
                if (
                    first_turn is not None
                    and second_turn is not None
                    and first_turn.get("status") == "ok"
                    and second_turn.get("status") == "ok"
                ):
                    break
                await anyio.sleep(0.05)

    first_thread_row = PmaThreadStore(hub_env.hub_root).get_thread(thread_a)
    second_thread_row = PmaThreadStore(hub_env.hub_root).get_thread(thread_b)
    assert first_thread_row is not None
    assert second_thread_row is not None
    assert first_thread_row["resource_kind"] == "agent_workspace"
    assert second_thread_row["resource_kind"] == "agent_workspace"
    assert first_thread_row["resource_id"] == workspace.id
    assert second_thread_row["resource_id"] == workspace.id
    assert (
        PmaTranscriptStore(hub_env.hub_root)
        .read_transcript(first_payload["managed_turn_id"])["content"]
        .strip()
        == "zeroclaw-output:zeroclaw-session-1"
    )
    assert (
        PmaTranscriptStore(hub_env.hub_root)
        .read_transcript(second_payload["managed_turn_id"])["content"]
        .strip()
        == "zeroclaw-output:zeroclaw-session-2"
    )


def test_managed_thread_completion_subscription_enqueues_wakeup(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    install_fake_supervisor(app)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        sub_resp = client.post(
            "/hub/pma/subscriptions",
            json={
                "event_types": ["managed_thread_completed"],
                "thread_id": managed_thread_id,
                "from_state": "running",
                "to_state": "completed",
                "lane_id": "pma:lane-next",
                "idempotency_key": "managed-completion-sub",
            },
        )
        assert sub_resp.status_code == 200

        message_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "trigger completion"},
        )
        assert message_resp.status_code == 200
        assert message_resp.json()["status"] == "ok"

    automation_store = app.state.hub_supervisor.get_pma_automation_store()
    assert automation_store.list_pending_wakeups(limit=10) == []
    dispatched = automation_store.list_wakeups(state_filter="dispatched")
    assert any(entry.get("thread_id") == managed_thread_id for entry in dispatched)

    queue_path = (
        hub_env.hub_root
        / ".codex-autorunner"
        / "pma"
        / "queue"
        / "pma__COLON__lane-next.jsonl"
    )
    assert queue_path.exists()
    lines = [
        line.strip()
        for line in queue_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines
    wake_ups = [
        (json.loads(line).get("payload") or {}).get("wake_up") or {} for line in lines
    ]
    assert any(
        wake_up.get("thread_id") == managed_thread_id
        and wake_up.get("to_state") == "completed"
        and wake_up.get("lane_id") == "pma:lane-next"
        for wake_up in wake_ups
    )


def test_send_message_notify_on_terminal_auto_subscribes_once(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    install_fake_supervisor(app)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        message_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={
                "message": "trigger completion",
                "notify_on": "terminal",
                "notify_lane": "pma:auto-lane",
                "notify_once": True,
            },
        )
        assert message_resp.status_code == 200
        payload = message_resp.json()
        assert payload["status"] == "ok"
        assert payload["send_state"] == "accepted"
        notification = payload.get("notification") or {}
        subscription = notification.get("subscription") or {}
        assert subscription.get("thread_id") == managed_thread_id
        assert subscription.get("lane_id") == "pma:auto-lane"

    automation_store = app.state.hub_supervisor.get_pma_automation_store()
    active_subs = automation_store.list_subscriptions(thread_id=managed_thread_id)
    assert active_subs == []
    all_subs = automation_store.list_subscriptions(
        include_inactive=True, thread_id=managed_thread_id
    )
    assert all_subs
    assert all_subs[0].get("state") == "cancelled"
    assert all_subs[0].get("match_count") == 1


def test_send_message_defaults_to_terminal_followup_subscription(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    install_fake_supervisor(app)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        message_resp = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={
                "message": "trigger completion",
            },
        )
        assert message_resp.status_code == 200
        payload = message_resp.json()
        assert payload["status"] == "ok"
        assert payload["send_state"] == "accepted"
        notification = payload.get("notification") or {}
        subscription = notification.get("subscription") or {}
        assert notification.get("mode") == "terminal"
        assert subscription.get("thread_id") == managed_thread_id

    automation_store = app.state.hub_supervisor.get_pma_automation_store()
    active_subs = automation_store.list_subscriptions(thread_id=managed_thread_id)
    assert active_subs == []
    all_subs = automation_store.list_subscriptions(
        include_inactive=True, thread_id=managed_thread_id
    )
    assert all_subs
    assert all_subs[0].get("state") == "cancelled"
    assert all_subs[0].get("match_count") == 1


@pytest.mark.anyio
async def test_send_message_defer_execution_drains_five_rapid_messages_in_order(
    hub_env,
) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    blockers = [asyncio.Event() for _ in range(5)]
    prompts = [f"rapid message {index}" for index in range(1, 6)]

    class FakeTurnHandle:
        def __init__(self, turn_id: str, blocker: asyncio.Event, text: str) -> None:
            self.turn_id = turn_id
            self._blocker = blocker
            self._text = text

        async def wait(self, timeout=None):
            _ = timeout
            await self._blocker.wait()
            return type(
                "Result",
                (),
                {
                    "agent_messages": [self._text],
                    "raw_events": [],
                    "errors": [],
                },
            )()

    class FakeClient:
        def __init__(self) -> None:
            self.turn_start_calls: list[str] = []
            self._turn_index = 0

        async def thread_resume(self, thread_id: str) -> None:
            _ = thread_id

        async def thread_start(self, root: str) -> dict[str, str]:
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
            _ = thread_id, approval_policy, sandbox_policy, turn_kwargs
            turn_index = self._turn_index
            self._turn_index += 1
            self.turn_start_calls.append(prompt)
            return FakeTurnHandle(
                f"backend-turn-{turn_index + 1}",
                blockers[turn_index],
                f"assistant-output-{turn_index + 1}",
            )

        async def turn_interrupt(
            self, turn_id: str, *, thread_id: str | None = None
        ) -> None:
            _ = turn_id, thread_id
            raise RuntimeError("backend interrupt exploded")

    fake_supervisor = FakeSupervisor(FakeClient())
    app.state.app_server_supervisor = fake_supervisor
    app.state.app_server_events = object()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        create_resp = await client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        payloads: list[dict[str, object]] = []
        active_turn_id: object | None = None
        for index, prompt in enumerate(prompts):
            response = await client.post(
                f"/hub/pma/threads/{managed_thread_id}/messages",
                json={"message": prompt, "defer_execution": True},
            )
            assert response.status_code == 200
            payload = response.json()
            payloads.append(payload)
            if index == 0:
                active_turn_id = payload["managed_turn_id"]
                assert payload["send_state"] == "accepted"
                assert payload["execution_state"] == "running"
            else:
                assert payload["send_state"] == "queued"
                assert payload["execution_state"] == "queued"
                assert payload["queue_depth"] == index
                assert payload["active_managed_turn_id"] == active_turn_id

        store = PmaThreadStore(hub_env.hub_root)
        for index, payload in enumerate(payloads):
            turn = store.get_turn(managed_thread_id, str(payload["managed_turn_id"]))
            assert turn is not None
            assert turn["status"] == ("running" if index == 0 else "queued")

        for index, payload in enumerate(payloads):
            blockers[index].set()
            managed_turn_id = str(payload["managed_turn_id"])
            with anyio.fail_after(2):
                while True:
                    turn = store.get_turn(managed_thread_id, managed_turn_id)
                    if turn is not None and turn.get("status") == "ok":
                        break
                    await anyio.sleep(0.05)
            if index < len(payloads) - 1:
                next_turn_id = str(payloads[index + 1]["managed_turn_id"])
                with anyio.fail_after(2):
                    while True:
                        next_turn = store.get_turn(managed_thread_id, next_turn_id)
                        if (
                            next_turn is not None
                            and next_turn.get("status") == "running"
                        ):
                            break
                        await anyio.sleep(0.05)

    store = PmaThreadStore(hub_env.hub_root)
    for index, payload in enumerate(payloads, start=1):
        turn = store.get_turn(managed_thread_id, str(payload["managed_turn_id"]))
        assert turn is not None
        assert turn["status"] == "ok"
        assert turn["assistant_text"] == f"assistant-output-{index}"
        assert turn["backend_turn_id"] == f"backend-turn-{index}"
    assert store.get_running_turn(managed_thread_id) is None
    assert store.get_queue_depth(managed_thread_id) == 0
    assert len(fake_supervisor.client.turn_start_calls) == 5
    for prompt, runtime_prompt in zip(prompts, fake_supervisor.client.turn_start_calls):
        assert prompt in runtime_prompt


@pytest.mark.anyio
async def test_send_message_defer_execution_completes_in_background(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    blocker = asyncio.Event()

    install_fake_supervisor(app, client=FakeClient(blocker=blocker))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        create_resp = await client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]
        await _bind_thread_to_discord(
            hub_env,
            managed_thread_id=managed_thread_id,
            channel_id="discord-background",
        )

        message_resp = await client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "background turn", "defer_execution": True},
        )
        assert message_resp.status_code == 200
        payload = message_resp.json()
        assert payload["status"] == "ok"
        assert payload["send_state"] == "accepted"
        assert payload["execution_state"] == "running"
        assert payload["assistant_text"] == ""

        store = PmaThreadStore(hub_env.hub_root)
        managed_turn_id = payload["managed_turn_id"]
        turn = store.get_turn(managed_thread_id, managed_turn_id)
        assert turn is not None
        assert turn["status"] == "running"
        assert store.has_running_turn(managed_thread_id) is True

        task_pool = getattr(app.state, "pma_managed_thread_tasks", None)
        assert isinstance(task_pool, set)
        assert len(task_pool) == 1

        blocker.set()

        with anyio.fail_after(2):
            while True:
                turn = store.get_turn(managed_thread_id, managed_turn_id)
                if turn is not None and turn.get("status") == "ok":
                    break
                await anyio.sleep(0.05)

        finalized_thread = store.get_thread(managed_thread_id)
        assert finalized_thread is not None
        assert finalized_thread["last_turn_id"] == managed_turn_id
        assert finalized_thread["last_message_preview"] == "background turn"

        transcript = PmaTranscriptStore(hub_env.hub_root).read_transcript(
            managed_turn_id
        )
        assert transcript is not None
        assert transcript["content"].strip() == "assistant-output"

        with anyio.fail_after(2):
            while len(getattr(app.state, "pma_managed_thread_tasks", set())) != 0:
                await anyio.sleep(0.05)

    discord_store = DiscordStateStore(
        hub_env.hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    try:
        with anyio.fail_after(2):
            while True:
                discord_outbox = await discord_store.list_outbox()
                if any(
                    record.channel_id == "discord-background"
                    and record.payload_json.get("content") == "assistant-output"
                    for record in discord_outbox
                ):
                    break
                await anyio.sleep(0.05)
    finally:
        await discord_store.close()


@pytest.mark.anyio
async def test_queued_message_runs_after_background_turn_finishes(hub_env) -> None:
    _enable_pma(hub_env.hub_root)
    app = create_hub_app(hub_env.hub_root)
    first_blocker = asyncio.Event()
    second_blocker = asyncio.Event()

    class FakeTurnHandle:
        def __init__(self, turn_id: str, blocker: asyncio.Event, text: str) -> None:
            self.turn_id = turn_id
            self._blocker = blocker
            self._text = text

        async def wait(self, timeout=None):
            _ = timeout
            await self._blocker.wait()
            return type(
                "Result",
                (),
                {
                    "agent_messages": [self._text],
                    "raw_events": [],
                    "errors": [],
                },
            )()

    class FakeClient:
        def __init__(self) -> None:
            self.turn_start_calls: list[str] = []

        async def thread_start(self, root: str) -> dict:
            _ = root
            return {"id": "backend-thread-1"}

        async def thread_resume(self, thread_id: str) -> None:
            _ = thread_id

        async def turn_start(
            self,
            thread_id: str,
            prompt: str,
            approval_policy: str,
            sandbox_policy: str,
            **turn_kwargs,
        ):
            _ = thread_id, approval_policy, sandbox_policy, turn_kwargs
            self.turn_start_calls.append(prompt)
            if len(self.turn_start_calls) == 1:
                return FakeTurnHandle("backend-turn-1", first_blocker, "first-output")
            return FakeTurnHandle("backend-turn-2", second_blocker, "second-output")

    class FakeSupervisor:
        def __init__(self) -> None:
            self.client = FakeClient()

        async def get_client(self, hub_root: Path):
            _ = hub_root
            return self.client

    fake_supervisor = FakeSupervisor()
    app.state.app_server_supervisor = fake_supervisor
    app.state.app_server_events = object()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        create_resp = await client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        first_resp = await client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "first", "defer_execution": True},
        )
        assert first_resp.status_code == 200
        first_payload = first_resp.json()
        assert first_payload["send_state"] == "accepted"

        second_resp = await client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "second"},
        )
        assert second_resp.status_code == 200
        second_payload = second_resp.json()
        assert second_payload["send_state"] == "queued"
        assert second_payload["execution_state"] == "queued"
        assert second_payload["queue_depth"] == 1
        assert (
            second_payload["active_managed_turn_id"] == first_payload["managed_turn_id"]
        )

        store = PmaThreadStore(hub_env.hub_root)
        queued_turn = store.get_turn(
            managed_thread_id, second_payload["managed_turn_id"]
        )
        assert queued_turn is not None
        assert queued_turn["status"] == "queued"

        first_blocker.set()

        with anyio.fail_after(2):
            while True:
                queued_turn = store.get_turn(
                    managed_thread_id, second_payload["managed_turn_id"]
                )
                if queued_turn is not None and queued_turn.get("status") == "running":
                    break
                await anyio.sleep(0.05)

        second_blocker.set()

        with anyio.fail_after(2):
            while True:
                queued_turn = store.get_turn(
                    managed_thread_id, second_payload["managed_turn_id"]
                )
                if queued_turn is not None and queued_turn.get("status") == "ok":
                    break
                await anyio.sleep(0.05)

    assert len(fake_supervisor.client.turn_start_calls) == 2
