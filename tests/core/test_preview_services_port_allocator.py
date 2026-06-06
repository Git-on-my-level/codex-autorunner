from __future__ import annotations

import socket
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from codex_autorunner.core.preview_services import (
    PreviewPortAllocationError,
    PreviewPortAllocator,
    PreviewServiceRegistry,
)


def managed_service_payload(service_id: str) -> dict:
    return {
        "service_id": service_id,
        "name": f"Managed {service_id}",
        "kind": "managed_command",
        "status": "registered",
        "created_at": "2026-06-05T00:00:00Z",
        "updated_at": "2026-06-05T00:00:00Z",
        "target": {
            "host": "127.0.0.1",
            "port": 1,
            "scheme": "http",
            "direct_url": "http://127.0.0.1:1/",
        },
        "exposure": {"car_url": f"/preview/services/{service_id}/"},
        "port_policy": {"mode": "auto"},
        "command": {
            "argv": ["python", "-m", "http.server", "$PORT"],
            "cwd": "/tmp",
            "env": {"PORT": "$PORT"},
        },
    }


def test_exact_policy_succeeds_when_port_is_available(tmp_path: Path) -> None:
    port = _find_available_ports(1)[0]
    allocator = PreviewPortAllocator(tmp_path, port_range=(port, port))

    result = allocator.allocate({"mode": "exact", "port": port})

    assert result.port == port
    assert result.direct_url == f"http://127.0.0.1:{port}/"
    assert result.fallback_used is False


def test_exact_policy_rejects_car_reserved_port(tmp_path: Path) -> None:
    port = _find_available_ports(1)[0]
    registry = PreviewServiceRegistry(tmp_path)
    payload = managed_service_payload("svc_reserved001")
    payload["target"]["port"] = port
    payload["target"]["direct_url"] = f"http://127.0.0.1:{port}/"
    payload["port_policy"] = {"mode": "auto", "allocated_port": port}
    registry.create(payload)
    allocator = PreviewPortAllocator(tmp_path, port_range=(port, port))

    with pytest.raises(PreviewPortAllocationError, match="reserved by service"):
        allocator.allocate({"mode": "exact", "port": port})


def test_preferred_policy_falls_back_when_requested_port_is_reserved(
    tmp_path: Path,
) -> None:
    preferred, fallback = _find_available_ports(2)
    registry = PreviewServiceRegistry(tmp_path)
    payload = managed_service_payload("svc_preferred001")
    payload["target"]["port"] = preferred
    payload["target"]["direct_url"] = f"http://127.0.0.1:{preferred}/"
    payload["port_policy"] = {"mode": "auto", "allocated_port": preferred}
    registry.create(payload)
    allocator = PreviewPortAllocator(tmp_path, port_range=(fallback, fallback))

    result = allocator.allocate({"mode": "preferred", "port": preferred})

    assert result.port == fallback
    assert result.requested_port == preferred
    assert result.fallback_used is True


def test_auto_policy_chooses_available_port_in_range(tmp_path: Path) -> None:
    port = _find_available_ports(1)[0]
    allocator = PreviewPortAllocator(tmp_path, port_range=(port, port))

    result = allocator.allocate({"mode": "auto"})

    assert result.port == port


def test_host_bound_ports_are_skipped_for_auto_policy(tmp_path: Path) -> None:
    held_port, fallback = _find_available_ports(2)
    with _bound_socket(held_port):
        allocator = PreviewPortAllocator(tmp_path, port_range=(held_port, fallback))

        result = allocator.allocate({"mode": "auto"})

    assert result.port == fallback


def test_exact_policy_rejects_host_bound_port(tmp_path: Path) -> None:
    port = _find_available_ports(1)[0]
    with _bound_socket(port):
        allocator = PreviewPortAllocator(tmp_path, port_range=(port, port))

        with pytest.raises(PreviewPortAllocationError, match="already bound"):
            allocator.allocate({"mode": "exact", "port": port})


def test_reserve_updates_service_record_under_allocator_lock(tmp_path: Path) -> None:
    port = _find_available_ports(1)[0]
    registry = PreviewServiceRegistry(tmp_path)
    registry.create(managed_service_payload("svc_reserve001"))
    allocator = PreviewPortAllocator(tmp_path, port_range=(port, port))

    result = allocator.reserve("svc_reserve001", {"mode": "auto"})

    loaded = registry.require("svc_reserve001")
    assert result.port == port
    assert loaded.port_policy is not None
    assert loaded.port_policy.allocated_port == port
    assert loaded.target is not None
    assert loaded.target.port == port
    assert loaded.target.direct_url == f"http://127.0.0.1:{port}/"


def test_concurrent_reservations_do_not_select_the_same_port(tmp_path: Path) -> None:
    ports = _find_available_ports(4)
    registry = PreviewServiceRegistry(tmp_path)
    service_ids = [
        "svc_concurrent001",
        "svc_concurrent002",
        "svc_concurrent003",
        "svc_concurrent004",
    ]
    for service_id in service_ids:
        registry.create(managed_service_payload(service_id))

    with ProcessPoolExecutor(max_workers=4) as executor:
        reserved_ports = list(
            executor.map(
                _reserve_service_port,
                [str(tmp_path)] * len(service_ids),
                service_ids,
                [min(ports)] * len(service_ids),
                [max(ports)] * len(service_ids),
            )
        )

    assert sorted(reserved_ports) == ports


def _reserve_service_port(
    hub_root: str,
    service_id: str,
    start_port: int,
    end_port: int,
) -> int:
    allocator = PreviewPortAllocator(
        Path(hub_root),
        port_range=(start_port, end_port),
    )
    return allocator.reserve(service_id, {"mode": "auto"}).port


def _find_available_ports(count: int) -> list[int]:
    for port in range(41000, 55000):
        ports = list(range(port, port + count))
        if all(_port_can_bind(candidate) for candidate in ports):
            return ports
    raise RuntimeError("could not find available test ports")


def _port_can_bind(port: int) -> bool:
    try:
        with _bound_socket(port):
            return True
    except OSError:
        return False


class _bound_socket:
    def __init__(self, port: int) -> None:
        self._port = port
        self._socket: socket.socket | None = None

    def __enter__(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", self._port))
            sock.listen(1)
        except OSError:
            sock.close()
            raise
        self._socket = sock
        return sock

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._socket is not None:
            self._socket.close()
