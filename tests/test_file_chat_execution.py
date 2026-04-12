from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from codex_autorunner.core import drafts as draft_utils
from codex_autorunner.core.state import now_iso
from codex_autorunner.surfaces.web.routes.file_chat_routes import (
    execution as execution_module,
)
from codex_autorunner.surfaces.web.routes.file_chat_routes import runtime
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

    assert result == {
        "status": "error",
        "detail": (
            "Agent 'broken' does not support file-chat execution "
            "(missing capability: message_turns)"
        ),
    }


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
