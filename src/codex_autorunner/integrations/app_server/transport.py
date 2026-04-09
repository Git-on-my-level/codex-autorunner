from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


@dataclass
class ReadLoopState:
    dropping_oversize: bool = False
    drain_limit_reached: bool = False
    oversize_preview: bytearray = field(default_factory=bytearray)
    oversize_bytes_dropped: int = 0


def build_message(
    method: Optional[str] = None,
    *,
    params: Optional[dict[str, object]] = None,
    req_id: Optional[int | str] = None,
    result: Optional[object] = None,
    error: Optional[dict[str, object]] = None,
) -> dict[str, object]:
    message: dict[str, object] = {}
    if req_id is not None:
        message["id"] = req_id
    if method is not None:
        message["method"] = method
    if params is not None:
        message["params"] = params
    if result is not None:
        message["result"] = result
    if error is not None:
        message["error"] = error
    return message


class AppServerReadBuffer:
    def __init__(
        self,
        *,
        max_message_bytes: int,
        oversize_preview_bytes: int,
        max_oversize_drain_bytes: int,
        on_payload_line: Callable[[bytes], Awaitable[None]],
        on_oversize: Callable[..., Awaitable[None]],
    ) -> None:
        self._max_message_bytes = max_message_bytes
        self._oversize_preview_bytes = oversize_preview_bytes
        self._max_oversize_drain_bytes = max_oversize_drain_bytes
        self._on_payload_line = on_payload_line
        self._on_oversize = on_oversize
        self._buffer = bytearray()
        self._state = ReadLoopState()

    async def feed(self, chunk: bytes, *, initializing: bool) -> None:
        if self._state.dropping_oversize:
            await self._drain_oversize_chunk(chunk)
        else:
            await self._collect_chunk(chunk, initializing=initializing)
        if self._state.dropping_oversize:
            return
        await self._drain_buffer_lines()

    async def finalize(self) -> None:
        if self._state.dropping_oversize:
            if self._state.oversize_bytes_dropped:
                await self._on_oversize(
                    bytes_dropped=self._state.oversize_bytes_dropped,
                    preview=self._state.oversize_preview,
                    truncated=True,
                )
            return
        if not self._buffer:
            return
        if len(self._buffer) > self._max_message_bytes:
            await self._on_oversize(
                bytes_dropped=len(self._buffer),
                preview=self._buffer[: self._oversize_preview_bytes],
                truncated=True,
            )
            return
        await self._on_payload_line(self._buffer)

    async def _collect_chunk(self, chunk: bytes, *, initializing: bool) -> None:
        self._buffer.extend(chunk)
        await self._drain_buffer_lines()
        if initializing or len(self._buffer) <= self._max_message_bytes:
            return
        oversized = bytes(self._buffer)
        self._buffer.clear()
        self._state.dropping_oversize = True
        await self._drain_oversize_chunk(oversized)

    async def _drain_oversize_chunk(self, chunk: bytes) -> None:
        newline_index = chunk.find(b"\n")
        if newline_index == -1:
            await self._track_oversize_fragment(chunk)
            return
        before = chunk[: newline_index + 1]
        after = chunk[newline_index + 1 :]
        if not self._state.drain_limit_reached:
            self._append_oversize_preview(before)
            self._state.oversize_bytes_dropped += len(before)
            await self._on_oversize(
                bytes_dropped=self._state.oversize_bytes_dropped,
                preview=self._state.oversize_preview,
            )
        self._state.dropping_oversize = False
        self._state.drain_limit_reached = False
        self._state.oversize_preview = bytearray()
        self._state.oversize_bytes_dropped = 0
        if after:
            self._buffer.extend(after)
            await self._drain_buffer_lines()
            if len(self._buffer) > self._max_message_bytes:
                self._state.oversize_preview = bytearray(
                    self._buffer[: self._oversize_preview_bytes]
                )
                self._state.oversize_bytes_dropped = len(self._buffer)
                self._buffer.clear()
                self._state.dropping_oversize = True

    async def _track_oversize_fragment(self, chunk: bytes) -> None:
        if self._state.drain_limit_reached:
            return
        self._append_oversize_preview(chunk)
        self._state.oversize_bytes_dropped += len(chunk)
        if self._state.oversize_bytes_dropped >= self._max_oversize_drain_bytes:
            await self._on_oversize(
                bytes_dropped=self._state.oversize_bytes_dropped,
                preview=self._state.oversize_preview,
                aborted=True,
                drain_limit=self._max_oversize_drain_bytes,
            )
            self._state.drain_limit_reached = True

    def _append_oversize_preview(self, chunk: bytes) -> None:
        if len(self._state.oversize_preview) >= self._oversize_preview_bytes:
            return
        remaining = self._oversize_preview_bytes - len(self._state.oversize_preview)
        self._state.oversize_preview.extend(chunk[:remaining])

    async def _drain_buffer_lines(self) -> None:
        while True:
            newline_index = self._buffer.find(b"\n")
            if newline_index == -1:
                return
            line = self._buffer[:newline_index]
            del self._buffer[: newline_index + 1]
            await self._on_payload_line(line)


__all__ = ["AppServerReadBuffer", "ReadLoopState", "build_message"]
