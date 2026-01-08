from codex_autorunner.integrations.telegram.helpers import is_interrupt_status


def test_is_interrupt_status_matches_known_values() -> None:
    for status in ("interrupted", "cancelled", "canceled", "aborted"):
        assert is_interrupt_status(status)


def test_is_interrupt_status_rejects_non_interrupt_values() -> None:
    for status in (None, "", "completed", "failed", "running"):
        assert not is_interrupt_status(status)


def test_is_interrupt_status_is_case_insensitive() -> None:
    assert is_interrupt_status("Interrupted")
    assert is_interrupt_status("CANCELLED")
    assert is_interrupt_status("CANCELED")
