from __future__ import annotations

from typing import Optional


class TextDeltaCoalescer:
    def __init__(self, flush_on_newline: bool = False) -> None:
        self._buffer: str = ""
        self._flush_on_newline = flush_on_newline

    def add(self, delta: Optional[str]) -> None:
        if not isinstance(delta, str) or not delta:
            return
        self._buffer += delta

    def flush_lines(self) -> list[str]:
        lines: list[str] = []
        if not self._buffer:
            return lines

        parts = self._buffer.split("\n")
        if len(parts) == 1:
            return lines

        lines.extend(parts[:-1])
        self._buffer = parts[-1]
        return lines

    def flush_all(self) -> list[str]:
        lines: list[str] = []
        if not self._buffer:
            return lines

        for line in self._buffer.splitlines():
            lines.append(line)
        self._buffer = ""
        return lines

    def get_buffer(self) -> str:
        return self._buffer

    def clear(self) -> None:
        self._buffer = ""


class StreamingTextCoalescer:
    """
    Coalesce small streaming text deltas into larger, readable chunks.

    - Flushes whole lines as soon as a newline is observed.
    - Flushes buffered text when it grows past `min_flush_chars` and ends at a
      natural boundary (whitespace or sentence punctuation).
    - Enforces an upper bound on the buffer size to avoid unbounded growth.
    """

    def __init__(self, min_flush_chars: int = 32, max_buffer_chars: int = 2048):
        self._buffer: str = ""
        self._min_flush_chars = max(1, int(min_flush_chars))
        self._max_buffer_chars = max(self._min_flush_chars, int(max_buffer_chars))

    def add(self, delta: Optional[str]) -> list[str]:
        chunks: list[str] = []
        if not isinstance(delta, str) or not delta:
            return chunks

        self._buffer += delta
        self._flush_complete_lines(chunks)
        self._flush_if_boundary(chunks)
        self._flush_if_oversized(chunks)
        return chunks

    def flush(self) -> list[str]:
        if not self._buffer:
            return []
        chunk = self._buffer
        self._buffer = ""
        return [chunk]

    def _flush_complete_lines(self, chunks: list[str]) -> None:
        while "\n" in self._buffer:
            line, remainder = self._buffer.split("\n", 1)
            # Preserve the newline boundary so the caller keeps the same text.
            chunks.append(f"{line}\n")
            self._buffer = remainder

    def _flush_if_boundary(self, chunks: list[str]) -> None:
        if len(self._buffer) < self._min_flush_chars:
            return
        last_char = self._buffer[-1]
        if last_char.isspace() or last_char in ".!?;:":
            chunks.append(self._buffer)
            self._buffer = ""

    def _flush_if_oversized(self, chunks: list[str]) -> None:
        while len(self._buffer) > self._max_buffer_chars:
            chunks.append(self._buffer[: self._max_buffer_chars])
            self._buffer = self._buffer[self._max_buffer_chars :]
