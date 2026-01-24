from __future__ import annotations

import importlib.metadata

from codex_autorunner.agents.registry import (
    CAR_AGENT_ENTRYPOINT_GROUP,
    CAR_PLUGIN_API_VERSION,
    AgentDescriptor,
    get_registered_agents,
    reload_agents,
)


class _FakeEntryPoint:
    def __init__(self, name: str, obj):
        self.name = name
        self.group = CAR_AGENT_ENTRYPOINT_GROUP
        self._obj = obj

    def load(self):
        return self._obj


class _FakeEntryPoints(list):
    def select(self, *, group: str, **kwargs):
        if group == CAR_AGENT_ENTRYPOINT_GROUP:
            return self
        return _FakeEntryPoints()


def test_load_agent_plugin(monkeypatch):
    plugin = AgentDescriptor(
        id="myagent",
        name="My Agent",
        capabilities=frozenset(["threads"]),
        make_harness=lambda ctx: None,  # type: ignore[return-value]
        plugin_api_version=CAR_PLUGIN_API_VERSION,
    )

    def fake_entry_points():
        return _FakeEntryPoints([_FakeEntryPoint("myagent", plugin)])

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    reload_agents()

    agents = get_registered_agents()
    assert "myagent" in agents
    assert agents["myagent"].name == "My Agent"


def test_skip_agent_plugin_version_mismatch(monkeypatch):
    plugin = AgentDescriptor(
        id="badagent",
        name="Bad Agent",
        capabilities=frozenset(["threads"]),
        make_harness=lambda ctx: None,  # type: ignore[return-value]
        plugin_api_version=CAR_PLUGIN_API_VERSION + 1,
    )

    def fake_entry_points():
        return _FakeEntryPoints([_FakeEntryPoint("badagent", plugin)])

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    reload_agents()

    agents = get_registered_agents()
    assert "badagent" not in agents
