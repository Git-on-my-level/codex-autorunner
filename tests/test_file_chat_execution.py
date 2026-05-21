from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from codex_autorunner.core import drafts as draft_utils
from codex_autorunner.core.orchestration import FreshConversationRequiredError
from codex_autorunner.core.state import now_iso
from codex_autorunner.surfaces.web.routes.file_chat import FileChatRoutesState
from codex_autorunner.surfaces.web.routes.file_chat_routes import (
    execution as execution_module,
)
from codex_autorunner.surfaces.web.routes.file_chat_routes import (
    execution_agents,
    runtime,
)
from codex_autorunner.surfaces.web.routes.file_chat_routes.drafts import (
    apply_file_patch,
    pending_file_patch,
)
from codex_autorunner.surfaces.web.routes.file_chat_routes.execution import (
    execute_file_chat,
)
from codex_autorunner.surfaces.web.routes.file_chat_routes.targets import (
    build_patch,
    parse_target,
)
from codex_autorunner.surfaces.web.services import file_chat as file_chat_service


def _make_request_state(
    repo_root: Path,
    *,
    config: object | None = None,
    **extra_state: object,
) -> SimpleNamespace:
    state = dict(
        app_server_supervisor=object(),
        app_server_threads=None,
        opencode_supervisor=None,
        config=config,
        engine=SimpleNamespace(repo_root=repo_root),
        app_server_events=None,
    )
    state.update(extra_state)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def _make_request(repo_root: Path, *, config: object | None = None) -> SimpleNamespace:
    return _make_request_state(repo_root, config=config)


def _make_request_with_hermes(
    repo_root: Path, *, config: object | None = None
) -> SimpleNamespace:
    return _make_request_state(
        repo_root,
        config=config,
        hermes_supervisor=object(),
    )


class _ThreadRegistryStub:
    def __init__(self, thread_id: str | None = None) -> None:
        self.thread_id = thread_id
        self.reset_calls: list[str] = []
        self.set_calls: list[tuple[str, str]] = []

    def get_thread_id(self, thread_key: str) -> str | None:
        return self.thread_id

    def set_thread_id(self, thread_key: str, thread_id: str) -> None:
        self.thread_id = thread_id
        self.set_calls.append((thread_key, thread_id))

    def reset_thread(self, thread_key: str) -> None:
        self.thread_id = None
        self.reset_calls.append(thread_key)


class _HarnessTurnStub:
    def __init__(self, conversation_id: str, turn_id: str = "turn-1") -> None:
        self.conversation_id = conversation_id
        self.turn_id = turn_id


class _HarnessResultStub:
    assistant_text = "Agent: updated via hermes"
    raw_events: list[object] = []
    errors: list[str] = []


