from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import textwrap
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_autorunner.browser import BrowserServeSession, ReadinessTimeoutError
from codex_autorunner.browser.runtime import BrowserRunResult
from codex_autorunner.cli import app
from codex_autorunner.core import optional_dependencies


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
        try:
            status = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                check=False,
                capture_output=True,
                text=True,
            )
            fields = status.stdout.strip().split()
            if fields and fields[0].startswith("Z"):
                return
        except (OSError, subprocess.SubprocessError):
            return
        time.sleep(0.05)
    pytest.fail(f"process {pid} still running")


def _write_server_script(tmp_path: Path) -> Path:
    script = tmp_path / "cli_server_fixture.py"
    script.write_text(
        textwrap.dedent(
            """
            import argparse
            import http.server
            import os
            from pathlib import Path

            parser = argparse.ArgumentParser()
            parser.add_argument("--port", type=int, required=True)
            parser.add_argument("--pid-file", required=True)
            args = parser.parse_args()

            Path(args.pid_file).write_text(str(os.getpid()), encoding="utf-8")

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
            print(f"READY http://127.0.0.1:{args.port}/health", flush=True)
            server.serve_forever()
            """
        ),
        encoding="utf-8",
    )
    return script


def _patch_playwright_present(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    original_find_spec = optional_dependencies.importlib.util.find_spec

    def fake_find_spec(name: str):  # type: ignore[no-untyped-def]
        if name == "playwright":
            return object()
        return original_find_spec(name)

    monkeypatch.setattr(
        optional_dependencies.importlib.util, "find_spec", fake_find_spec
    )


def test_render_screenshot_serve_mode_starts_and_cleans_up(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    capture_path = tmp_path / "capture.png"
    capture_path.write_bytes(b"png")

    def fake_capture_screenshot(self, **_kwargs):  # type: ignore[no-untyped-def]
        return BrowserRunResult(
            ok=True,
            mode="screenshot",
            target_url="http://127.0.0.1:1234",
            artifacts={"capture": capture_path},
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_screenshot",
        fake_capture_screenshot,
    )

    port = _free_port()
    pid_file = tmp_path / "pid.txt"
    script = _write_server_script(tmp_path)
    cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            "--port",
            str(port),
            "--pid-file",
            shlex.quote(str(pid_file)),
        ]
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "screenshot",
            "--serve-cmd",
            cmd,
            "--ready-url",
            f"http://127.0.0.1:{port}/health",
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code == 0
    assert str(capture_path) in result.output
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    _wait_process_gone(pid)


def test_render_demo_serve_mode_uses_shared_cleanup(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    summary_path = tmp_path / "demo-summary.json"
    screenshot_path = tmp_path / "demo-step.png"
    summary_path.write_text("{}", encoding="utf-8")
    screenshot_path.write_bytes(b"png")

    def fake_capture_demo(self, **_kwargs):  # type: ignore[no-untyped-def]
        return BrowserRunResult(
            ok=True,
            mode="demo",
            target_url="http://127.0.0.1:1234",
            artifacts={"summary": summary_path, "step_1.screenshot": screenshot_path},
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_demo",
        fake_capture_demo,
    )

    port = _free_port()
    pid_file = tmp_path / "pid.txt"
    script = _write_server_script(tmp_path)
    demo_script = tmp_path / "demo.yaml"
    demo_script.write_text("version: 1\nsteps: []\n", encoding="utf-8")
    cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            "--port",
            str(port),
            "--pid-file",
            shlex.quote(str(pid_file)),
        ]
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "demo",
            "--script",
            str(demo_script),
            "--serve-cmd",
            cmd,
            "--ready-url",
            f"http://127.0.0.1:{port}/health",
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code == 0
    assert str(screenshot_path) in result.output
    assert str(summary_path) not in result.output
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    _wait_process_gone(pid)


def test_render_demo_full_artifacts_keeps_structured_outputs(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    summary_path = tmp_path / "demo-summary.json"
    screenshot_path = tmp_path / "demo-step.png"
    summary_path.write_text("{}", encoding="utf-8")
    screenshot_path.write_bytes(b"png")

    def fake_capture_demo(self, **_kwargs):  # type: ignore[no-untyped-def]
        return BrowserRunResult(
            ok=True,
            mode="demo",
            target_url="http://127.0.0.1:1234",
            artifacts={"summary": summary_path, "step_1.screenshot": screenshot_path},
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_demo",
        fake_capture_demo,
    )

    port = _free_port()
    pid_file = tmp_path / "pid.txt"
    script = _write_server_script(tmp_path)
    demo_script = tmp_path / "demo.yaml"
    demo_script.write_text("version: 1\nsteps: []\n", encoding="utf-8")
    cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            "--port",
            str(port),
            "--pid-file",
            shlex.quote(str(pid_file)),
        ]
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "demo",
            "--script",
            str(demo_script),
            "--serve-cmd",
            cmd,
            "--ready-url",
            f"http://127.0.0.1:{port}/health",
            "--full-artifacts",
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code == 0
    assert str(screenshot_path) in result.output
    assert str(summary_path) in result.output
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    _wait_process_gone(pid)


def test_render_observe_serve_mode_uses_shared_cleanup(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    snapshot_path = tmp_path / "observe-a11y.json"
    metadata_path = tmp_path / "observe-meta.json"
    run_manifest_path = tmp_path / "observe-run-manifest.json"
    snapshot_path.write_text("{}", encoding="utf-8")
    metadata_path.write_text("{}", encoding="utf-8")
    run_manifest_path.write_text("{}", encoding="utf-8")

    def fake_capture_observe(self, **_kwargs):  # type: ignore[no-untyped-def]
        return BrowserRunResult(
            ok=True,
            mode="observe",
            target_url="http://127.0.0.1:1234",
            artifacts={
                "snapshot": snapshot_path,
                "metadata": metadata_path,
                "run_manifest": run_manifest_path,
            },
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_observe",
        fake_capture_observe,
    )

    port = _free_port()
    pid_file = tmp_path / "pid.txt"
    script = _write_server_script(tmp_path)
    cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            "--port",
            str(port),
            "--pid-file",
            shlex.quote(str(pid_file)),
        ]
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "observe",
            "--serve-cmd",
            cmd,
            "--ready-url",
            f"http://127.0.0.1:{port}/health",
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code == 0
    assert str(snapshot_path) in result.output
    assert str(metadata_path) in result.output
    assert str(run_manifest_path) in result.output
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    _wait_process_gone(pid)


def test_render_screenshot_serve_mode_wires_project_context_flags(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    capture_path = tmp_path / "capture-project-context.png"
    capture_path.write_bytes(b"png")
    captured = {}

    def fake_capture_screenshot(self, **_kwargs):  # type: ignore[no-untyped-def]
        return BrowserRunResult(
            ok=True,
            mode="screenshot",
            target_url="http://127.0.0.1:1234",
            artifacts={"capture": capture_path},
        )

    @contextmanager
    def fake_supervised_server(config):  # type: ignore[no-untyped-def]
        captured["config"] = config
        yield BrowserServeSession(
            pid=123,
            pgid=None,
            ready_source="ready_url",
            target_url="http://127.0.0.1:1234",
            ready_url="http://127.0.0.1:1234/health",
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_screenshot",
        fake_capture_screenshot,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.supervised_server",
        fake_supervised_server,
    )

    requested_project_root = tmp_path / "project-context-root"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "screenshot",
            "--serve-cmd",
            "python -c \"print('ok')\"",
            "--ready-url",
            "http://127.0.0.1:4321/health",
            "--project-root",
            str(requested_project_root),
            "--env",
            "CAR_TEST_ENV=cli",
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code == 0
    assert str(capture_path) in result.output
    config = captured["config"]
    assert config.project_context_enabled is True
    assert config.project_root == requested_project_root.resolve()
    assert config.env_overrides["CAR_TEST_ENV"] == "cli"


def test_render_screenshot_serve_mode_no_project_context_wires_disable_flag(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    capture_path = tmp_path / "capture-no-project-context.png"
    capture_path.write_bytes(b"png")
    captured = {}

    def fake_capture_screenshot(self, **_kwargs):  # type: ignore[no-untyped-def]
        return BrowserRunResult(
            ok=True,
            mode="screenshot",
            target_url="http://127.0.0.1:1234",
            artifacts={"capture": capture_path},
        )

    @contextmanager
    def fake_supervised_server(config):  # type: ignore[no-untyped-def]
        captured["config"] = config
        yield BrowserServeSession(
            pid=123,
            pgid=None,
            ready_source="ready_url",
            target_url="http://127.0.0.1:1234",
            ready_url="http://127.0.0.1:1234/health",
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_screenshot",
        fake_capture_screenshot,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.supervised_server",
        fake_supervised_server,
    )

    requested_project_root = tmp_path / "project-context-root-disabled"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "screenshot",
            "--serve-cmd",
            "python -c \"print('ok')\"",
            "--ready-url",
            "http://127.0.0.1:4321/health",
            "--project-root",
            str(requested_project_root),
            "--no-project-context",
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code == 0
    assert str(capture_path) in result.output
    config = captured["config"]
    assert config.project_context_enabled is False
    assert config.project_root is None


def test_render_screenshot_serve_mode_failure_includes_project_context_details(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    @contextmanager
    def failing_supervised_server(_config):  # type: ignore[no-untyped-def]
        raise ReadinessTimeoutError("simulated readiness timeout")
        yield

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.supervised_server",
        failing_supervised_server,
    )

    requested_project_root = tmp_path / "project-context-root-failure"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "screenshot",
            "--serve-cmd",
            "python -c \"print('ok')\"",
            "--ready-url",
            "http://127.0.0.1:4321/health",
            "--project-root",
            str(requested_project_root),
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code != 0
    assert "project_context=enabled" in result.output
    resolved_root = str(requested_project_root.resolve())
    assert f"project_root={resolved_root}" in result.output
    assert f"cwd={resolved_root}" in result.output
