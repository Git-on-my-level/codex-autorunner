"""Characterization tests for legacy route compatibility surface.

These tests verify the backward-compatible shim behavior in
src/codex_autorunner/routes/ which re-exports from the canonical
src/codex_autorunner/surfaces/web/routes/ modules.
"""

from __future__ import annotations

import sys


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


def test_all_shims_use_wildcard_re_export_pattern():
    """Verify all shim modules use consistent wildcard re-export pattern."""
    shim_files = [
        "base",
        "sessions",
        "analytics",
        "app_server",
        "usage",
        "repos",
        "settings",
        "shared",
        "review",
        "voice",
        "agents",
        "flows",
        "system",
        "messages",
        "file_chat",
    ]

    for shim_name in shim_files:
        module = sys.modules.get(f"codex_autorunner.routes.{shim_name}")
        if module is None:
            __import__(f"codex_autorunner.routes.{shim_name}")
            module = sys.modules[f"codex_autorunner.routes.{shim_name}"]

        source_lines = open(module.__file__, encoding="utf-8").read().splitlines()

        assert any(
            "from ..surfaces.web.routes" in line for line in source_lines
        ), f"Shim {shim_name} should use relative import from canonical routes"

        assert "sys.modules[__name__]" not in "".join(
            source_lines
        ), f"Shim {shim_name} should not use sys.modules aliasing"


def test_monkeypatch_base_shim_is_visible_to_canonical_code():
    """Verify monkeypatching the legacy shim is visible via _get_pty_session_cls."""
    from codex_autorunner.routes import base as legacy_base
    from codex_autorunner.surfaces.web.routes import base as canonical_base

    class FakePTY:
        pass

    original_get_cls = canonical_base._get_pty_session_cls

    try:
        legacy_base.PTYSession = FakePTY
        result_cls = canonical_base._get_pty_session_cls()
        assert result_cls is FakePTY
    finally:
        canonical_base._get_pty_session_cls = original_get_cls
        if hasattr(legacy_base, "PTYSession"):
            delattr(legacy_base, "PTYSession")
