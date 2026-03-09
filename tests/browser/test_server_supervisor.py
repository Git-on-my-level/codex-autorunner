from __future__ import annotations

import os
import shlex
import socket
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

import codex_autorunner.browser.server as server_mod
from codex_autorunner.browser.server import (
    BrowserServeConfig,
    BrowserServerSupervisor,
    ProcessExitedEarlyError,
    ReadinessTimeoutError,
    supervised_server,
)

pytestmark = pytest.mark.slow


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_process_gone(pid: int, *, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.05)
    pytest.fail(f"process {pid} still running")


def _write_fixture_server_script(tmp_path: Path) -> Path:
    script_path = tmp_path / "fixture_server.py"
    script_path.write_text(
        textwrap.dedent(
            """
            import argparse
            import http.server
            import os
            from pathlib import Path

            parser = argparse.ArgumentParser()
            parser.add_argument("--port", type=int, required=True)
            parser.add_argument("--marker", default="")
            parser.add_argument("--ready-line", default="")
            args = parser.parse_args()

            if args.marker:
                Path(args.marker).write_text(
                    (
                        f"{os.getcwd()}\\n"
                        f"{os.environ.get('CAR_TEST_ENV', '')}\\n"
                        f"{os.environ.get('PATH', '')}\\n"
                    ),
                    encoding="utf-8",
                )

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):  # noqa: N802
                    body = b"ok"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_args, **_kwargs):
                    return

            server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
            ready_line = args.ready_line or f"READY http://127.0.0.1:{args.port}/health"
            print(ready_line, flush=True)
            server.serve_forever()
            """
        ),
        encoding="utf-8",
    )
    return script_path


def _expected_project_bin_entries(project_root: Path) -> list[str]:
    venv_bin = "Scripts" if os.name == "nt" else "bin"
    return [
        str(project_root / "node_modules" / ".bin"),
        str(project_root / ".venv" / venv_bin),
        str(project_root / ".codex-autorunner" / "bin"),
        str(project_root / "bin"),
    ]


def test_supervised_server_ready_url_happy_path_applies_cwd_and_env(
    tmp_path: Path,
) -> None:
    script_path = _write_fixture_server_script(tmp_path)
    marker_path = tmp_path / "marker.txt"
    cwd_dir = tmp_path / "workdir"
    cwd_dir.mkdir()
    port = _free_port()
    ready_url = f"http://127.0.0.1:{port}/health"
    cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script_path)),
            "--port",
            str(port),
            "--marker",
            shlex.quote(str(marker_path)),
        ]
    )

    session_pid = None
    with supervised_server(
        BrowserServeConfig(
            serve_cmd=cmd,
            ready_url=ready_url,
            cwd=cwd_dir,
            env_overrides={"CAR_TEST_ENV": "applied"},
            timeout_seconds=10.0,
        )
    ) as session:
        session_pid = session.pid
        assert session.ready_source == "ready_url"
        assert session.target_url == f"http://127.0.0.1:{port}"
        assert httpx.get(ready_url, timeout=1.0).status_code == 200

    assert session_pid is not None
    _wait_process_gone(session_pid)
    marker_text = marker_path.read_text(encoding="utf-8").splitlines()
    assert marker_text[0] == str(cwd_dir)
    assert marker_text[1] == "applied"


