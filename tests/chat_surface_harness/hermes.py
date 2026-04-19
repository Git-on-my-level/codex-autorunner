from __future__ import annotations

import logging
from typing import Any, Sequence

import codex_autorunner.agents.registry as registry_module
from codex_autorunner.agents.registry import AgentDescriptor
from tests.chat_surface_lab import backend_runtime as chat_surface_backend_runtime
from tests.chat_surface_lab.backend_runtime import HermesFixtureRuntime


def fake_acp_command(scenario: str) -> list[str]:
    return chat_surface_backend_runtime.fake_acp_command(scenario)


def patch_hermes_registry(
    monkeypatch: Any,
    *,
    scenario: str,
    targets: Sequence[Any],
) -> HermesFixtureRuntime:
    runtime = HermesFixtureRuntime(scenario)
    descriptor = runtime.descriptor()

    def _registry(_context: Any = None) -> dict[str, AgentDescriptor]:
        return {"hermes": descriptor}

    monkeypatch.setattr(registry_module, "get_registered_agents", _registry)
    for target in targets:
        monkeypatch.setattr(target, "get_registered_agents", _registry)
    return runtime


def logger_for(name: str) -> logging.Logger:
    return logging.getLogger(f"test.chat_surface_harness.{name}")
