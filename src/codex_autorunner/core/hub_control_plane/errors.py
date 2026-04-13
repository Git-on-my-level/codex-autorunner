from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Optional, cast

HubControlPlaneErrorCode = Literal[
    "hub_unavailable",
    "hub_incompatible",
    "hub_rejected",
    "transport_failure",
    "protocol_failure",
]

_VALID_ERROR_CODES: tuple[HubControlPlaneErrorCode, ...] = (
    "hub_unavailable",
    "hub_incompatible",
    "hub_rejected",
    "transport_failure",
    "protocol_failure",
)


def _copy_details(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return dict(value)


def _parse_error_code(value: Any) -> HubControlPlaneErrorCode:
    raw_code = str(value or "").strip()
    if raw_code not in _VALID_ERROR_CODES:
        raise ValueError("Unknown hub control-plane error code")
    return cast(HubControlPlaneErrorCode, raw_code)


def default_retryable(code: HubControlPlaneErrorCode) -> bool:
    return code in {"hub_unavailable", "transport_failure"}


@dataclass(frozen=True)
class HubControlPlaneErrorInfo:
    """Serializable error payload for the shared-state control plane."""

    code: HubControlPlaneErrorCode
    message: str
    retryable: bool
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "HubControlPlaneErrorInfo":
        raw_code = _parse_error_code(data.get("code"))
        raw_retryable = data.get("retryable")
        retryable = (
            bool(raw_retryable)
            if isinstance(raw_retryable, bool)
            else default_retryable(raw_code)
        )
        message = str(data.get("message") or "").strip()
        if not message:
            raise ValueError("Hub control-plane errors require a message")
        return cls(
            code=raw_code,
            message=message,
            retryable=retryable,
            details=_copy_details(data.get("details")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HubControlPlaneError(RuntimeError):
    """Typed control-plane failure surfaced to side-process clients."""

    def __init__(
        self,
        code: HubControlPlaneErrorCode,
        message: str,
        *,
        retryable: Optional[bool] = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code: HubControlPlaneErrorCode = code
        self.retryable = (
            default_retryable(code) if retryable is None else bool(retryable)
        )
        self.details = _copy_details(details)

    @property
    def info(self) -> HubControlPlaneErrorInfo:
        return HubControlPlaneErrorInfo(
            code=self.code,
            message=str(self),
            retryable=self.retryable,
            details=self.details,
        )

    @classmethod
    def from_info(cls, info: HubControlPlaneErrorInfo) -> "HubControlPlaneError":
        return cls(
            info.code,
            info.message,
            retryable=info.retryable,
            details=info.details,
        )


__all__ = [
    "HubControlPlaneError",
    "HubControlPlaneErrorCode",
    "HubControlPlaneErrorInfo",
    "default_retryable",
]
