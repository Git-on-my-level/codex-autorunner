"""
API routes module - now re-exports from the modular routes package.

This module is kept for backward compatibility. New code should import
from codex_autorunner.routes directly.
"""

from __future__ import annotations

from .routes import build_repo_router
from .pty_session import ActiveSession

__all__ = [
    "ActiveSession",
    "build_codex_terminal_cmd",
    "build_repo_router",
]


def build_codex_terminal_cmd(engine, *, resume_mode: bool) -> list[str]:
    """
    Back-compat wrapper for terminal command construction.

    Kept here so older imports can continue to reference this helper.
    """
    if resume_mode:
        return [
            engine.config.codex_binary,
            "--yolo",
            "resume",
            *engine.config.codex_terminal_args,
        ]

    cmd = [
        engine.config.codex_binary,
        "--yolo",
        *engine.config.codex_terminal_args,
    ]
    return cmd
