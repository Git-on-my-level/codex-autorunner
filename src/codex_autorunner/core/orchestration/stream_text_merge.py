from __future__ import annotations

from dataclasses import dataclass

_NO_SPACE_BEFORE = frozenset(",.;:!?)]}")
_NO_SPACE_AFTER = frozenset("([{")


def _needs_readable_boundary(current: str, incoming: str) -> bool:
    if not current or not incoming:
        return False
    previous = current[-1]
    next_char = incoming[0]
    if previous.isspace() or next_char.isspace():
        return False
    if next_char in _NO_SPACE_BEFORE:
        return False
    if previous in _NO_SPACE_AFTER:
        return False
    if incoming and all(char == "*" for char in incoming):
        return previous in {".", ":", ";", "!", "?"}
    if previous.isalnum() and (next_char.isalnum() or next_char in {"`", "*"}):
        return True
    if previous in {"`", "*"} and next_char.isalnum():
        return True
    if previous in {".", ":", ";", "!", "?"} and next_char in {"`", "*"}:
        return True
    return False


def append_assistant_stream_text_readably(current: str, incoming: str) -> str:
    """Append tokenized assistant chunks without erasing word boundaries."""

    if not incoming:
        return current
    if not current:
        return incoming
    separator = " " if _needs_readable_boundary(current, incoming) else ""
    return f"{current}{separator}{incoming}"


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

    def append_delta(self, text: str, *, preserve_word_boundaries: bool = False) -> str:
        """Record a strict append-only stream delta."""
        if isinstance(text, str) and text:
            if preserve_word_boundaries:
                self.stream_text = append_assistant_stream_text_readably(
                    self.stream_text, text
                )
            else:
                self.stream_text = f"{self.stream_text}{text}"
        return self.text

    def merge_snapshot(
        self, text: str, *, preserve_word_boundaries: bool = False
    ) -> str:
        """Record a stream chunk that may be cumulative or overlap prior chunks."""
        if isinstance(text, str) and text:
            merged = merge_assistant_stream_text(self.stream_text, text)
            if (
                preserve_word_boundaries
                and merged == f"{self.stream_text}{text}"
                and not text.startswith(self.stream_text)
            ):
                merged = append_assistant_stream_text_readably(self.stream_text, text)
            self.stream_text = merged
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


@dataclass
class AssistantOutputState(AssistantTextAccumulator):
    """Reduced assistant output state, distinct from append-only timelines."""

    def note_stream_delta(
        self, text: str, *, preserve_word_boundaries: bool = False
    ) -> str:
        return self.append_delta(
            text, preserve_word_boundaries=preserve_word_boundaries
        )

    def note_stream_snapshot(
        self, text: str, *, preserve_word_boundaries: bool = False
    ) -> str:
        return self.merge_snapshot(
            text, preserve_word_boundaries=preserve_word_boundaries
        )

    def note_final_message(self, text: str) -> str:
        return self.replace_final(text)
