from types import SimpleNamespace

from codex_autorunner.integrations.telegram.handlers.commands.shared import (
    SharedHelpers,
)


class _HelperStub(SharedHelpers):
    def __init__(self, config: object) -> None:
        self._config = config


def test_opencode_stall_timeout_defaults_to_none_when_config_missing() -> None:
    helper = _HelperStub(SimpleNamespace())
    assert helper._opencode_session_stall_timeout_seconds() is None


def test_opencode_stall_timeout_ignores_invalid_values() -> None:
    helper = _HelperStub(
        SimpleNamespace(
            opencode=SimpleNamespace(session_stall_timeout_seconds="not-a-number")
        )
    )
    assert helper._opencode_session_stall_timeout_seconds() is None


def test_opencode_stall_timeout_parses_positive_numbers() -> None:
    helper = _HelperStub(
        SimpleNamespace(opencode=SimpleNamespace(session_stall_timeout_seconds=15))
    )
    assert helper._opencode_session_stall_timeout_seconds() == 15.0
