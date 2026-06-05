from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

from codex_autorunner.core.force_attestation import (
    FORCE_ATTESTATION_REQUIRED_PHRASE,
)
from codex_autorunner.core.locks import process_is_active
from codex_autorunner.core.managed_processes.registry import read_process_record
from codex_autorunner.core.preview_services import (
    PROCESS_KIND,
    PreviewServiceKind,
    PreviewServiceStatus,
    PreviewServiceSupervisor,
    PreviewServiceSupervisorError,
)


def test_register_static_and_loopback_services_do_not_start_processes(
    tmp_path: Path,
) -> None:
    html = tmp_path / "index.html"
    html.write_text("<h1>Preview</h1>", encoding="utf-8")
    supervisor = PreviewServiceSupervisor(tmp_path)

    static = supervisor.register_static(html, name="Static")
    loopback = supervisor.register_loopback_url(
        "http://127.0.0.1:39001/",
        name="Loopback",
    )

    assert static.kind == PreviewServiceKind.STATIC_FILE.value
    assert static.status == PreviewServiceStatus.REGISTERED.value
    assert static.process is None
    assert static.command is None
    assert loopback.kind == PreviewServiceKind.LOOPBACK_URL.value
    assert loopback.process is None


def test_managed_service_start_creates_process_record_log_and_default_restart_policy(
    tmp_path: Path,
) -> None:
    port = _find_available_port()
    supervisor = PreviewServiceSupervisor(tmp_path, port_range=(port, port))

    record = supervisor.start_managed_command(
        name="Test server",
        argv=_server_command(),
        cwd=tmp_path,
        port_policy={"mode": "auto"},
        health_check={"type": "http", "path": "/health", "expected_status": [200]},
    )
    try:
        assert record.status == PreviewServiceStatus.RUNNING.value
        assert record.process is not None
        assert record.process.pid is not None
        assert process_is_active(record.process.pid)
        assert record.target is not None
        assert record.target.port == port
        assert record.restart_policy.auto_start_on_hub_start is False

        process_record = read_process_record(tmp_path, PROCESS_KIND, record.service_id)
        assert process_record is not None
        assert process_record.kind == PROCESS_KIND
        assert process_record.handle_id == record.service_id

        assert _wait_for_health(supervisor, record.service_id)
        with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            assert response.status == 200
            assert response.read() == b"ok"

        log_text = _wait_for_log(supervisor, record.service_id, "preview service ready")
        assert "preview service ready" in log_text
    finally:
        supervisor.stop(record.service_id)


def test_stop_restart_and_kill_lifecycle_clean_up_process_records(
    tmp_path: Path,
) -> None:
    first_port = _find_available_port()
    second_port = _find_available_port(start=first_port + 1)
    supervisor = PreviewServiceSupervisor(
        tmp_path,
        port_range=(first_port, second_port),
    )
    record = supervisor.start_managed_command(
        name="Restartable server",
        argv=_server_command(),
        cwd=tmp_path,
        port_policy={"mode": "auto"},
        health_check={"type": "tcp", "path": None},
    )
    first_pid = record.process.pid if record.process else None
    assert first_pid is not None

    stopped = supervisor.stop(record.service_id)
    assert stopped.status == PreviewServiceStatus.STOPPED.value
    assert stopped.process is None
    assert read_process_record(tmp_path, PROCESS_KIND, record.service_id) is None
    assert _wait_for_inactive(first_pid)

    restarted = supervisor.restart(record.service_id)
    restarted_pid = restarted.process.pid if restarted.process else None
    assert restarted.status == PreviewServiceStatus.RUNNING.value
    assert restarted_pid is not None
    assert restarted_pid != first_pid

    with pytest.raises(PreviewServiceSupervisorError, match="requires force"):
        supervisor.kill(record.service_id)

    killed = supervisor.kill(
        record.service_id,
        force=True,
        force_attestation={
            "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
            "user_request": "test force kill preview service",
            "target_scope": f"hub.preview_services.kill:{record.service_id}",
        },
    )
    assert killed.status == PreviewServiceStatus.STOPPED.value
    assert killed.process is None
    assert read_process_record(tmp_path, PROCESS_KIND, record.service_id) is None
    assert _wait_for_inactive(restarted_pid)


def _server_command() -> list[str]:
    script = (
        "import http.server, os\n"
        "port=int(os.environ['PORT'])\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'ok')\n"
        "    def log_message(self, *args):\n"
        "        return\n"
        "print('preview service ready', port, flush=True)\n"
        "http.server.ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )
    return [sys.executable, "-u", "-c", script]


def _find_available_port(start: int = 41000) -> int:
    import socket

    for port in range(start, 55000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no available test port")


def _wait_for_health(
    supervisor: PreviewServiceSupervisor,
    service_id: str,
    *,
    timeout: float = 5.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if supervisor.check_health(service_id).ok:
            return True
        time.sleep(0.05)
    return False


def _wait_for_log(
    supervisor: PreviewServiceSupervisor,
    service_id: str,
    marker: str,
    *,
    timeout: float = 5.0,
) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = supervisor.logs(service_id, tail=20)
        if marker in text:
            return text
        time.sleep(0.05)
    return text


def _wait_for_inactive(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_is_active(pid):
            return True
        time.sleep(0.05)
    return not process_is_active(pid)
