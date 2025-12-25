"""
API routes module - now re-exports from the modular routes package.

This module is kept for backward compatibility. New code should import
from codex_autorunner.routes directly.
"""

from __future__ import annotations

from .routes import build_repo_router
from .routes.shared import build_codex_terminal_cmd
from .pty_session import ActiveSession

__all__ = [
    "ActiveSession",
    "build_codex_terminal_cmd",
    "build_repo_router",
]
