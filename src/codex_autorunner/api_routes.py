"""
API routes module - now re-exports from the modular routes package.

This module is kept for backward compatibility. New code should import
from codex_autorunner.routes directly.
"""

from __future__ import annotations

from .about_car import ABOUT_CAR_REL_PATH, ensure_about_car_file
from .routes import build_repo_router
from .routes.shared import (
    codex_supports_input_flag as _shared_codex_supports_input_flag,
)
from .pty_session import ActiveSession

__all__ = [
    "ActiveSession",
    "build_codex_terminal_cmd",
    "build_repo_router",
]


def _codex_supports_input_flag(binary: str) -> bool:
    return _shared_codex_supports_input_flag(binary)


def build_codex_terminal_cmd(engine, *, resume_mode: bool) -> list[str]:
    """
    Back-compat wrapper for terminal command construction.

    Kept here so older imports and tests can monkeypatch
    `_codex_supports_input_flag` in this module.
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
    try:
        about_path = engine.repo_root / ABOUT_CAR_REL_PATH
        if not about_path.exists():
            ensure_about_car_file(engine.config)
        if _codex_supports_input_flag(engine.config.codex_binary):
            cmd.extend(["--input", str(about_path)])
        else:
            about_text = about_path.read_text(encoding="utf-8")
            if about_text.strip():
                cmd.append(about_text)
    except Exception:
        pass
    return cmd
