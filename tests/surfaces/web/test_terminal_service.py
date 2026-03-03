from __future__ import annotations

from codex_autorunner.surfaces.web.services import terminal as terminal_service


def test_select_terminal_subprotocol_picks_supported_token() -> None:
    header = "foo, car-token-b64.abc, bar"
    assert terminal_service.select_terminal_subprotocol(header) == "car-token-b64.abc"
    assert terminal_service.select_terminal_subprotocol("x,car-token") == "car-token"
    assert terminal_service.select_terminal_subprotocol("foo,bar") is None
    assert terminal_service.select_terminal_subprotocol(None) is None


def test_session_key_keeps_codex_default_and_scopes_others() -> None:
    assert terminal_service.session_key("/repo", "codex") == "/repo"
    assert terminal_service.session_key("/repo", " CODEX ") == "/repo"
    assert terminal_service.session_key("/repo", "opencode") == "/repo:opencode"


def test_remove_session_from_repo_mapping_normalizes_keys() -> None:
    mapping = {
        "/repo": "session-1",
        "/repo:CODEX": "session-1",
        "/repo:opencode": "session-2",
        "/repo:OpenCode": "session-3",
    }
    filtered = terminal_service.remove_session_from_repo_mapping(
        mapping, session_id="session-1"
    )
    assert filtered == {"/repo:opencode": "session-3"}