def test_supervised_server_project_context_applies_path_and_default_cwd(
    tmp_path: Path,
) -> None:
    script_path = _write_fixture_server_script(tmp_path)
    marker_path = tmp_path / "marker-project-context.txt"
    project_root = tmp_path / "project-root"
    for rel_path in (
        Path("node_modules/.bin"),
        Path(".venv") / ("Scripts" if os.name == "nt" else "bin"),
        Path(".codex-autorunner/bin"),
        Path("bin"),
    ):
        (project_root / rel_path).mkdir(parents=True, exist_ok=True)
    port = _free_port()
    ready_url = f"http://127.0.0.1:{port}/health"
    cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script_path)),
            "--port",
            str(port),
            "--marker",
            shlex.quote(str(marker_path)),
        ]
    )

    session_pid = None
    with supervised_server(
        BrowserServeConfig(
            serve_cmd=cmd,
            ready_url=ready_url,
            env_overrides={"CAR_TEST_ENV": "project-context"},
            project_root=project_root,
            project_context_enabled=True,
            timeout_seconds=10.0,
        )
    ) as session:
        session_pid = session.pid
        assert session.ready_source == "ready_url"
        assert session.target_url == f"http://127.0.0.1:{port}"
        assert httpx.get(ready_url, timeout=1.0).status_code == 200

    assert session_pid is not None
    _wait_process_gone(session_pid)
    marker_text = marker_path.read_text(encoding="utf-8").splitlines()
    expected_root = str(project_root.resolve())
    assert marker_text[0] == expected_root
    assert marker_text[1] == "project-context"
    path_entries = [entry for entry in marker_text[2].split(os.pathsep) if entry]
    expected_prefixes = _expected_project_bin_entries(project_root.resolve())
    assert path_entries[: len(expected_prefixes)] == expected_prefixes


def test_supervised_server_no_project_context_keeps_default_cwd_and_path(
    tmp_path: Path,
) -> None:
    script_path = _write_fixture_server_script(tmp_path)
    marker_path = tmp_path / "marker-no-project-context.txt"
    project_root = tmp_path / "project-root-disabled"
    port = _free_port()
    ready_url = f"http://127.0.0.1:{port}/health"
    cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script_path)),
            "--port",
            str(port),
            "--marker",
            shlex.quote(str(marker_path)),
        ]
    )

    session_pid = None
    with supervised_server(
        BrowserServeConfig(
            serve_cmd=cmd,
            ready_url=ready_url,
            env_overrides={"CAR_TEST_ENV": "no-project-context"},
            project_root=project_root,
            project_context_enabled=False,
            timeout_seconds=10.0,
        )
    ) as session:
        session_pid = session.pid
        assert session.ready_source == "ready_url"
        assert session.target_url == f"http://127.0.0.1:{port}"
        assert httpx.get(ready_url, timeout=1.0).status_code == 200

    assert session_pid is not None
    _wait_process_gone(session_pid)
    marker_text = marker_path.read_text(encoding="utf-8").splitlines()
    assert marker_text[0] != str(project_root.resolve())
    assert marker_text[1] == "no-project-context"
    path_entries = set(marker_text[2].split(os.pathsep))
    for expected_prefix in _expected_project_bin_entries(project_root):
        assert expected_prefix not in path_entries


def test_supervised_server_explicit_env_override_beats_project_context_path(
    tmp_path: Path,
) -> None:
    script_path = _write_fixture_server_script(tmp_path)
    marker_path = tmp_path / "marker-env-precedence.txt"
    project_root = tmp_path / "project-root-override"
    project_root.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    ready_url = f"http://127.0.0.1:{port}/health"
    cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script_path)),
            "--port",
            str(port),
            "--marker",
            shlex.quote(str(marker_path)),
        ]
    )

    session_pid = None
    with supervised_server(
        BrowserServeConfig(
            serve_cmd=cmd,
            ready_url=ready_url,
            env_overrides={
                "CAR_TEST_ENV": "path-override",
                "PATH": "EXPLICIT_PATH_VALUE",
            },
            project_root=project_root,
            project_context_enabled=True,
            timeout_seconds=10.0,
        )
    ) as session:
        session_pid = session.pid
        assert session.ready_source == "ready_url"
        assert session.target_url == f"http://127.0.0.1:{port}"
        assert httpx.get(ready_url, timeout=1.0).status_code == 200

    assert session_pid is not None
    _wait_process_gone(session_pid)
    marker_text = marker_path.read_text(encoding="utf-8").splitlines()
    assert marker_text[1] == "path-override"
    assert marker_text[2] == "EXPLICIT_PATH_VALUE"


