"""Regression: reinject prompt.md when opening a fresh backend conversation mid-delta turn."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from codex_autorunner.core.orchestration import FreshConversationRequiredError
from codex_autorunner.core.pma_origin import PmaOriginContext
from codex_autorunner.surfaces.web.routes.pma_routes.chat_queue_execution import (
    _resolve_profile_with_stale_pma_origin_fallback,
    resolve_pma_execution_origin,
    resolve_pma_session_key,
)
from codex_autorunner.surfaces.web.routes.pma_routes.chat_runtime_execution import (
    execute_harness_turn,
)


class _HarnessTurnStub:
    def __init__(self, conversation_id: str, turn_id: str = "turn-1") -> None:
        self.conversation_id = conversation_id
        self.turn_id = turn_id


class _HarnessResultStub:
    assistant_text = "ok"
    raw_events: list[object] = []
    errors: list[str] = []


class _ThreadRegistryStub:
    def __init__(self, thread_id: str | None) -> None:
        self.thread_id = thread_id
        self.set_calls: list[tuple[str, str]] = []

    def get_thread_id(self, thread_key: str) -> str | None:
        return self.thread_id

    def set_thread_id(self, thread_key: str, thread_id: str) -> None:
        self.thread_id = thread_id
        self.set_calls.append((thread_key, thread_id))

    def reset_thread(self, thread_key: str) -> None:
        self.thread_id = None
        _ = thread_key


@pytest.mark.asyncio
async def test_fresh_conversation_uses_rebuild_prompt_for_base_prompt_injection(
    tmp_path: Path,
) -> None:
    """When resume fails, start_turn receives rebuild_prompt(True), not the delta-only prompt."""
    hub_root = tmp_path
    registry = _ThreadRegistryStub(thread_id="gone-conv")
    rebuild_calls: list[bool] = []
    prompts_seen: list[str] = []

    async def rebuild_prompt(force_full_base_prompt: bool) -> str:
        rebuild_calls.append(force_full_base_prompt)
        return (
            "DELTA_PROMPT"
            if not force_full_base_prompt
            else "DELTA_PROMPT\n\nCORE PMA FROM PROMPT_MD\n\n"
        )

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
            self, _repo_root: Path, conversation_id: str, prompt: str, *args, **kwargs
        ):
            prompts_seen.append(prompt)
            return _HarnessTurnStub(conversation_id, "turn-fresh")

        async def wait_for_turn(
            self, _repo_root: Path, conversation_id: str, turn_id: str, timeout=None
        ):
            return _HarnessResultStub()

    result = await execute_harness_turn(
        Harness(),
        hub_root,
        "DELTA_PROMPT",
        asyncio.Event(),
        thread_registry=registry,
        thread_key="web.pma.test",
        rebuild_prompt=rebuild_prompt,
    )

    assert result["status"] == "ok"
    assert rebuild_calls == [True]
    assert prompts_seen == ["DELTA_PROMPT\n\nCORE PMA FROM PROMPT_MD\n\n"]


def test_resolve_pma_session_key_isolates_automation_from_interactive_pma() -> None:
    interactive = resolve_pma_session_key(
        "hermes",
        "m4-pma",
        automation_trigger=False,
    )
    automation = resolve_pma_session_key(
        "hermes",
        "m4-pma",
        automation_trigger=True,
    )

    assert interactive == "pma.hermes.profile.m4-pma"
    assert automation == "pma.hermes.profile.m4-pma.automation"
    assert automation != interactive


def test_resolve_pma_session_key_reuses_interactive_session_for_origin_wakeups() -> (
    None
):
    session_key = resolve_pma_session_key(
        "hermes",
        "m4-pma",
        automation_trigger=True,
        pma_origin=PmaOriginContext(thread_id="backend-thread-123"),
    )

    assert session_key == "pma.hermes.profile.m4-pma"


def test_resolve_pma_execution_origin_resumes_matching_origin_session() -> None:
    execution_origin = resolve_pma_execution_origin(
        "hermes",
        "m4-pma",
        automation_trigger=True,
        wake_up={
            "metadata": {
                "pma_origin": {
                    "thread_id": "backend-thread-123",
                    "agent": "hermes",
                    "profile": "m4-pma",
                }
            }
        },
    )

    assert execution_origin.session_key == "pma.hermes.profile.m4-pma"
    assert execution_origin.backend_thread_id == "backend-thread-123"


def test_resolve_pma_execution_origin_ignores_mismatched_origin_session() -> None:
    execution_origin = resolve_pma_execution_origin(
        "codex",
        "m4-pma",
        automation_trigger=True,
        wake_up={
            "metadata": {
                "pma_origin": {
                    "thread_id": "backend-thread-123",
                    "agent": "hermes",
                    "profile": "m4-pma",
                }
            }
        },
    )

    assert execution_origin.session_key == "pma.profile.m4-pma.automation"
    assert execution_origin.backend_thread_id is None


def test_queue_profile_resolution_falls_back_when_stored_origin_profile_is_stale() -> (
    None
):
    """Invalid persisted ``pma_origin.profile`` must not fail the lane when payload omits profile."""

    class _Config:
        def agent_profiles(self, agent_id: str) -> dict[str, object]:
            _ = agent_id
            return {"good": {}}

        def agent_default_profile(self, agent_id: str) -> str:
            _ = agent_id
            return "good"

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(config=_Config()))
    )
    origin = PmaOriginContext(
        thread_id="t1",
        agent="codex",
        profile="removed-profile",
    )

    resolved = _resolve_profile_with_stale_pma_origin_fallback(
        request,
        "codex",
        None,
        origin,
        default_profile=None,
    )
    assert resolved == "good"


def test_queue_profile_resolution_does_not_swallow_explicit_invalid_payload_profile() -> (
    None
):
    class _Config:
        def agent_profiles(self, agent_id: str) -> dict[str, object]:
            _ = agent_id
            return {"good": {}}

        def agent_default_profile(self, agent_id: str) -> str:
            _ = agent_id
            return "good"

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(config=_Config()))
    )
    origin = PmaOriginContext(thread_id="t1", agent="codex", profile="good")

    with pytest.raises(HTTPException) as excinfo:
        _resolve_profile_with_stale_pma_origin_fallback(
            request,
            "codex",
            "bad-payload-profile",
            origin,
            default_profile=None,
        )
    assert excinfo.value.status_code == 400
