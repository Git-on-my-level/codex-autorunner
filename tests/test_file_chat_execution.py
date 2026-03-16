from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core import drafts as draft_utils
from codex_autorunner.core.state import now_iso
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


def _make_request(repo_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                app_server_supervisor=object(),
                app_server_threads=None,
                opencode_supervisor=None,
                engine=SimpleNamespace(repo_root=repo_root),
                app_server_events=None,
            )
        )
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
        "codex_autorunner.surfaces.web.routes.file_chat_routes.execution.execute_app_server",
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
        "codex_autorunner.surfaces.web.routes.file_chat_routes.execution.execute_app_server",
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