def test_supervisor_readiness_timeout_still_cleans_up(tmp_path: Path) -> None:
    script = tmp_path / "sleep_forever.py"
    script.write_text("import time\nprint('booting', flush=True)\ntime.sleep(300)\n")
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    supervisor = BrowserServerSupervisor(
        BrowserServeConfig(
            serve_cmd=cmd,
            ready_url=f"http://127.0.0.1:{_free_port()}/health",
            timeout_seconds=0.4,
            poll_interval_seconds=0.05,
        )
    )
    supervisor.start()
    pid = supervisor.process_pid
    try:
        with pytest.raises(ReadinessTimeoutError):
            supervisor.wait_until_ready()
    finally:
        supervisor.stop()
    assert pid is not None
    _wait_process_gone(pid)


def test_supervisor_honors_small_ready_timeout_when_probe_is_slow(
    tmp_path: Path,
) -> None:
    script = tmp_path / "sleep_forever.py"
    script.write_text("import time\nprint('booting', flush=True)\ntime.sleep(300)\n")
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    supervisor = BrowserServerSupervisor(
        BrowserServeConfig(
            serve_cmd=cmd,
            ready_url=f"http://127.0.0.1:{_free_port()}/health",
            timeout_seconds=0.2,
            poll_interval_seconds=0.05,
        )
    )

    def _slow_probe(*_args, **kwargs):
        timeout = float(kwargs.get("timeout", kwargs.get("timeout_seconds", 0.0)))
        time.sleep(timeout)
        raise httpx.ConnectTimeout("simulated timeout")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(server_mod.httpx, "get", _slow_probe)

    supervisor.start()
    started = time.monotonic()
    elapsed_wait = None
    try:
        with pytest.raises(ReadinessTimeoutError):
            supervisor.wait_until_ready()
        elapsed_wait = time.monotonic() - started
    finally:
        supervisor.stop()
        monkeypatch.undo()
    assert elapsed_wait is not None
    assert (
        elapsed_wait < 0.6
    ), f"timeout should be near configured deadline, got {elapsed_wait:.3f}s"


def test_supervisor_reports_early_process_exit(tmp_path: Path) -> None:
    script = tmp_path / "exit_early.py"
    script.write_text("import sys\nprint('bye', flush=True)\nsys.exit(7)\n")
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    supervisor = BrowserServerSupervisor(
        BrowserServeConfig(
            serve_cmd=cmd,
            ready_log_pattern="READY",
            timeout_seconds=2.0,
            poll_interval_seconds=0.05,
        )
    )
    supervisor.start()
    pid = supervisor.process_pid
    try:
        with pytest.raises(ProcessExitedEarlyError):
            supervisor.wait_until_ready()
    finally:
        supervisor.stop()
    assert pid is not None
    _wait_process_gone(pid)


def test_supervised_server_log_pattern_ready_with_url_group(tmp_path: Path) -> None:
    script_path = _write_fixture_server_script(tmp_path)
    port = _free_port()
    ready_line = f"SERVICE url=http://127.0.0.1:{port}/health"
    cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script_path)),
            "--port",
            str(port),
            "--ready-line",
            shlex.quote(ready_line),
        ]
    )
    session_pid = None
    with supervised_server(
        BrowserServeConfig(
            serve_cmd=cmd,
            ready_log_pattern=r"url=(?P<url>http://127\.0\.0\.1:\d+/health)",
            timeout_seconds=10.0,
        )
    ) as session:
        session_pid = session.pid
        assert session.ready_source == "ready_log_pattern"
        assert session.target_url == f"http://127.0.0.1:{port}"
    assert session_pid is not None
    _wait_process_gone(session_pid)


def test_supervised_server_interrupt_path_still_cleans_up(tmp_path: Path) -> None:
    script_path = _write_fixture_server_script(tmp_path)
    port = _free_port()
    cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script_path)),
            "--port",
            str(port),
        ]
    )
    pid_holder = {"pid": None}
    with pytest.raises(KeyboardInterrupt):
        with supervised_server(
            BrowserServeConfig(
                serve_cmd=cmd,
                ready_url=f"http://127.0.0.1:{port}/health",
                timeout_seconds=10.0,
            )
        ) as session:
            pid_holder["pid"] = session.pid
            raise KeyboardInterrupt()
    assert pid_holder["pid"] is not None
    _wait_process_gone(int(pid_holder["pid"]))
