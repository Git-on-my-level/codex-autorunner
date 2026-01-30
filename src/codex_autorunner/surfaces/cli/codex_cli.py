"""Codex CLI helpers for the CLI surface.

Delegates to core.utils to keep logic centralized.
"""

from ...core.utils import (  # noqa: F401
    apply_codex_options,
    extract_flag_value,
    inject_flag,
    supports_flag,
    supports_reasoning,
)

__all__ = [
    "apply_codex_options",
    "extract_flag_value",
    "inject_flag",
    "supports_flag",
    "supports_reasoning",
]
