from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence

from .runtime_thread_events import RuntimeThreadRunEventState
from .runtime_threads import RuntimeThreadOutcome
from .turn_assistant_output import (
    TurnAssistantOutput,
    TurnAssistantOutputOwnership,
    TurnAssistantOutputSource,
)

TurnOutputScope = Literal[
    "current_turn_final",
    "current_turn_stream",
    "cumulative_transcript_trimmed",
    "stale_prior_output",
    "empty",
]


def _output_source(source: str) -> TurnAssistantOutputSource:
    if source == "event_stream":
        return "runtime_stream"
    if source == "event_final":
        return "runtime_final"
    if source in {"outcome", "prior_guard"}:
        return "reducer"
    return "none"


def _scope_ownership(scope: TurnOutputScope) -> TurnAssistantOutputOwnership:
    if scope == "current_turn_stream":
        return "current_turn_stream"
    if scope == "cumulative_transcript_trimmed":
        return "trimmed_from_cumulative"
    if scope == "stale_prior_output":
        return "rejected_stale_prior"
    if scope == "empty":
        return "empty"
    return "current_turn"


def _turn_output(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: str | None,
    backend_turn_id: str | None,
    text: str,
    scope: TurnOutputScope,
    source: str,
    matched_prior_text: str = "",
    candidate_source: str | None = None,
) -> TurnAssistantOutput:
    provenance: dict[str, Any] = {
        "reducer_scope": scope,
    }
    if candidate_source:
        provenance["candidate_source"] = candidate_source
    if matched_prior_text:
        provenance["matched_prior_text"] = matched_prior_text
    return TurnAssistantOutput(
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        backend_thread_id=backend_thread_id,
        backend_turn_id=backend_turn_id,
        text=text,
        ownership=_scope_ownership(scope),
        source=_output_source(source),
        provenance=provenance,
    )


def _whitespace_insensitive_prefix_end(value: str, prefix: str) -> int | None:
    value_index = 0
    prefix_index = 0
    prefix_non_ws = 0
    value_length = len(value)
    prefix_length = len(prefix)
    while prefix_index < prefix_length:
        prefix_char = prefix[prefix_index]
        if prefix_char.isspace():
            prefix_index += 1
            continue
        while value_index < value_length and value[value_index].isspace():
            value_index += 1
        if value_index >= value_length or value[value_index] != prefix_char:
            return None
        value_index += 1
        prefix_index += 1
        prefix_non_ws += 1
    if prefix_non_ws == 0:
        return None
    return value_index


def assistant_text_extends_prefix(assistant_text: str, prefix: str) -> bool:
    current = str(assistant_text or "")
    previous = str(prefix or "")
    if not current.strip() or not previous.strip():
        return False
    if current == previous:
        return True
    if current.startswith(previous):
        return True
    current_lstrip = current.lstrip()
    previous_stripped = previous.strip()
    if current_lstrip.startswith(previous_stripped):
        return True
    return _whitespace_insensitive_prefix_end(current, previous) is not None


def trim_cumulative_assistant_text(
    assistant_text: str,
    previous_assistant_text: str,
) -> tuple[str, TurnOutputScope]:
    """Normalize provider text to the active-turn segment.

    Equal-to-prior text is treated as stale, not as a valid final answer. A
    legitimate repeated answer should arrive as a current-turn delta/final event
    from an adapter with a turn cursor; ambiguous transcript-level equality is
    exactly the failure mode this boundary is meant to reject.
    """

    current = str(assistant_text or "")
    previous = str(previous_assistant_text or "")
    if not current.strip():
        return current, "empty"
    if not previous.strip():
        return current, "current_turn_final"
    if current == previous:
        return "", "stale_prior_output"
    if current.startswith(previous):
        trimmed = current[len(previous) :].lstrip()
        return (
            (trimmed, "cumulative_transcript_trimmed")
            if trimmed
            else ("", "stale_prior_output")
        )
    current_lstrip = current.lstrip()
    previous_stripped = previous.strip()
    if current_lstrip.startswith(previous_stripped):
        leading = len(current) - len(current_lstrip)
        trimmed = current[leading + len(previous_stripped) :].lstrip()
        return (
            (trimmed, "cumulative_transcript_trimmed")
            if trimmed
            else ("", "stale_prior_output")
        )
    prefix_end = _whitespace_insensitive_prefix_end(current, previous)
    if prefix_end is None:
        return current, "current_turn_final"
    trimmed = current[prefix_end:].lstrip()
    return (
        (trimmed, "cumulative_transcript_trimmed")
        if trimmed
        else ("", "stale_prior_output")
    )


def _normalize_candidate(
    text: str,
    prior_assistant_texts: Sequence[str],
) -> tuple[str, TurnOutputScope, str]:
    current = str(text or "")
    if not current.strip():
        return "", "empty", ""
    for prior in prior_assistant_texts:
        normalized, scope = trim_cumulative_assistant_text(current, prior)
        if scope != "current_turn_final":
            return normalized, scope, prior
    return current, "current_turn_final", ""


def reduce_turn_output(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: str | None,
    backend_turn_id: str | None,
    outcome: RuntimeThreadOutcome,
    event_state: RuntimeThreadRunEventState,
    prior_assistant_texts: Sequence[str],
) -> TurnAssistantOutput:
    """Return the only assistant text downstream code may persist for this turn."""

    if outcome.status != "ok":
        return _turn_output(
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
            text="",
            scope="empty",
            source="non_ok_outcome",
            candidate_source="outcome",
        )

    candidates = [
        ("outcome", outcome.assistant_text),
        ("event_final", event_state.assistant_message_text),
        ("event_stream", event_state.assistant_stream_text),
    ]
    stale_match = ""
    for source, raw_text in candidates:
        normalized, scope, matched_prior = _normalize_candidate(
            raw_text,
            prior_assistant_texts,
        )
        if scope == "empty":
            continue
        if scope == "stale_prior_output":
            stale_match = stale_match or matched_prior
            continue
        if source == "event_stream" and scope == "current_turn_final":
            scope = "current_turn_stream"
        return _turn_output(
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
            text=normalized,
            scope=scope,
            source=source,
            matched_prior_text=matched_prior,
            candidate_source=source,
        )

    return _turn_output(
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        backend_thread_id=backend_thread_id,
        backend_turn_id=backend_turn_id,
        text="",
        scope="stale_prior_output" if stale_match else "empty",
        source="prior_guard" if stale_match else "no_output",
        matched_prior_text=stale_match,
        candidate_source="prior_guard" if stale_match else None,
    )


def build_assistant_transcript_prefix(entries: Sequence[Mapping[str, Any]]) -> str:
    prefix = ""
    for entry in entries:
        entry_turn_id = str(
            entry.get("managed_turn_id") or entry.get("turn_id") or ""
        ).strip()
        if not entry_turn_id:
            continue
        assistant_text = assistant_text_from_transcript_content(
            str(entry.get("content") or "")
        )
        if not assistant_text.strip():
            continue
        if assistant_text_extends_prefix(assistant_text, prefix):
            prefix = assistant_text
        else:
            prefix += assistant_text
    return prefix


def assistant_text_from_transcript_content(content: str) -> str:
    text = str(content or "").strip()
    marker = "\n\nAssistant:\n"
    if marker in text:
        return text.rsplit(marker, 1)[-1].strip()
    return text


__all__ = [
    "TurnOutputScope",
    "assistant_text_extends_prefix",
    "assistant_text_from_transcript_content",
    "build_assistant_transcript_prefix",
    "reduce_turn_output",
    "trim_cumulative_assistant_text",
]
