from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TurnAssistantOutputOwnership = Literal[
    "current_turn",
    "current_turn_stream",
    "trimmed_from_cumulative",
    "rejected_stale_prior",
    "empty",
]

TurnAssistantOutputSource = Literal[
    "adapter_final",
    "adapter_stream",
    "runtime_final",
    "runtime_stream",
    "reducer",
    "none",
]


@dataclass(frozen=True)
class TurnAssistantOutput:
    """Turn-owned assistant output consumed after runtime finalization."""

    managed_thread_id: str
    managed_turn_id: str
    backend_thread_id: str | None
    backend_turn_id: str | None
    text: str
    ownership: TurnAssistantOutputOwnership
    source: TurnAssistantOutputSource
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def matched_prior_text(self) -> str:
        return str(self.provenance.get("matched_prior_text") or "")

    @property
    def evidence(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "turn_output_ownership": self.ownership,
            "turn_output_source": self.source,
        }
        if self.matched_prior_text:
            data["turn_output_prior_chars"] = len(self.matched_prior_text)
        if self.provenance:
            prov = dict(self.provenance)
            prov.pop("matched_prior_text", None)
            if prov:
                data["turn_output_provenance"] = prov
        return data


__all__ = [
    "TurnAssistantOutput",
    "TurnAssistantOutputOwnership",
    "TurnAssistantOutputSource",
]
