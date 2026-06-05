from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from ..locks import file_lock
from .models import (
    PortPolicy,
    PortPolicyMode,
    PreviewServiceRecord,
    PreviewServiceStatus,
    ServiceTarget,
    service_record_from_dict,
    utc_now_iso,
)
from .registry import PreviewServiceRegistry

DEFAULT_PORT_RANGE_START = 39000
DEFAULT_PORT_RANGE_END = 39999
DEFAULT_PREVIEW_HOST = "127.0.0.1"
DEFAULT_PREVIEW_SCHEME: Literal["http", "https"] = "http"

_STATE_DIR = ".codex-autorunner"
_SERVICES_DIR = "services"
_ALLOCATOR_LOCK = "allocator.lock"

_RESERVED_STATUSES = {
    PreviewServiceStatus.REGISTERED.value,
    PreviewServiceStatus.STARTING.value,
    PreviewServiceStatus.RUNNING.value,
    PreviewServiceStatus.HEALTHY.value,
    PreviewServiceStatus.UNHEALTHY.value,
}


class PreviewPortAllocationError(ValueError):
    pass


@dataclass(frozen=True)
class PreviewPortAllocationResult:
    port: int
    host: str
    scheme: Literal["http", "https"]
    direct_url: str
    policy_mode: str
    requested_port: int | None = None
    fallback_used: bool = False
    service_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "host": self.host,
            "scheme": self.scheme,
            "direct_url": self.direct_url,
            "policy_mode": self.policy_mode,
            "requested_port": self.requested_port,
            "fallback_used": self.fallback_used,
            "service_id": self.service_id,
        }


class PreviewPortAllocator:
    def __init__(
        self,
        hub_root: Path,
        *,
        port_range: tuple[int, int] = (
            DEFAULT_PORT_RANGE_START,
            DEFAULT_PORT_RANGE_END,
        ),
        host: str = DEFAULT_PREVIEW_HOST,
        scheme: Literal["http", "https"] = DEFAULT_PREVIEW_SCHEME,
        durable: bool = False,
    ) -> None:
        self._hub_root = hub_root.resolve()
        self._registry = PreviewServiceRegistry(self._hub_root, durable=durable)
        self._port_range = _validate_port_range(port_range)
        self._host = host
        self._scheme = _validate_scheme(scheme)

    @property
    def lock_path(self) -> Path:
        return self._hub_root / _STATE_DIR / _SERVICES_DIR / _ALLOCATOR_LOCK

    def allocate(
        self,
        policy: PortPolicy | dict[str, Any],
        *,
        exclude_service_id: str | None = None,
    ) -> PreviewPortAllocationResult:
        parsed_policy = _coerce_port_policy(policy)
        with file_lock(self.lock_path):
            records = self._registry.list()
            used_ports = _reserved_ports(records, exclude_service_id=exclude_service_id)
            return self._allocate_locked(
                parsed_policy,
                used_ports=used_ports,
                service_id=exclude_service_id,
            )

    def reserve(
        self,
        service_id: str,
        policy: PortPolicy | dict[str, Any] | None = None,
    ) -> PreviewPortAllocationResult:
        with file_lock(self.lock_path):
            record = self._registry.require(service_id)
            parsed_policy = _coerce_port_policy(policy or record.port_policy)
            used_ports = _reserved_ports(
                self._registry.list(),
                exclude_service_id=service_id,
            )
            result = self._allocate_locked(
                parsed_policy,
                used_ports=used_ports,
                service_id=service_id,
            )
            self._registry.update(
                service_id,
                lambda _current: _record_with_allocation(
                    record,
                    parsed_policy,
                    result,
                ),
            )
            return result

    def _allocate_locked(
        self,
        policy: PortPolicy,
        *,
        used_ports: dict[int, str],
        service_id: str | None,
    ) -> PreviewPortAllocationResult:
        for candidate in _candidate_ports(policy, self._port_range):
            reserver = used_ports.get(candidate)
            if reserver is not None:
                if policy.mode == PortPolicyMode.EXACT:
                    raise PreviewPortAllocationError(
                        f"Exact preview port {candidate} is already reserved by "
                        f"service {reserver}"
                    )
                continue
            if not _can_bind_loopback(self._host, candidate):
                if policy.mode == PortPolicyMode.EXACT:
                    raise PreviewPortAllocationError(
                        f"Exact preview port {candidate} is already bound on "
                        f"{self._host}"
                    )
                continue
            requested_port = policy.port
            return PreviewPortAllocationResult(
                port=candidate,
                host=self._host,
                scheme=self._scheme,
                direct_url=f"{self._scheme}://{self._host}:{candidate}/",
                policy_mode=str(policy.mode),
                requested_port=requested_port,
                fallback_used=requested_port is not None
                and candidate != requested_port,
                service_id=service_id,
            )
        start, end = self._port_range
        raise PreviewPortAllocationError(
            f"No available preview ports in configured range {start}-{end}"
        )


