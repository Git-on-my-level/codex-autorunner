"""Characterization tests for legacy route compatibility surface.

These tests verify the backward-compatible shim behavior in
src/codex_autorunner/routes/ which re-exports from the canonical
src/codex_autorunner/surfaces/web/routes/ modules.
"""

from __future__ import annotations


def test_base_shim_re_exports_canonical_build_base_routes():
    from codex_autorunner.routes import base as legacy_base
    from codex_autorunner.surfaces.web.routes import base as canonical_base

    assert legacy_base.build_base_routes is canonical_base.build_base_routes
    assert legacy_base.build_frontend_routes is canonical_base.build_frontend_routes


def test_flows_shim_re_exports_canonical_build_flow_routes():
    from codex_autorunner.routes import flows as legacy_flows
    from codex_autorunner.surfaces.web.routes import flows as canonical_flows

    assert legacy_flows.build_flow_routes is canonical_flows.build_flow_routes


def test_sessions_shim_re_exports_canonical_build_sessions_routes():
    from codex_autorunner.routes import sessions as legacy_sessions
    from codex_autorunner.surfaces.web.routes import sessions as canonical_sessions

    assert (
        legacy_sessions.build_sessions_routes
        is canonical_sessions.build_sessions_routes
    )


def test_analytics_shim_re_exports_canonical_build_analytics_routes():
    from codex_autorunner.routes import analytics as legacy_analytics
    from codex_autorunner.surfaces.web.routes import analytics as canonical_analytics

    assert (
        legacy_analytics.build_analytics_routes
        is canonical_analytics.build_analytics_routes
    )


def test_system_shim_re_exports_canonical_build_system_routes():
    from codex_autorunner.routes import system as legacy_system
    from codex_autorunner.surfaces.web.routes import system as canonical_system

    assert legacy_system.build_system_routes is canonical_system.build_system_routes


def test_messages_shim_re_exports_canonical_build_messages_routes():
    from codex_autorunner.routes import messages as legacy_messages
    from codex_autorunner.surfaces.web.routes import messages as canonical_messages

    assert (
        legacy_messages.build_messages_routes
        is canonical_messages.build_messages_routes
    )


def test_file_chat_shim_re_exports_canonical_build_file_chat_routes():
    from codex_autorunner.routes import file_chat as legacy_file_chat
    from codex_autorunner.surfaces.web.routes import file_chat as canonical_file_chat

    assert (
        legacy_file_chat.build_file_chat_routes
        is canonical_file_chat.build_file_chat_routes
    )


def test_flows_shim_preserves_module_identity_for_private_helpers():
    from codex_autorunner.routes import flows as legacy_flows
    from codex_autorunner.surfaces.web.routes import flows as canonical_flows

    assert legacy_flows is canonical_flows
    assert legacy_flows._get_flow_controller is canonical_flows._get_flow_controller


def test_system_shim_preserves_module_identity_for_private_helpers():
    from codex_autorunner.routes import system as legacy_system
    from codex_autorunner.surfaces.web.routes import system as canonical_system

    assert legacy_system is canonical_system
    assert (
        legacy_system._normalize_update_target
        is canonical_system._normalize_update_target
    )


def test_messages_shim_preserves_module_identity_for_private_helpers():
    from codex_autorunner.routes import messages as legacy_messages
    from codex_autorunner.surfaces.web.routes import messages as canonical_messages

    assert legacy_messages is canonical_messages
    assert (
        legacy_messages._safe_attachment_name
        is canonical_messages._safe_attachment_name
    )


def test_file_chat_shim_preserves_module_identity_for_private_helpers():
    from codex_autorunner.routes import file_chat as legacy_file_chat
    from codex_autorunner.surfaces.web.routes import file_chat as canonical_file_chat

    assert legacy_file_chat is canonical_file_chat
    assert legacy_file_chat._read_file is canonical_file_chat._read_file


def test_monkeypatch_base_shim_is_visible_to_canonical_code():
    """Verify monkeypatching the legacy shim is visible via _get_pty_session_cls."""
    from codex_autorunner.routes import base as legacy_base
    from codex_autorunner.surfaces.web.routes import base as canonical_base

    class FakePTY:
        pass

    original_get_cls = canonical_base._get_pty_session_cls
    original_pty_session = legacy_base.PTYSession

    try:
        legacy_base.PTYSession = FakePTY
        result_cls = canonical_base._get_pty_session_cls()
        assert result_cls is FakePTY
    finally:
        canonical_base._get_pty_session_cls = original_get_cls
        legacy_base.PTYSession = original_pty_session
