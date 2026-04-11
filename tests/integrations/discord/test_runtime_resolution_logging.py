from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.discord_message_turns_support import (
    _config,
    _FakeGateway,
    _FakeOutboxManager,
    _FakeRest,
)

import codex_autorunner.integrations.discord.message_turns as discord_message_turns_module
from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


def _build_service(tmp_path: Path) -> DiscordBotService:
    store = DiscordStateStore(tmp_path / "state.sqlite3")
    service = DiscordBotService(
        _config(tmp_path, max_message_length=100),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._discord_thread_orchestration_service = None
    service._discord_managed_thread_orchestration_service = None
    return service


def test_discord_harness_factory_logs_canonical_hermes_runtime_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_logs: list[dict[str, Any]] = []

    def _fake_make_harness(_ctx: Any) -> SimpleNamespace:
        return SimpleNamespace(
            _supervisor=SimpleNamespace(
                launch_command=("hermes", "-p", "m4-pma", "acp")
            )
        )

    descriptor = AgentDescriptor(
        id="hermes",
        name="Hermes",
        capabilities=("durable_threads",),
        make_harness=_fake_make_harness,
        healthcheck=lambda: True,
        runtime_kind="hermes",
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "get_registered_agents",
        lambda context=None: {"hermes": descriptor},
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_agent_runtime",
        lambda *args, **kwargs: SimpleNamespace(
            logical_agent_id="hermes",
            logical_profile="m4-pma",
            runtime_agent_id="hermes",
            runtime_profile="m4-pma",
            resolution_kind="canonical_profile",
        ),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "log_event",
        lambda _logger, _level, event, **fields: captured_logs.append(
            {"event": event, **fields}
        ),
    )

    orch = discord_message_turns_module.build_discord_thread_orchestration_service(
        _build_service(tmp_path)
    )
    orch._harness_for_agent("hermes", "m4-pma")

    resolution_logs = [
        record
        for record in captured_logs
        if record.get("event") == "discord.hermes.runtime_resolution"
    ]
    assert resolution_logs == [
        {
            "event": "discord.hermes.runtime_resolution",
            "requested_agent_id": "hermes",
            "requested_profile": "m4-pma",
            "logical_agent_id": "hermes",
            "logical_profile": "m4-pma",
            "resolution_kind": "canonical_profile",
            "runtime_agent_id": "hermes",
            "runtime_profile": "m4-pma",
            "launch_command": ["hermes", "-p", "m4-pma", "acp"],
        }
    ]


def test_discord_harness_factory_logs_alias_hermes_runtime_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_logs: list[dict[str, Any]] = []

    def _fake_make_harness(_ctx: Any) -> SimpleNamespace:
        return SimpleNamespace(
            _supervisor=SimpleNamespace(
                launch_command=("hermes", "-p", "hermes-m4-pma", "acp")
            )
        )

    descriptor = AgentDescriptor(
        id="hermes-m4-pma",
        name="Hermes Alias",
        capabilities=("durable_threads",),
        make_harness=_fake_make_harness,
        healthcheck=lambda: True,
        runtime_kind="hermes",
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "get_registered_agents",
        lambda context=None: {"hermes-m4-pma": descriptor},
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_agent_runtime",
        lambda *args, **kwargs: SimpleNamespace(
            logical_agent_id="hermes",
            logical_profile="m4-pma",
            runtime_agent_id="hermes-m4-pma",
            runtime_profile=None,
            resolution_kind="alias_profile",
        ),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "log_event",
        lambda _logger, _level, event, **fields: captured_logs.append(
            {"event": event, **fields}
        ),
    )

    orch = discord_message_turns_module.build_discord_thread_orchestration_service(
        _build_service(tmp_path)
    )
    orch._harness_for_agent("hermes", "m4-pma")

    resolution_logs = [
        record
        for record in captured_logs
        if record.get("event") == "discord.hermes.runtime_resolution"
    ]
    assert resolution_logs == [
        {
            "event": "discord.hermes.runtime_resolution",
            "requested_agent_id": "hermes",
            "requested_profile": "m4-pma",
            "logical_agent_id": "hermes",
            "logical_profile": "m4-pma",
            "resolution_kind": "alias_profile",
            "runtime_agent_id": "hermes-m4-pma",
            "runtime_profile": None,
            "launch_command": ["hermes", "-p", "hermes-m4-pma", "acp"],
        }
    ]
