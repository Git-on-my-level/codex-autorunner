from __future__ import annotations

import asyncio
import concurrent.futures
import os
import signal
import sys
import threading
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
    NEEDS_ATTENTION_STATUSES,
    PROCESS_KIND,
    PreviewServiceKind,
    PreviewServiceStatus,
    PreviewServiceSupervisor,
    PreviewServiceSupervisorError,
)
from codex_autorunner.core.preview_services import supervisor as supervisor_module


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
        assert record.status == PreviewServiceStatus.HEALTHY.value
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


def test_start_failure_after_process_launch_cleans_up_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _find_available_port()
    supervisor = PreviewServiceSupervisor(tmp_path, port_range=(port, port))
    launched = []
    real_popen = supervisor_module.subprocess.Popen

    def tracking_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        launched.append(process)
        return process

    def fail_process_record_write(*args, **kwargs):
        raise OSError("simulated process record write failure")

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", tracking_popen)
    monkeypatch.setattr(
        supervisor_module,
        "write_process_record",
        fail_process_record_write,
    )

    with pytest.raises(
        PreviewServiceSupervisorError,
        match="simulated process record write failure",
    ):
        supervisor.start_managed_command(
            name="Bookkeeping failure server",
            argv=_server_command(),
            cwd=tmp_path,
            port_policy={"mode": "auto"},
            health_check={"type": "tcp", "path": None},
        )

    assert launched
    assert _wait_for_inactive(launched[0].pid)
    records = supervisor.registry.list()
    assert len(records) == 1
    failed = records[0]
    assert failed.status == PreviewServiceStatus.FAILED.value
    assert failed.process is None
    assert failed.target is None
    assert failed.port_policy is not None
    assert failed.port_policy.allocated_port is None
    assert read_process_record(tmp_path, PROCESS_KIND, failed.service_id) is None


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
    assert restarted.status == PreviewServiceStatus.HEALTHY.value
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


