from __future__ import annotations

from dataclasses import dataclass


def merge_assistant_stream_text(current: str, incoming: str) -> str:
    """Merge overlapping streamed assistant chunks without duplicating prefixes."""
    if not incoming:
        return current
    if not current:
        return incoming
    if incoming == current:
        return current
    if len(incoming) > len(current) and incoming.startswith(current):
        return incoming
    max_overlap = min(len(current), max(len(incoming) - 1, 0))
    for overlap in range(max_overlap, 0, -1):
        if current[-overlap:] == incoming[:overlap]:
            return f"{current}{incoming[overlap:]}"
    return f"{current}{incoming}"


@dataclass
class AssistantTextAccumulator:
    """Shared assistant text reducer for stream and terminal message text."""

    stream_text: str = ""
    final_text: str = ""

    def append_delta(self, text: str) -> str:
        """Record a strict append-only stream delta."""
        if isinstance(text, str) and text:
            self.stream_text = f"{self.stream_text}{text}"
        return self.text

    def merge_snapshot(self, text: str) -> str:
        """Record a stream chunk that may be cumulative or overlap prior chunks."""
        if isinstance(text, str) and text:
            self.stream_text = merge_assistant_stream_text(self.stream_text, text)
        return self.text

    def replace_final(self, text: str) -> str:
        """Record the canonical terminal assistant message."""
        if isinstance(text, str):
            self.final_text = text
        return self.text

    @property
    def text(self) -> str:
        if self.final_text.strip():
            return self.final_text
        return self.stream_text