@pytest.mark.asyncio
async def test_file_chat_service_non_stream_and_stream_share_turn_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    target_path = repo_root / ".codex-autorunner" / "contextspace" / "spec.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("before\n", encoding="utf-8")
    request = _make_request(repo_root)
    calls: list[str] = []
    canonical_requests: list[dict[str, object]] = []

    async def fake_execute_file_chat_agent_turn(
        _request,
        _repo_root: Path,
        _target,
        message: str,
        **kwargs,
    ) -> dict[str, object]:
        turn_request = kwargs.get("turn_request")
        assert turn_request is not None
        canonical_requests.append(turn_request.to_dict())
        on_meta = kwargs.get("on_meta")
        assert on_meta is not None
        await on_meta("codex", f"thread-{message}", f"turn-{message}")
        calls.append(message)
        return {
            "status": "ok",
            "message": f"done {message}",
            "agent_message": f"done {message}",
            "thread_id": f"thread-{message}",
            "turn_id": f"turn-{message}",
            "raw_events": [{"type": "delta", "message": message}],
            "usage_parts": [{"tokens": 1}],
        }

    monkeypatch.setattr(
        file_chat_service,
        "execute_file_chat_agent_turn",
        fake_execute_file_chat_agent_turn,
    )

    non_stream = await file_chat_service.run_turn(
        request,
        file_chat_service.FileChatTurnRequest(
            target="contextspace:spec",
            message="nonstream",
            client_turn_id="client-nonstream",
        ),
    )
    state = request.app.state.file_chat_routes_state
    assert non_stream["status"] == "ok"
    assert non_stream["client_turn_id"] == "client-nonstream"
    assert state.active_chats == {}
    assert state.current_by_client == {}
    assert state.last_by_client["client-nonstream"]["turn_id"] == "turn-nonstream"

    stream_iter = await file_chat_service.open_stream_turn(
        request,
        file_chat_service.FileChatTurnRequest(
            target="contextspace:spec",
            message="stream",
            client_turn_id="client-stream",
        ),
    )
    stream_items = [item async for item in stream_iter]
    assert calls == ["nonstream", "stream"]
    assert [item["request_id"] for item in canonical_requests] == [
        "client-nonstream",
        "client-stream",
    ]
    assert {
        key: canonical_requests[0][key]
        for key in ("target_id", "target_kind", "request_kind", "agent")
    } == {
        key: canonical_requests[1][key]
        for key in ("target_id", "target_kind", "request_kind", "agent")
    }
    assert canonical_requests[0]["approval_policy"] == "on-request"
    assert canonical_requests[0]["sandbox_policy"] == "dangerFullAccess"
    assert [item.event for item in stream_items] == [
        "status",
        "app-server",
        "token_usage",
        "update",
        "done",
    ]
    assert stream_items[-2].payload["client_turn_id"] == "client-stream"
    assert state.active_chats == {}
    assert state.current_by_client == {}
    assert state.last_by_client["client-stream"]["turn_id"] == "turn-stream"
    assert state.last_by_client["client-stream"]["execution_id"] == "client-stream"
    assert state.last_by_client["client-stream"]["turn_request"]["request_id"] == (
        "client-stream"
    )