def test_managed_service_logs_are_bounded_while_process_runs(tmp_path: Path) -> None:
    supervisor = PreviewServiceSupervisor(tmp_path, log_max_bytes=256)
    script = (
        "import time\n"
        "for index in range(200):\n"
        "    print('line', index, 'x' * 80, flush=True)\n"
        "time.sleep(30)\n"
    )
    record = supervisor.start_managed_command(
        name="Noisy service",
        argv=[sys.executable, "-u", "-c", script],
        cwd=tmp_path,
        health_check={"type": "none"},
    )
    pid = record.process.pid if record.process else None
    assert pid is not None
    try:
        log_path = (
            tmp_path
            / ".codex-autorunner"
            / "services"
            / "logs"
            / f"{record.service_id}.log"
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if log_path.exists() and log_path.stat().st_size > 0:
                break
            time.sleep(0.05)
        assert log_path.exists()
        assert log_path.stat().st_size <= 256
    finally:
        supervisor.stop(record.service_id)


def test_managed_service_env_policy_excludes_secrets_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("SESSION_SECRET", "session-secret")
    supervisor = PreviewServiceSupervisor(tmp_path)
    script = (
        "import os, time\n"
        "for key in ['OPENAI_API_KEY','GITHUB_TOKEN','ANTHROPIC_API_KEY','DATABASE_URL','SESSION_SECRET']:\n"
        "    print(key + '=' + str(key in os.environ), flush=True)\n"
        "print('CUSTOM_ENV=' + os.environ.get('CUSTOM_ENV', ''), flush=True)\n"
        "time.sleep(30)\n"
    )

    record = supervisor.start_managed_command(
        name="Minimal env service",
        argv=[sys.executable, "-u", "-c", script],
        cwd=tmp_path,
        env={"CUSTOM_ENV": "allowed"},
        health_check={"type": "none"},
    )
    try:
        log_text = _wait_for_log(supervisor, record.service_id, "CUSTOM_ENV=allowed")
        assert "OPENAI_API_KEY=False" in log_text
        assert "GITHUB_TOKEN=False" in log_text
        assert "ANTHROPIC_API_KEY=False" in log_text
        assert "DATABASE_URL=False" in log_text
        assert "SESSION_SECRET=False" in log_text
        assert "CUSTOM_ENV=allowed" in log_text
    finally:
        supervisor.stop(record.service_id)


def test_managed_service_env_policy_inherit_all_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    supervisor = PreviewServiceSupervisor(tmp_path)
    script = (
        "import os, time\n"
        "print('OPENAI_API_KEY=' + os.environ.get('OPENAI_API_KEY', ''), flush=True)\n"
        "time.sleep(30)\n"
    )

    record = supervisor.start_managed_command(
        name="Inherited env service",
        argv=[sys.executable, "-u", "-c", script],
        cwd=tmp_path,
        env_policy="inherit_all",
        health_check={"type": "none"},
    )
    try:
        log_text = _wait_for_log(
            supervisor,
            record.service_id,
            "OPENAI_API_KEY=openai-secret",
        )
        assert "OPENAI_API_KEY=openai-secret" in log_text
    finally:
        supervisor.stop(record.service_id)


def test_close_all_stops_running_managed_services_without_autostarting_stopped(
    tmp_path: Path,
) -> None:
    port = _find_available_port()
    supervisor = PreviewServiceSupervisor(tmp_path, port_range=(port, port))
    stopped = supervisor.register_managed_command(
        name="Stopped server",
        argv=_server_command(),
        cwd=tmp_path,
        port_policy={"mode": "auto"},
        auto_start_on_hub_start=True,
    )
    running = supervisor.start(stopped.service_id)
    pid = running.process.pid if running.process else None
    assert pid is not None
    assert _wait_for_process(pid)

    asyncio.run(supervisor.close_all())

    closed = supervisor.registry.require(running.service_id)
    assert closed.status == PreviewServiceStatus.STOPPED.value
    assert closed.process is None
    assert closed.restart_policy.auto_start_on_hub_start is True
    assert read_process_record(tmp_path, PROCESS_KIND, running.service_id) is None
    assert _wait_for_inactive(pid)

    fresh_manager = PreviewServiceSupervisor(tmp_path, port_range=(port, port))
    after_restart = fresh_manager.registry.require(stopped.service_id)
    assert after_restart.status == PreviewServiceStatus.STOPPED.value
    assert after_restart.process is None


def test_reconciler_records_managed_service_exit_code_and_event(
    tmp_path: Path,
) -> None:
    supervisor = PreviewServiceSupervisor(tmp_path)
    record = supervisor.start_managed_command(
        name="Short lived service",
        argv=[
            sys.executable,
            "-u",
            "-c",
            "import time; print('ready', flush=True); time.sleep(0.1); raise SystemExit(7)",
        ],
        cwd=tmp_path,
        health_check={"type": "none"},
    )
    pid = record.process.pid if record.process else None
    assert pid is not None

    assert _wait_for_inactive(pid)
    reconciled = supervisor.reconcile_service(record.service_id)

    assert reconciled.status == PreviewServiceStatus.EXITED.value
    assert reconciled.process is not None
    assert reconciled.process.exit_code == 7
    assert reconciled.process.exited_at is not None
    assert reconciled.process.last_exit_reason == "process exited"
    assert read_process_record(tmp_path, PROCESS_KIND, record.service_id) is None
    assert _event_types(supervisor, record.service_id) == [
        "created",
        "starting",
        "started",
        "healthy",
        "exited",
    ]


def test_stale_pid_identity_mismatch_is_orphaned_before_stop_or_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = PreviewServiceSupervisor(tmp_path)
    record = supervisor.start_managed_command(
        name="Identity checked server",
        argv=[
            sys.executable,
            "-u",
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
        ],
        cwd=tmp_path,
        health_check={"type": "none"},
    )
    pid = record.process.pid if record.process else None
    assert pid is not None

    def fail_terminate(*args, **kwargs):
        raise AssertionError("mismatched process must not be terminated")

    monkeypatch.setattr(
        supervisor_module, "_process_identity_status", lambda *_: "mismatch"
    )
    monkeypatch.setattr(supervisor_module, "terminate_record", fail_terminate)

    with pytest.raises(PreviewServiceSupervisorError, match="stale PID"):
        supervisor.stop(record.service_id)
    orphaned = supervisor.registry.require(record.service_id)
    assert orphaned.status == PreviewServiceStatus.ORPHANED.value
    assert process_is_active(pid)

    with pytest.raises(PreviewServiceSupervisorError, match="stale PID"):
        supervisor.kill(
            record.service_id,
            force=True,
            force_attestation={
                "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
                "user_request": "test force kill preview service",
                "target_scope": f"hub.preview_services.kill:{record.service_id}",
            },
        )
    assert process_is_active(pid)
    os.kill(pid, signal.SIGTERM)
    assert _wait_for_inactive(pid)


def test_live_pgid_mismatch_marks_orphaned_without_kill(tmp_path: Path) -> None:
    supervisor = PreviewServiceSupervisor(tmp_path)
    record = supervisor.start_managed_command(
        name="PGID checked server",
        argv=[
            sys.executable,
            "-u",
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
        ],
        cwd=tmp_path,
        health_check={"type": "none"},
    )
    pid = record.process.pid if record.process else None
    pgid = record.process.pgid if record.process else None
    assert pid is not None
    assert pgid is not None
    supervisor.registry.update(
        record.service_id,
        lambda latest: latest.model_copy(
            update={
                "process": latest.process.model_copy(update={"pgid": pgid + 1}),
                "updated_at": latest.updated_at,
            }
        ),
    )

    with pytest.raises(PreviewServiceSupervisorError, match="stale PID"):
        supervisor.stop(record.service_id)

    orphaned = supervisor.registry.require(record.service_id)
    assert orphaned.status == PreviewServiceStatus.ORPHANED.value
    assert process_is_active(pid)
    os.kill(pid, signal.SIGTERM)
    assert _wait_for_inactive(pid)


def test_concurrent_start_calls_are_serialized_by_service_lock(tmp_path: Path) -> None:
    supervisor = PreviewServiceSupervisor(tmp_path)
    record = supervisor.register_managed_command(
        name="Concurrent start service",
        argv=[
            sys.executable,
            "-u",
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
        ],
        cwd=tmp_path,
        health_check={"type": "none"},
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(supervisor.start, record.service_id),
            executor.submit(supervisor.start, record.service_id),
        ]
        results = [future.result(timeout=10) for future in futures]

    pids = {item.process.pid for item in results if item.process is not None}
    assert len(pids) == 1
    process_records = [
        read_process_record(tmp_path, PROCESS_KIND, record.service_id),
    ]
    assert process_records[0] is not None
    supervisor.stop(record.service_id)


def test_restart_and_kill_wait_for_service_lock_before_mutating(
    tmp_path: Path,
) -> None:
    supervisor = PreviewServiceSupervisor(tmp_path)
    record = supervisor.start_managed_command(
        name="Locked restart service",
        argv=[
            sys.executable,
            "-u",
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
        ],
        cwd=tmp_path,
        health_check={"type": "none"},
    )
    restart_entered = threading.Event()
    kill_entered = threading.Event()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def restart_under_lock():
        restart_entered.set()
        return supervisor.restart(record.service_id)

    def kill_under_lock():
        kill_entered.set()
        return supervisor.kill(
            record.service_id,
            force=True,
            force_attestation={
                "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
                "user_request": "test force kill preview service",
                "target_scope": f"hub.preview_services.kill:{record.service_id}",
            },
        )

    try:
        with supervisor.service_lock(record.service_id):
            restart_future = executor.submit(restart_under_lock)
            kill_future = executor.submit(kill_under_lock)
            assert restart_entered.wait(timeout=2)
            assert kill_entered.wait(timeout=2)
            time.sleep(0.2)
            assert not restart_future.done()
            assert not kill_future.done()

        results = [
            restart_future.result(timeout=10),
            kill_future.result(timeout=10),
        ]
        assert {result.service_id for result in results} == {record.service_id}
    finally:
        executor.shutdown(wait=True)
        latest = supervisor.registry.require(record.service_id)
        if latest.process is not None:
            supervisor.stop(record.service_id)


def test_slow_startup_health_uses_realistic_default_timeout(tmp_path: Path) -> None:
    port = _find_available_port()
    supervisor = PreviewServiceSupervisor(tmp_path, port_range=(port, port))
    script = (
        "import http.server, os, time\n"
        "time.sleep(0.5)\n"
        "port=int(os.environ['PORT'])\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'ok')\n"
        "    def log_message(self, *args):\n"
        "        return\n"
        "http.server.ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )

    record = supervisor.start_managed_command(
        name="Slow server",
        argv=[sys.executable, "-u", "-c", script],
        cwd=tmp_path,
        port_policy={"mode": "auto"},
        health_check={"type": "http", "path": "/", "expected_status": [200]},
    )
    try:
        assert record.status == PreviewServiceStatus.HEALTHY.value
    finally:
        supervisor.stop(record.service_id)


def test_needs_attention_statuses_are_centralized() -> None:
    assert {
        PreviewServiceStatus.FAILED.value,
        PreviewServiceStatus.UNHEALTHY.value,
        PreviewServiceStatus.CONFLICT.value,
        PreviewServiceStatus.ORPHANED.value,
        PreviewServiceStatus.EXITED.value,
    } == set(NEEDS_ATTENTION_STATUSES)


def test_event_history_records_lifecycle_and_health_failures(tmp_path: Path) -> None:
    supervisor = PreviewServiceSupervisor(tmp_path, event_history_limit=4)
    loopback = supervisor.register_loopback_url(
        "http://127.0.0.1:9/",
        name="Unhealthy loopback",
    )
    supervisor.check_health(loopback.service_id)

    service = supervisor.start_managed_command(
        name="Eventful service",
        argv=[
            sys.executable,
            "-u",
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
        ],
        cwd=tmp_path,
        health_check={"type": "none"},
    )
    supervisor.stop(service.service_id)
    restarted = supervisor.start(service.service_id)
    supervisor.kill(
        restarted.service_id,
        force=True,
        force_attestation={
            "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
            "user_request": "test force kill preview service",
            "target_scope": f"hub.preview_services.kill:{restarted.service_id}",
        },
    )

    assert "health_failed" in _event_types(supervisor, loopback.service_id)
    service_events = _event_types(supervisor, service.service_id)
    assert service_events == ["starting", "started", "healthy", "killed"]


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


def _wait_for_process(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_is_active(pid):
            return True
        time.sleep(0.05)
    return process_is_active(pid)


def _wait_for_inactive(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_is_active(pid):
            return True
        time.sleep(0.05)
    return not process_is_active(pid)


def _event_types(
    supervisor: PreviewServiceSupervisor,
    service_id: str,
) -> list[str]:
    return [str(event.get("type")) for event in supervisor.events(service_id)]
