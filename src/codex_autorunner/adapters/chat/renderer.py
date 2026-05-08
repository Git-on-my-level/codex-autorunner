"""Text rendering contract for platform-specific output formatting.

This module belongs to the adapter layer and keeps rendering concerns separate
from orchestration logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class RenderedText:
    """Rendered output fragment with optional platform parse mode metadata."""

    text: str
    parse_mode: Optional[str] = None


@runtime_checkable
class TextRenderer(Protocol):
    """Protocol for formatting and splitting user-visible text."""

    def render_text(
        self, text: str, *, parse_mode: Optional[str] = None
    ) -> RenderedText:
        """Render plain text into platform-ready output."""

    def split_text(
        self,
        rendered: RenderedText,
        *,
        max_length: Optional[int] = None,
    ) -> tuple[RenderedText, ...]:
        """Split rendered output into deliverable chunks for platform limits."""
