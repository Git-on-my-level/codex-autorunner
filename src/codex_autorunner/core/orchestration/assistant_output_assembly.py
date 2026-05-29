from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from .stream_text_merge import (
    AssistantOutputState,
    append_assistant_stream_text_readably,
)

AssistantOutputEventKind = Literal[
    "delta",
    "snapshot",
    "final_message",
    "commentary",
    "reasoning",
]

_NO_SCOPE = "<no-scope>"


@dataclass(frozen=True)
class AssistantOutputEvent:
    kind: AssistantOutputEventKind
    text: str
    scope: Optional[str] = None
    preserve_word_boundaries: bool = False


class AssistantOutputAssembler:
    """Reduce runtime-native assistant text events into canonical user output.

    Runtime adapters still own protocol-specific parsing. This class owns the
    shared invariant after parsing: only current-turn assistant answer text
    participates in terminal output; commentary/reasoning stay out, final
    messages outrank stream text, and scoped stream fragments do not bleed
    across message/part boundaries.
    """

    def __init__(self) -> None:
        self._scoped_streams: dict[str, AssistantOutputState] = {}
        self._scope_order: list[str] = []
        self._final_text = ""
        self._final_scope: Optional[str] = None

    def note(self, event: AssistantOutputEvent) -> None:
        text = event.text if isinstance(event.text, str) else ""
        if event.kind in {"commentary", "reasoning"}:
            return
        if event.kind == "final_message":
            self._final_text = text if text.strip() else ""
            self._final_scope = self._scope_key(event.scope)
            return
        if not text:
            return
        state = self._stream_state(event.scope)
        if event.kind == "snapshot":
            state.note_stream_snapshot(
                text,
                preserve_word_boundaries=event.preserve_word_boundaries,
            )
        else:
            state.note_stream_delta(
                text,
                preserve_word_boundaries=event.preserve_word_boundaries,
            )

    @property
    def text(self) -> str:
        if self._final_text.strip():
            return self._final_text.strip()
        return self.stream_text.strip()

    @property
    def stream_text(self) -> str:
        if self._final_scope is not None:
            state = self._scoped_streams.get(self._final_scope)
            return state.text if state is not None else ""
        merged = ""
        for scope in self._scope_order:
            state = self._scoped_streams.get(scope)
            if state is None or not state.text:
                continue
            merged = append_assistant_stream_text_readably(merged, state.text)
        return merged

    def _stream_state(self, scope: Optional[str]) -> AssistantOutputState:
        key = self._scope_key(scope)
        state = self._scoped_streams.get(key)
        if state is None:
            state = AssistantOutputState()
            self._scoped_streams[key] = state
            self._scope_order.append(key)
        return state

    @staticmethod
    def _scope_key(scope: Optional[str]) -> str:
        normalized = str(scope or "").strip()
        return normalized or _NO_SCOPE


def assemble_assistant_output(events: list[AssistantOutputEvent]) -> str:
    assembler = AssistantOutputAssembler()
    for event in events:
        assembler.note(event)
    return assembler.text


__all__ = [
    "AssistantOutputAssembler",
    "AssistantOutputEvent",
    "AssistantOutputEventKind",
    "assemble_assistant_output",
]
