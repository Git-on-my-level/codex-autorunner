from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Sequence

from ...core.orchestration.turn_output_reducer import (
    prior_assistant_text_candidates,
    trim_cumulative_assistant_text,
)

ACPIngressInputKind = Literal[
    "current_turn_delta",
    "current_turn_snapshot",
    "final_message",
]
ACPIngressClassification = Literal[
    "current_turn_delta",
    "current_turn_snapshot",
    "final_message",
    "transcript_projection",
    "invalid_stale_output",
]

_ASSISTANT_MARKER_RE = re.compile(
    r"(?ims)(?:^|\n)\s*(?:assistant|agent)\s*:\s*(.*?)(?=(?:\n\s*(?:user|human)\s*:)|\Z)"
)


@dataclass(frozen=True)
class ACPIngressNormalizedOutput:
    text: str
    classification: ACPIngressClassification
    input_kind: ACPIngressInputKind
    trimmed: bool = False
    matched_prior_chars: int = 0


def _latest_assistant_section(text: str) -> tuple[str, bool]:
    current = str(text or "").strip()
    if not current:
        return "", False
    matches = [
        match.group(1).strip() for match in _ASSISTANT_MARKER_RE.finditer(current)
    ]
    matches = [match for match in matches if match]
    if matches:
        return matches[-1], True
    marker = "\n\nAssistant:\n"
    if marker in current:
        return current.rsplit(marker, 1)[-1].strip(), True
    return current, False


def normalize_acp_ingress_output(
    text: str,
    *,
    input_kind: ACPIngressInputKind,
    prior_assistant_texts: Sequence[str] = (),
) -> ACPIngressNormalizedOutput:
    """Classify and normalize assistant text before ACP prompt state consumes it."""

    raw = str(text or "")
    if input_kind == "current_turn_delta":
        return ACPIngressNormalizedOutput(
            text=raw,
            classification="current_turn_delta",
            input_kind=input_kind,
        )

    candidate, transcript_projection = _latest_assistant_section(raw)
    if not candidate.strip():
        return ACPIngressNormalizedOutput(
            text="",
            classification="invalid_stale_output",
            input_kind=input_kind,
        )

    for prior in prior_assistant_text_candidates(prior_assistant_texts):
        normalized, scope = trim_cumulative_assistant_text(candidate, prior)
        if scope == "current_turn_final":
            continue
        if scope == "stale_prior_output" and not transcript_projection:
            continue
        return ACPIngressNormalizedOutput(
            text=normalized,
            classification=(
                "invalid_stale_output"
                if scope == "stale_prior_output"
                else "transcript_projection"
            ),
            input_kind=input_kind,
            trimmed=normalized != candidate,
            matched_prior_chars=len(prior),
        )

    if transcript_projection:
        return ACPIngressNormalizedOutput(
            text=candidate,
            classification="transcript_projection",
            input_kind=input_kind,
            trimmed=candidate != raw.strip(),
        )
    return ACPIngressNormalizedOutput(
        text=candidate,
        classification=input_kind,
        input_kind=input_kind,
    )


__all__ = [
    "ACPIngressClassification",
    "ACPIngressInputKind",
    "ACPIngressNormalizedOutput",
    "normalize_acp_ingress_output",
]
