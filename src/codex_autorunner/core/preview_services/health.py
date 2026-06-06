from __future__ import annotations

import socket
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from typing import Any

from .models import HealthCheck, HealthCheckType, PreviewServiceRecord

DEFAULT_HEALTH_TIMEOUT_SECONDS = 2.0


class PreviewServiceHealthError(ValueError):
    pass


@dataclass(frozen=True)
class PreviewServiceHealthResult:
    ok: bool
    check_type: str
    status_code: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "type": self.check_type}
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.error is not None:
            payload["error"] = self.error
        return payload


def check_service_health(
    record: PreviewServiceRecord,
    *,
    timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
) -> PreviewServiceHealthResult:
    check = record.health_check or HealthCheck(type=HealthCheckType.HTTP)
    if check.type == HealthCheckType.NONE.value:
        return PreviewServiceHealthResult(
            ok=True, check_type=HealthCheckType.NONE.value
        )
    target = record.target
    if target is None or target.host is None or target.port is None:
        raise PreviewServiceHealthError("Preview service has no host/port target")
    if check.type == HealthCheckType.TCP.value:
        return _check_tcp(target.host, target.port, timeout_seconds=timeout_seconds)
    if check.type == HealthCheckType.HTTP.value:
        return _check_http(
            target.host,
            target.port,
            scheme=target.scheme or "http",
            path=check.path or "/",
            expected_status=set(check.expected_status),
            timeout_seconds=timeout_seconds,
        )
    raise PreviewServiceHealthError(f"Unsupported health check type: {check.type}")


def _check_tcp(
    host: str,
    port: int,
    *,
    timeout_seconds: float,
) -> PreviewServiceHealthResult:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return PreviewServiceHealthResult(ok=True, check_type="tcp")
    except OSError as exc:
        return PreviewServiceHealthResult(
            ok=False,
            check_type="tcp",
            error=str(exc),
        )


def _check_http(
    host: str,
    port: int,
    *,
    scheme: str,
    path: str,
    expected_status: set[int],
    timeout_seconds: float,
) -> PreviewServiceHealthResult:
    connection_cls = HTTPSConnection if scheme == "https" else HTTPConnection
    connection = connection_cls(host, port, timeout=timeout_seconds)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        status = int(response.status)
    except OSError as exc:
        return PreviewServiceHealthResult(
            ok=False,
            check_type="http",
            error=str(exc),
        )
    finally:
        connection.close()
    return PreviewServiceHealthResult(
        ok=status in expected_status,
        check_type="http",
        status_code=status,
    )
