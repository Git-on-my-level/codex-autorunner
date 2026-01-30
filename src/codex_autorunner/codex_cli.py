"""Backward-compatible Codex CLI helpers.

Delegates to core.utils to avoid duplicated logic.
"""

from .core.utils import (  # noqa: F401
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
