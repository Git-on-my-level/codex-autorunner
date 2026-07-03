from __future__ import annotations

import pytest

from codex_autorunner.agents.registry import (
    _make_omp_harness,
    _RequestedAgentContext,
)


class _PiOmpAliasConfig:
    def agent_binary(self, agent_id: str, *, profile: str | None = None) -> str:
        assert profile is None
        if agent_id == "pi":
            return "/opt/pi/omp"
        if agent_id == "omp":
            return "omp"
        raise KeyError(agent_id)

    def agent_backend(self, agent_id: str) -> str:
        if agent_id == "pi":
            return "omp"
        return agent_id


def test_make_omp_harness_uses_requested_alias_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class _FakeOMPSupervisor:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            observed["command"] = list(command)
            observed["kwargs"] = dict(kwargs)

    monkeypatch.setattr(
        "codex_autorunner.agents.omp.supervisor.OMPSupervisor",
        _FakeOMPSupervisor,
    )

    config = _PiOmpAliasConfig()
    ctx = _RequestedAgentContext(config, agent_id="pi")
    harness = _make_omp_harness(ctx)

    assert harness is not None
    assert observed["command"] == ["/opt/pi/omp", "acp"]
