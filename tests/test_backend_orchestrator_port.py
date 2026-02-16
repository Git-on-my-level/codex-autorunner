from __future__ import annotations

import inspect

from codex_autorunner.core.ports.backend_orchestrator import BackendOrchestrator


def test_backend_orchestrator_port_has_no_protocol_specific_names() -> None:
    members = {
        name.lower()
        for name, value in inspect.getmembers(BackendOrchestrator)
        if callable(value)
    }
    assert all("app_server" not in name for name in members)
    assert all("opencode" not in name for name in members)
