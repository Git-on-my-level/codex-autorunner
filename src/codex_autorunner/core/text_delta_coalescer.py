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
