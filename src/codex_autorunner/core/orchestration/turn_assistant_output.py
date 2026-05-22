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

_OWNERSHIPS = frozenset(
    {
        "current_turn",
        "current_turn_stream",
        "trimmed_from_cumulative",
        "rejected_stale_prior",
        "empty",
    }
)
_SOURCES = frozenset(
    {
        "adapter_final",
        "adapter_stream",
        "runtime_final",
        "runtime_stream",
        "reducer",
        "none",
    }
)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _sanitize_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(provenance or {})
    matched_prior = str(sanitized.pop("matched_prior_text", "") or "")
    if matched_prior:
        sanitized["matched_prior_chars"] = len(matched_prior)
    safe = _json_safe(sanitized)
    return safe if isinstance(safe, dict) else {}


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

    def to_durable_dict(self) -> dict[str, Any]:
        """Serialize turn output without carrying full prior-output diagnostics."""

        return {
            "managed_thread_id": self.managed_thread_id,
            "managed_turn_id": self.managed_turn_id,
            "backend_thread_id": self.backend_thread_id,
            "backend_turn_id": self.backend_turn_id,
            "text": self.text,
            "ownership": self.ownership,
            "source": self.source,
            "provenance": _sanitize_provenance(self.provenance),
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "TurnAssistantOutput":
        ownership = str(data.get("ownership") or "empty")
        source = str(data.get("source") or "none")
        if ownership not in _OWNERSHIPS:
            ownership = "empty"
        if source not in _SOURCES:
            source = "none"
        provenance = data.get("provenance")
        return cls(
            managed_thread_id=str(data.get("managed_thread_id") or ""),
            managed_turn_id=str(data.get("managed_turn_id") or ""),
            backend_thread_id=(
                str(data.get("backend_thread_id") or "").strip() or None
            ),
            backend_turn_id=str(data.get("backend_turn_id") or "").strip() or None,
            text=str(data.get("text") or ""),
            ownership=ownership,  # type: ignore[arg-type]
            source=source,  # type: ignore[arg-type]
            provenance=dict(provenance) if isinstance(provenance, dict) else {},
        )


__all__ = [
    "TurnAssistantOutput",
    "TurnAssistantOutputOwnership",
    "TurnAssistantOutputSource",
]