@pytest.mark.asyncio
async def test_file_chat_service_active_state_uses_canonical_execution_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    target_path = repo_root / ".codex-autorunner" / "contextspace" / "spec.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("before\n", encoding="utf-8")
    request = _make_request(repo_root)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def fake_execute_file_chat_agent_turn(
        _request,
        _repo_root: Path,
        _target,
        _message: str,
        **kwargs,
    ) -> dict[str, object]:
        on_meta = kwargs.get("on_meta")
        assert on_meta is not None
        await on_meta("codex", "thread-active", "turn-active")
        started.set()
        await finish.wait()
        return {
            "status": "ok",
            "message": "done",
            "thread_id": "thread-active",
            "turn_id": "turn-active",
        }

    monkeypatch.setattr(
        file_chat_service,
        "execute_file_chat_agent_turn",
        fake_execute_file_chat_agent_turn,
    )

    run_task = asyncio.create_task(
        file_chat_service.run_turn(
            request,
            file_chat_service.FileChatTurnRequest(
                target="contextspace:spec",
                message="active",
                client_turn_id="client-active",
            ),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    snapshot = await file_chat_service.get_active_snapshot(request, "client-active")
    assert snapshot.active is True
    assert snapshot.current["execution_id"] == "client-active"
    assert snapshot.current["turn_record"]["execution_id"] == "client-active"
    assert snapshot.current["turn_request"]["request_id"] == "client-active"

    finish.set()
    result = await run_task
    assert result["execution_id"] == "client-active"
    last = await file_chat_service.get_active_snapshot(request, "client-active")
    assert last.active is False
    assert last.last_result["execution_id"] == "client-active"


@pytest.mark.asyncio
async def test_file_chat_service_rejects_already_running_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    target = parse_target(repo_root, "ticket:1")
    request = _make_request(repo_root)
    request.app.state.file_chat_routes_state = FileChatRoutesState()
    request.app.state.file_chat_routes_state.active_chats[target.state_key] = (
        asyncio.Event()
    )

    async def fail_execute(*args, **kwargs):
        raise AssertionError("execution should not start")

    monkeypatch.setattr(
        file_chat_service,
        "execute_file_chat_agent_turn",
        fail_execute,
    )

    with pytest.raises(HTTPException) as exc_info:
        await file_chat_service.run_turn(
            request,
            file_chat_service.FileChatTurnRequest(
                target="ticket:1",
                message="edit",
                already_running_detail="Ticket chat already running",
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Ticket chat already running"


@pytest.mark.asyncio
async def test_file_chat_service_rejects_opencode_without_resolved_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    target_path = repo_root / ".codex-autorunner" / "contextspace" / "spec.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("before\n", encoding="utf-8")
    request = _make_request(repo_root)

    async def fail_execute(*args, **kwargs):
        raise AssertionError("OpenCode execution should not start")

    monkeypatch.setattr(
        file_chat_service,
        "execute_file_chat_agent_turn",
        fail_execute,
    )

    with pytest.raises(HTTPException) as exc_info:
        await file_chat_service.run_turn(
            request,
            file_chat_service.FileChatTurnRequest(
                target="contextspace:spec",
                message="edit",
                agent="opencode",
            ),
        )

    assert exc_info.value.status_code == 400
    assert (
        exc_info.value.detail
        == "opencode requests require resolved provider/model before execution"
    )


@pytest.mark.asyncio
async def test_file_chat_service_interrupt_cleans_up_active_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    request = _make_request(repo_root)

    async def fake_execute_file_chat_agent_turn(
        _request,
        _repo_root: Path,
        target,
        _message: str,
        **_kwargs,
    ) -> dict[str, str]:
        interrupt = await runtime.get_or_create_interrupt_event(
            _request, target.state_key
        )
        interrupt.set()
        return {"status": "interrupted", "detail": "File chat interrupted"}

    monkeypatch.setattr(
        file_chat_service,
        "execute_file_chat_agent_turn",
        fake_execute_file_chat_agent_turn,
    )

    result = await file_chat_service.run_turn(
        request,
        file_chat_service.FileChatTurnRequest(
            target="contextspace:active_context",
            message="stop",
            client_turn_id="client-stop",
        ),
    )

    state = request.app.state.file_chat_routes_state
    assert result["status"] == "interrupted"
    assert state.active_chats == {}
    assert state.current_by_client == {}
    assert state.last_by_client["client-stop"]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_file_chat_service_creates_contextspace_target_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    request = _make_request(repo_root)
    contextspace_dir = repo_root / ".codex-autorunner" / "contextspace"
    assert not contextspace_dir.exists()

    async def fake_execute_file_chat_agent_turn(
        _request,
        _repo_root: Path,
        target,
        _message: str,
        **_kwargs,
    ) -> dict[str, str]:
        assert target.kind == "contextspace"
        assert target.path.parent.exists()
        return {"status": "ok", "message": "created"}

    monkeypatch.setattr(
        file_chat_service,
        "execute_file_chat_agent_turn",
        fake_execute_file_chat_agent_turn,
    )

    result = await file_chat_service.run_turn(
        request,
        file_chat_service.FileChatTurnRequest(
            target="contextspace:active_context",
            message="create context doc",
        ),
    )

    assert result["status"] == "ok"
    assert contextspace_dir.exists()


@pytest.mark.asyncio
async def test_execute_harness_turn_recovers_from_stale_resume_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    registry = _ThreadRegistryStub(thread_id="stale-conv")
    started: list[str] = []

    class Harness:
        def supports(self, capability: str) -> bool:
            return capability in {"durable_threads", "message_turns"}

        async def ensure_ready(self, _repo_root: Path) -> None:
            return None

        async def resume_conversation(self, _repo_root: Path, conversation_id: str):
            raise FreshConversationRequiredError("missing session")

        async def new_conversation(self, _repo_root: Path, title: str = ""):
            return SimpleNamespace(id="fresh-conv")

        async def start_turn(
            self, _repo_root: Path, conversation_id: str, *args, **kwargs
        ):
            started.append(conversation_id)
            return _HarnessTurnStub(conversation_id, "turn-fresh")

        async def wait_for_turn(
            self, _repo_root: Path, conversation_id: str, turn_id: str, timeout=None
        ):
            return _HarnessResultStub()

    monkeypatch.setattr(
        execution_agents, "_build_runtime_harness", lambda *args, **kwargs: Harness()
    )

    result = await execution_agents.execute_harness_turn(
        _make_request(repo_root),
        repo_root,
        "edit file",
        asyncio.Event(),
        agent_id="hermes",
        profile="m4-pma",
        thread_registry=registry,
        thread_key="file_chat.hermes.profile.m4-pma.demo",
    )

    assert result["status"] == "ok"
    assert started == ["fresh-conv"]
    assert registry.thread_id == "fresh-conv"
    assert registry.reset_calls == []


@pytest.mark.asyncio
async def test_execute_harness_turn_resets_stale_thread_when_start_turn_requires_fresh_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    registry = _ThreadRegistryStub(thread_id="stale-conv")
    started: list[str] = []

    class Harness:
        def __init__(self) -> None:
            self.start_calls = 0

        def supports(self, capability: str) -> bool:
            return capability in {"durable_threads", "message_turns"}

        async def ensure_ready(self, _repo_root: Path) -> None:
            return None

        async def resume_conversation(self, _repo_root: Path, conversation_id: str):
            return SimpleNamespace(id=conversation_id)

        async def new_conversation(self, _repo_root: Path, title: str = ""):
            return SimpleNamespace(id="fresh-conv")

        async def start_turn(
            self, _repo_root: Path, conversation_id: str, *args, **kwargs
        ):
            self.start_calls += 1
            started.append(conversation_id)
            if self.start_calls == 1:
                raise FreshConversationRequiredError("stale turn target")
            return _HarnessTurnStub(conversation_id, "turn-fresh")

        async def wait_for_turn(
            self, _repo_root: Path, conversation_id: str, turn_id: str, timeout=None
        ):
            return _HarnessResultStub()

    monkeypatch.setattr(
        execution_agents, "_build_runtime_harness", lambda *args, **kwargs: Harness()
    )

    result = await execution_agents.execute_harness_turn(
        _make_request(repo_root),
        repo_root,
        "edit file",
        asyncio.Event(),
        agent_id="hermes",
        profile="m4-pma",
        thread_registry=registry,
        thread_key="file_chat.hermes.profile.m4-pma.demo",
    )

    assert result["status"] == "ok"
    assert started == ["stale-conv", "fresh-conv"]
    assert registry.reset_calls == ["file_chat.hermes.profile.m4-pma.demo"]
    assert registry.thread_id == "fresh-conv"


@pytest.mark.asyncio
async def test_execute_file_chat_writes_draft_working_copy_without_touching_live_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    target_path = repo_root / ".codex-autorunner" / "contextspace" / "spec.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("before\n", encoding="utf-8")
    target = parse_target(repo_root, "contextspace:spec")

    async def fake_get_or_create_interrupt_event(
        _request, _state_key: str
    ) -> asyncio.Event:
        return asyncio.Event()

    async def fake_update_turn_state(*args, **kwargs) -> None:
        return None

    async def fake_execute_app_server(
        _supervisor,
        fake_repo_root: Path,
        prompt: str,
        _interrupt_event: asyncio.Event,
        **kwargs,
    ) -> dict[str, str]:
        match = re.search(r"<path>\n(.+?)\n</path>", prompt, re.DOTALL)
        assert match is not None
        rel_path = match.group(1).strip()
        assert rel_path.startswith(".codex-autorunner/file-chat-drafts/")
        draft_path = fake_repo_root / rel_path
        draft_path.write_text("after\n", encoding="utf-8")
        return {
            "status": "ok",
            "agent_message": "updated draft",
            "message": "updated draft",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
        }

    monkeypatch.setattr(
        runtime, "get_or_create_interrupt_event", fake_get_or_create_interrupt_event
    )
    monkeypatch.setattr(runtime, "update_turn_state", fake_update_turn_state)
    monkeypatch.setattr(
        execution_module,
        "execute_app_server",
        fake_execute_app_server,
    )

    result = await execute_file_chat(
        _make_request(repo_root),
        repo_root,
        target,
        "edit the draft",
    )

    assert result["status"] == "ok"
    assert result["has_draft"] is True
    assert result["content"] == "after\n"
    assert target_path.read_text(encoding="utf-8") == "before\n"

    state = draft_utils.load_state(repo_root)
    draft = state["drafts"][target.state_key]
    assert draft["content"] == "after\n"
    assert draft["patch"] == build_patch(target.rel_path, "before\n", "after\n")
    assert draft["base_hash"] == draft_utils.hash_content("before\n")
    assert (repo_root / draft["draft_rel_path"]).read_text(
        encoding="utf-8"
    ) == "after\n"
    assert (repo_root / draft["base_rel_path"]).read_text(
        encoding="utf-8"
    ) == "before\n"


@pytest.mark.asyncio
async def test_execute_file_chat_continues_from_existing_draft_working_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    target_path = repo_root / ".codex-autorunner" / "contextspace" / "decisions.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("live\n", encoding="utf-8")
    target = parse_target(repo_root, "contextspace:decisions")
    work_path, base_path = draft_utils.write_draft_files(
        repo_root,
        target.state_key,
        target.rel_path,
        base_content="live\n",
        working_content="draft one\n",
    )
    draft_utils.save_state(
        repo_root,
        {
            "drafts": {
                target.state_key: {
                    "content": "draft one\n",
                    "patch": build_patch(target.rel_path, "live\n", "draft one\n"),
                    "agent_message": "seed draft",
                    "created_at": "2026-03-16T00:00:00Z",
                    "base_hash": draft_utils.hash_content("live\n"),
                    "target": target.target,
                    "rel_path": target.rel_path,
                    "draft_rel_path": str(work_path.relative_to(repo_root)),
                    "base_rel_path": str(base_path.relative_to(repo_root)),
                }
            }
        },
    )

    async def fake_get_or_create_interrupt_event(
        _request, _state_key: str
    ) -> asyncio.Event:
        return asyncio.Event()

    async def fake_update_turn_state(*args, **kwargs) -> None:
        return None

    async def fake_execute_app_server(
        _supervisor,
        fake_repo_root: Path,
        prompt: str,
        _interrupt_event: asyncio.Event,
        **kwargs,
    ) -> dict[str, str]:
        assert "<FILE_CONTENT>\ndraft one\n\n</FILE_CONTENT>" in prompt
        match = re.search(r"<path>\n(.+?)\n</path>", prompt, re.DOTALL)
        assert match is not None
        draft_path = fake_repo_root / match.group(1).strip()
        draft_path.write_text("draft two\n", encoding="utf-8")
        return {
            "status": "ok",
            "agent_message": "updated again",
            "message": "updated again",
            "thread_id": "thread-2",
            "turn_id": "turn-2",
        }

    monkeypatch.setattr(
        runtime, "get_or_create_interrupt_event", fake_get_or_create_interrupt_event
    )
    monkeypatch.setattr(runtime, "update_turn_state", fake_update_turn_state)
    monkeypatch.setattr(
        execution_module,
        "execute_app_server",
        fake_execute_app_server,
    )

    result = await execute_file_chat(
        _make_request(repo_root),
        repo_root,
        target,
        "refine the pending draft",
    )

    assert result["status"] == "ok"
    assert result["has_draft"] is True
    assert result["content"] == "draft two\n"
    assert result["created_at"] == "2026-03-16T00:00:00Z"
    assert target_path.read_text(encoding="utf-8") == "live\n"
    state = draft_utils.load_state(repo_root)
    assert state["drafts"][target.state_key]["patch"] == build_patch(
        target.rel_path, "live\n", "draft two\n"
    )


@pytest.mark.asyncio
async def test_extracted_draft_handlers_reconstruct_content_from_artifacts(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    target_path = repo_root / ".codex-autorunner" / "contextspace" / "active_context.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("base\n", encoding="utf-8")
    target = parse_target(repo_root, "contextspace:active_context")
    work_path, base_path = draft_utils.write_draft_files(
        repo_root,
        target.state_key,
        target.rel_path,
        base_content="base\n",
        working_content="drafted\n",
    )
    draft_utils.save_state(
        repo_root,
        {
            "drafts": {
                target.state_key: {
                    "agent_message": "artifact-backed",
                    "created_at": now_iso(),
                    "base_hash": draft_utils.hash_content("base\n"),
                    "target": target.target,
                    "rel_path": target.rel_path,
                    "draft_rel_path": str(work_path.relative_to(repo_root)),
                    "base_rel_path": str(base_path.relative_to(repo_root)),
                }
            }
        },
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(engine=SimpleNamespace(repo_root=repo_root))
        )
    )

    pending = await pending_file_patch(request, target.target)
    assert pending["content"] == "drafted\n"
    assert pending["patch"] == build_patch(target.rel_path, "base\n", "drafted\n")

    applied = await apply_file_patch(request, {"target": target.target})
    assert applied["content"] == "drafted\n"
    assert target_path.read_text(encoding="utf-8") == "drafted\n"
    assert target.state_key not in draft_utils.load_state(repo_root).get("drafts", {})
    assert not draft_utils.draft_dir(repo_root, target.state_key).exists()


@pytest.mark.asyncio
async def test_execute_file_chat_passes_hermes_profile_to_runtime_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    target_path = repo_root / ".codex-autorunner" / "contextspace" / "spec.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("before\n", encoding="utf-8")
    target = parse_target(repo_root, "contextspace:spec")

    async def fake_get_or_create_interrupt_event(
        _request, _state_key: str
    ) -> asyncio.Event:
        return asyncio.Event()

    async def fake_update_turn_state(*args, **kwargs) -> None:
        return None

    observed: dict[str, object] = {}

    async def fake_execute_harness_turn(
        _request,
        fake_repo_root: Path,
        prompt: str,
        _interrupt_event: asyncio.Event,
        *,
        agent_id: str,
        profile: str | None,
        thread_key: str | None,
        **kwargs,
    ) -> dict[str, str]:
        observed["agent_id"] = agent_id
        observed["profile"] = profile
        observed["thread_key"] = thread_key
        match = re.search(r"<path>\n(.+?)\n</path>", prompt, re.DOTALL)
        assert match is not None
        draft_path = fake_repo_root / match.group(1).strip()
        draft_path.write_text("after\n", encoding="utf-8")
        return {
            "status": "ok",
            "agent_message": "updated via hermes",
            "message": "updated via hermes",
            "thread_id": "hermes-thread-1",
            "turn_id": "hermes-turn-1",
        }

    monkeypatch.setattr(
        runtime, "get_or_create_interrupt_event", fake_get_or_create_interrupt_event
    )
    monkeypatch.setattr(runtime, "update_turn_state", fake_update_turn_state)

    def _resolve_profile(_request, _agent_id, requested_profile, default_profile=None):
        _ = default_profile
        return requested_profile

    monkeypatch.setattr(
        execution_module,
        "resolve_requested_agent_profile",
        _resolve_profile,
    )
    monkeypatch.setattr(
        execution_module,
        "execute_harness_turn",
        fake_execute_harness_turn,
    )

    result = await execute_file_chat(
        _make_request_with_hermes(repo_root),
        repo_root,
        target,
        "edit the file",
        agent="hermes",
        profile="m4-pma",
    )

    assert result["status"] == "ok"
    assert result["profile"] == "m4-pma"
    assert observed == {
        "agent_id": "hermes",
        "profile": "m4-pma",
        "thread_key": "file_chat.hermes.profile.m4-pma.contextspace_spec.md",
    }


@pytest.mark.asyncio
async def test_execute_file_chat_supports_hermes_without_codex_or_opencode_supervisors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    target_path = repo_root / ".codex-autorunner" / "contextspace" / "decisions.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("before\n", encoding="utf-8")
    target = parse_target(repo_root, "contextspace:decisions")

    async def fake_get_or_create_interrupt_event(
        _request, _state_key: str
    ) -> asyncio.Event:
        return asyncio.Event()

    async def fake_update_turn_state(*args, **kwargs) -> None:
        return None

    observed: dict[str, object] = {}

    async def fake_execute_harness_turn(
        _request,
        fake_repo_root: Path,
        prompt: str,
        _interrupt_event: asyncio.Event,
        *,
        agent_id: str,
        profile: str | None,
        thread_key: str | None,
        **kwargs,
    ) -> dict[str, str]:
        observed["agent_id"] = agent_id
        observed["profile"] = profile
        observed["thread_key"] = thread_key
        match = re.search(r"<path>\n(.+?)\n</path>", prompt, re.DOTALL)
        assert match is not None
        draft_path = fake_repo_root / match.group(1).strip()
        draft_path.write_text("after\n", encoding="utf-8")
        return {
            "status": "ok",
            "agent_message": "updated via hermes",
            "message": "updated via hermes",
            "thread_id": "hermes-thread-2",
            "turn_id": "hermes-turn-2",
        }

    monkeypatch.setattr(
        runtime, "get_or_create_interrupt_event", fake_get_or_create_interrupt_event
    )
    monkeypatch.setattr(runtime, "update_turn_state", fake_update_turn_state)

    def _resolve_profile(_request, _agent_id, requested_profile, default_profile=None):
        _ = default_profile
        return requested_profile

    monkeypatch.setattr(
        execution_module,
        "resolve_requested_agent_profile",
        _resolve_profile,
    )
    monkeypatch.setattr(
        execution_module,
        "execute_harness_turn",
        fake_execute_harness_turn,
    )

    request = _make_request_state(
        repo_root,
        app_server_supervisor=None,
        opencode_supervisor=None,
        hermes_supervisor=object(),
    )
    result = await execute_file_chat(
        request,
        repo_root,
        target,
        "edit the file",
        agent="hermes",
        profile="m4-pma",
    )

    assert result["status"] == "ok"
    assert result["profile"] == "m4-pma"
    assert observed == {
        "agent_id": "hermes",
        "profile": "m4-pma",
        "thread_key": "file_chat.hermes.profile.m4-pma.contextspace_decisions.md",
    }


@pytest.mark.asyncio
async def test_execute_file_chat_reports_capability_driven_error_for_unsupported_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    target_path = repo_root / ".codex-autorunner" / "contextspace" / "active_context.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("before\n", encoding="utf-8")
    target = parse_target(repo_root, "contextspace:active_context")

    async def fake_get_or_create_interrupt_event(
        _request, _state_key: str
    ) -> asyncio.Event:
        return asyncio.Event()

    async def fake_update_turn_state(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(
        runtime, "get_or_create_interrupt_event", fake_get_or_create_interrupt_event
    )
    monkeypatch.setattr(runtime, "update_turn_state", fake_update_turn_state)

    def _resolve_chat_agent(
        agent, profile=None, default="codex", context=None
    ) -> tuple[str, None]:
        _ = profile, default, context
        return str(agent).strip().lower(), None

    def _resolve_profile(_request, _agent_id, requested_profile, default_profile=None):
        _ = default_profile
        return requested_profile

    def _has_capability(agent_id, capability, _context=None) -> bool:
        return capability == "durable_threads" and agent_id == "broken"

    monkeypatch.setattr(
        execution_module,
        "resolve_chat_agent_and_profile",
        _resolve_chat_agent,
    )
    monkeypatch.setattr(
        execution_module,
        "validate_agent_id",
        lambda agent_id, _context=None: str(agent_id).strip().lower(),
    )
    monkeypatch.setattr(
        execution_module,
        "resolve_requested_agent_profile",
        _resolve_profile,
    )
    monkeypatch.setattr(
        execution_module,
        "has_capability",
        _has_capability,
    )

    request = _make_request_state(
        repo_root,
        app_server_supervisor=None,
        opencode_supervisor=None,
    )
    result = await execute_file_chat(
        request,
        repo_root,
        target,
        "edit the file",
        agent="broken",
    )

    assert {
        key: result[key]
        for key in (
            "status",
            "detail",
        )
    } == {
        "status": "error",
        "detail": (
            "Agent 'broken' does not support file-chat execution "
            "(missing capability: message_turns)"
        ),
    }
    assert result["turn_request"]["agent"] == "broken"


def test_resolve_file_chat_agent_selection_rejects_invalid_hermes_profile(
    tmp_path: Path,
) -> None:
    target = parse_target(tmp_path, "ticket:1")
    config = SimpleNamespace(
        agent_profiles=lambda agent_id: (
            {"m4-pma": object()} if agent_id == "hermes" else {}
        ),
        agent_default_profile=lambda _agent_id: None,
    )
    request = _make_request_with_hermes(tmp_path, config=config)

    with pytest.raises(HTTPException) as exc_info:
        execution_module.resolve_file_chat_agent_selection(
            request,
            target,
            agent="hermes",
            profile="bad-profile",
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "profile is invalid"


@pytest.mark.asyncio
async def test_execute_file_chat_hermes_without_profile_uses_resolved_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    target_path = repo_root / ".codex-autorunner" / "contextspace" / "spec.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("before\n", encoding="utf-8")
    target = parse_target(repo_root, "contextspace:spec")

    async def fake_get_or_create_interrupt_event(
        _request, _state_key: str
    ) -> asyncio.Event:
        return asyncio.Event()

    async def fake_update_turn_state(*args, **kwargs) -> None:
        return None

    observed: dict[str, object] = {}

    async def fake_execute_harness_turn(
        _request,
        fake_repo_root: Path,
        prompt: str,
        _interrupt_event: asyncio.Event,
        *,
        agent_id: str,
        profile: str | None,
        thread_key: str | None,
        **kwargs,
    ) -> dict[str, str]:
        observed["agent_id"] = agent_id
        observed["profile"] = profile
        observed["thread_key"] = thread_key
        match = re.search(r"<path>\n(.+?)\n</path>", prompt, re.DOTALL)
        assert match is not None
        draft_path = fake_repo_root / match.group(1).strip()
        draft_path.write_text("after\n", encoding="utf-8")
        return {
            "status": "ok",
            "agent_message": "updated via hermes",
            "message": "updated via hermes",
            "thread_id": "hermes-thread-default",
            "turn_id": "hermes-turn-default",
        }

    monkeypatch.setattr(
        runtime, "get_or_create_interrupt_event", fake_get_or_create_interrupt_event
    )
    monkeypatch.setattr(runtime, "update_turn_state", fake_update_turn_state)

    def _resolve_profile(_request, _agent_id, requested_profile, default_profile=None):
        _ = default_profile
        return requested_profile or "m4-default"

    monkeypatch.setattr(
        execution_module,
        "resolve_requested_agent_profile",
        _resolve_profile,
    )
    monkeypatch.setattr(
        execution_module,
        "execute_harness_turn",
        fake_execute_harness_turn,
    )

    result = await execute_file_chat(
        _make_request_with_hermes(repo_root),
        repo_root,
        target,
        "edit the file",
        agent="hermes",
        profile=None,
    )

    assert result["status"] == "ok"
    assert observed["agent_id"] == "hermes"
    assert observed["profile"] == "m4-default"
    assert (
        observed["thread_key"]
        == "file_chat.hermes.profile.m4-default.contextspace_spec.md"
    )


@pytest.mark.asyncio
async def test_execute_file_chat_hermes_thread_key_differs_by_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    target_path = repo_root / ".codex-autorunner" / "contextspace" / "decisions.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("before\n", encoding="utf-8")
    target = parse_target(repo_root, "contextspace:decisions")

    async def fake_get_or_create_interrupt_event(
        _request, _state_key: str
    ) -> asyncio.Event:
        return asyncio.Event()

    async def fake_update_turn_state(*args, **kwargs) -> None:
        return None

    thread_keys: list[str | None] = []

    async def fake_execute_harness_turn(
        _request,
        fake_repo_root: Path,
        prompt: str,
        _interrupt_event: asyncio.Event,
        *,
        agent_id: str,
        profile: str | None,
        thread_key: str | None,
        **kwargs,
    ) -> dict[str, str]:
        thread_keys.append(thread_key)
        match = re.search(r"<path>\n(.+?)\n</path>", prompt, re.DOTALL)
        assert match is not None
        draft_path = fake_repo_root / match.group(1).strip()
        draft_path.write_text("after\n", encoding="utf-8")
        return {
            "status": "ok",
            "agent_message": "updated",
            "message": "updated",
            "thread_id": "hermes-thread-x",
            "turn_id": "hermes-turn-x",
        }

    monkeypatch.setattr(
        runtime, "get_or_create_interrupt_event", fake_get_or_create_interrupt_event
    )
    monkeypatch.setattr(runtime, "update_turn_state", fake_update_turn_state)

    def _resolve_profile(_request, _agent_id, requested_profile, default_profile=None):
        _ = default_profile
        return requested_profile

    monkeypatch.setattr(
        execution_module,
        "resolve_requested_agent_profile",
        _resolve_profile,
    )
    monkeypatch.setattr(
        execution_module,
        "execute_harness_turn",
        fake_execute_harness_turn,
    )

    await execute_file_chat(
        _make_request_with_hermes(repo_root),
        repo_root,
        target,
        "edit",
        agent="hermes",
        profile=None,
    )
    await execute_file_chat(
        _make_request_with_hermes(repo_root),
        repo_root,
        target,
        "edit",
        agent="hermes",
        profile="m4-pma",
    )

    assert len(thread_keys) == 2
    assert thread_keys[0] != thread_keys[1]
    assert "profile" not in (thread_keys[0] or "")
    assert "profile" in (thread_keys[1] or "")
