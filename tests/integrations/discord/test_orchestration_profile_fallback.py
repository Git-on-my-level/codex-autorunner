"""Tests for Discord orchestration Hermes profile thread-target fallback."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.discord.orchestration_profile_fallback import (
    create_thread_target_with_profile_fallback,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d


def test_fallback_validates_definition_before_thread_store(workspace: Path) -> None:
    saw: dict[str, object] = {}

    def get_definition(agent_id: str):
        saw["definition_lookup"] = agent_id
        return SimpleNamespace(capabilities=frozenset({"durable_threads"}))

    def create_thread_target(agent_id: str, root: Path, **kwargs):
        saw["persist"] = (agent_id, root, kwargs)
        return "thread-target"

    orch = SimpleNamespace(
        definition_catalog=SimpleNamespace(get_definition=get_definition),
        thread_store=SimpleNamespace(create_thread_target=create_thread_target),
    )
    result = create_thread_target_with_profile_fallback(
        orch,
        "codex",
        workspace,
        metadata={"agent_profile": "default"},
    )
    assert result == "thread-target"
    assert saw["definition_lookup"] == "codex"
    assert saw["persist"][0] == "codex"


def test_fallback_reraises_unknown_agent_even_with_profile(workspace: Path) -> None:
    orch = SimpleNamespace(
        definition_catalog=SimpleNamespace(get_definition=lambda _aid: None),
        thread_store=SimpleNamespace(
            create_thread_target=lambda *a, **k: pytest.fail("should not persist")
        ),
    )
    orig = KeyError("unknown")

    with pytest.raises(KeyError) as exc_info:
        create_thread_target_with_profile_fallback(
            orch,
            "bad-agent",
            workspace,
            metadata={"agent_profile": "default"},
            key_error=orig,
        )
    assert exc_info.value is orig


def test_fallback_rejects_missing_durable_threads_capability(workspace: Path) -> None:
    orch = SimpleNamespace(
        definition_catalog=SimpleNamespace(
            get_definition=lambda _aid: SimpleNamespace(capabilities=frozenset())
        ),
        thread_store=SimpleNamespace(
            create_thread_target=lambda *a, **k: pytest.fail("should not persist")
        ),
    )
    with pytest.raises(ValueError, match="durable_threads"):
        create_thread_target_with_profile_fallback(
            orch,
            "codex",
            workspace,
            metadata={"agent_profile": "default"},
        )