def allocate_preview_port(
    hub_root: Path,
    policy: PortPolicy | dict[str, Any],
    *,
    exclude_service_id: str | None = None,
) -> PreviewPortAllocationResult:
    return PreviewPortAllocator(hub_root).allocate(
        policy,
        exclude_service_id=exclude_service_id,
    )


def reserve_preview_port(
    hub_root: Path,
    service_id: str,
    policy: PortPolicy | dict[str, Any] | None = None,
) -> PreviewPortAllocationResult:
    return PreviewPortAllocator(hub_root).reserve(service_id, policy)


def _coerce_port_policy(policy: PortPolicy | dict[str, Any] | None) -> PortPolicy:
    if policy is None:
        raise PreviewPortAllocationError("Port allocation requires a port_policy")
    if isinstance(policy, PortPolicy):
        return policy
    if isinstance(policy, dict):
        try:
            return PortPolicy.model_validate(policy)
        except ValueError as exc:
            raise PreviewPortAllocationError(str(exc)) from exc
    raise PreviewPortAllocationError("Port allocation policy must be a dict")


def _validate_port_range(port_range: tuple[int, int]) -> tuple[int, int]:
    start, end = port_range
    if start < 1 or end > 65535 or start > end:
        raise PreviewPortAllocationError(
            "Preview service port range must be within 1-65535 and start <= end"
        )
    return start, end


def _validate_scheme(scheme: str) -> Literal["http", "https"]:
    if scheme not in {"http", "https"}:
        raise PreviewPortAllocationError("Preview service scheme must be http or https")
    return cast(Literal["http", "https"], scheme)


def _candidate_ports(policy: PortPolicy, port_range: tuple[int, int]) -> list[int]:
    start, end = port_range
    if policy.mode == PortPolicyMode.EXACT:
        assert policy.port is not None
        return [policy.port]
    if policy.mode == PortPolicyMode.PREFERRED:
        assert policy.port is not None
        return [
            policy.port,
            *[port for port in range(start, end + 1) if port != policy.port],
        ]
    if policy.mode == PortPolicyMode.AUTO:
        return list(range(start, end + 1))
    raise PreviewPortAllocationError(f"Unsupported port policy mode: {policy.mode}")


def _reserved_ports(
    records: list[PreviewServiceRecord],
    *,
    exclude_service_id: str | None,
) -> dict[int, str]:
    used: dict[int, str] = {}
    for record in records:
        if record.service_id == exclude_service_id:
            continue
        if record.status not in _RESERVED_STATUSES:
            continue
        for port in _record_ports(record):
            used.setdefault(port, record.service_id)
    return used


def _record_ports(record: PreviewServiceRecord) -> set[int]:
    ports: set[int] = set()
    if record.port_policy is not None and record.port_policy.allocated_port is not None:
        ports.add(record.port_policy.allocated_port)
    if record.target is not None and record.target.port is not None:
        ports.add(record.target.port)
    return ports


def _record_with_allocation(
    record: PreviewServiceRecord,
    policy: PortPolicy,
    result: PreviewPortAllocationResult,
) -> PreviewServiceRecord:
    payload = record.to_dict()
    payload["updated_at"] = utc_now_iso()
    port_policy = policy.model_dump(mode="json", exclude_none=True)
    port_policy["allocated_port"] = result.port
    payload["port_policy"] = port_policy
    target = ServiceTarget(
        host=result.host,
        port=result.port,
        scheme=result.scheme,
        direct_url=result.direct_url,
    )
    payload["target"] = target.model_dump(mode="json", exclude_none=True)
    return service_record_from_dict(payload)


def _can_bind_loopback(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError:
        return False
    return True
