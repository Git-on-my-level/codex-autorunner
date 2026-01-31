from __future__ import annotations

from codex_autorunner.integrations.telegram.handlers.commands.execution import (
    ExecutionCommands,
)


def test_telegram_car_context_always_injected() -> None:
    handler = ExecutionCommands()
    prompt, injected = handler._maybe_inject_car_context(
        "fix failing tests in src/foo.py"
    )

    assert injected is True
    assert "<injected context>" in prompt
    assert "</injected context>" in prompt
    assert ".codex-autorunner/ABOUT_CAR.md" in prompt
    assert "fix failing tests in src/foo.py" in prompt
