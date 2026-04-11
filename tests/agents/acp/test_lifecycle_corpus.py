from __future__ import annotations

from tests.acp_lifecycle_corpus import load_acp_lifecycle_corpus

from codex_autorunner.core.acp_lifecycle import analyze_acp_lifecycle_message

_ALLOWED_KINDS = {
    "message",
    "output_delta",
    "permission_requested",
    "progress",
    "session",
    "token_usage",
    "turn_started",
    "turn_terminal",
    "unknown",
}
_ALLOWED_RUNTIME_TERMINAL_STATUSES = {None, "ok", "error", "interrupted"}


def test_acp_lifecycle_corpus_is_well_formed_and_matches_shared_parser() -> None:
    seen_names: set[str] = set()

    for case in load_acp_lifecycle_corpus():
        name = str(case["name"])
        raw = dict(case["raw"])
        expected = dict(case["expected"])
        snapshot = analyze_acp_lifecycle_message(raw)

        assert name not in seen_names
        seen_names.add(name)
        assert isinstance(raw.get("method"), str) and raw["method"]
        assert expected["normalized_kind"] in _ALLOWED_KINDS
        assert expected["runtime_terminal_status"] in _ALLOWED_RUNTIME_TERMINAL_STATUSES

        assert snapshot.normalized_kind == expected["normalized_kind"]
        assert snapshot.terminal_status == expected["terminal_status"]
        assert snapshot.runtime_terminal_status == expected["runtime_terminal_status"]
        assert snapshot.uses_turn_id_fallback is expected["uses_turn_id_fallback"]
        assert snapshot.closes_turn_buffer is expected["closes_turn_buffer"]
        assert snapshot.assistant_text == expected["assistant_text"]
        assert snapshot.output_delta == expected["output_delta"]
        assert snapshot.progress_message == expected["progress_message"]
        assert snapshot.error_message == expected["error_message"]
        assert snapshot.session_status == expected["session_status"]
        assert snapshot.session_update_kind == expected["session_update_kind"]
        if "permission_request_id" in expected:
            assert snapshot.permission_request_id == expected["permission_request_id"]
        if "permission_description" in expected:
            assert snapshot.permission_description == expected["permission_description"]
