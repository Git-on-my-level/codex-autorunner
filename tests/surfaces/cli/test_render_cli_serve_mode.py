from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import textwrap
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from codex_autorunner.browser import (
    BrowserPreflightResult,
    BrowserServeConfig,
    BrowserServeSession,
    DemoPreflightResult,
    DemoPreflightStepReport,
    DemoWorkflowConfigError,
    ReadinessTimeoutError,
    persist_render_session,
)
from codex_autorunner.browser.runtime import BrowserRunResult
from codex_autorunner.cli import app
from codex_autorunner.core import optional_dependencies

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


def test_render_demo_preflight_only_skips_capture(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    demo_script = tmp_path / "demo-preflight-only.yaml"
    demo_script.write_text("version: 1\nsteps: []\n", encoding="utf-8")
    preflight_report = tmp_path / "preflight-report.json"
    called = {"capture": False, "preflight": False}

    def fake_preflight_demo(self, **_kwargs):  # type: ignore[no-untyped-def]
        called["preflight"] = True
        return BrowserPreflightResult(
            ok=True,
            mode="demo_preflight",
            target_url="http://127.0.0.1:3100",
            diagnostics=DemoPreflightResult(
                ok=True,
                steps=[
                    DemoPreflightStepReport(
                        index=1,
                        action="click",
                        ok=True,
                        detail="locator is actionable: role=button name=Submit",
                    )
                ],
            ),
        )

    def fake_capture_demo(self, **_kwargs):  # type: ignore[no-untyped-def]
        called["capture"] = True
        return BrowserRunResult(
            ok=True,
            mode="demo",
            target_url="http://127.0.0.1:3100",
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.preflight_demo",
        fake_preflight_demo,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_demo",
        fake_capture_demo,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "demo",
            "--url",
            "http://127.0.0.1:3100",
            "--script",
            str(demo_script),
            "--preflight-only",
            "--preflight-report",
            str(preflight_report),
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code == 0
    assert called["preflight"] is True
    assert called["capture"] is False
    assert "Demo preflight passed." in result.output
    assert str(preflight_report) in result.output
    report = json.loads(preflight_report.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["steps"][0]["detail"].startswith("locator is actionable")


def test_render_demo_preflight_failure_blocks_capture(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    demo_script = tmp_path / "demo-preflight-fail.yaml"
    demo_script.write_text("version: 1\nsteps: []\n", encoding="utf-8")
    called = {"capture": False}

    def fake_preflight_demo(self, **_kwargs):  # type: ignore[no-untyped-def]
        return BrowserPreflightResult(
            ok=False,
            mode="demo_preflight",
            target_url="http://127.0.0.1:3200",
            diagnostics=DemoPreflightResult(
                ok=False,
                steps=[
                    DemoPreflightStepReport(
                        index=2,
                        action="click",
                        ok=False,
                        detail="Locator resolved to zero elements.",
                    )
                ],
                error_message=(
                    "Preflight failed for 1 step(s): step 2 (click): "
                    "Locator resolved to zero elements."
                ),
            ),
            error_message=(
                "Preflight failed for 1 step(s): step 2 (click): "
                "Locator resolved to zero elements."
            ),
            error_type="DemoStepError",
        )

    def fake_capture_demo(self, **_kwargs):  # type: ignore[no-untyped-def]
        called["capture"] = True
        return BrowserRunResult(ok=True, mode="demo", target_url="http://127.0.0.1")

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.preflight_demo",
        fake_preflight_demo,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_demo",
        fake_capture_demo,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "demo",
            "--url",
            "http://127.0.0.1:3200",
            "--script",
            str(demo_script),
            "--preflight",
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code != 0
    assert called["capture"] is False
    assert "Demo preflight failed." in result.output
    assert "[fail] step 2 click" in result.output
    assert "Render demo preflight failed (step_validation_failure)" in result.output


def test_render_demo_preflight_success_allows_capture(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    demo_script = tmp_path / "demo-preflight-ok.yaml"
    demo_script.write_text("version: 1\nsteps: []\n", encoding="utf-8")
    screenshot_path = tmp_path / "demo-preflight-step.png"
    summary_path = tmp_path / "demo-preflight-summary.json"
    screenshot_path.write_bytes(b"png")
    summary_path.write_text("{}", encoding="utf-8")
    called = {"capture": False}

    def fake_preflight_demo(self, **_kwargs):  # type: ignore[no-untyped-def]
        return BrowserPreflightResult(
            ok=True,
            mode="demo_preflight",
            target_url="http://127.0.0.1:3300",
            diagnostics=DemoPreflightResult(
                ok=True,
                steps=[
                    DemoPreflightStepReport(
                        index=1,
                        action="goto",
                        ok=True,
                        detail="goto reachable: http://127.0.0.1:3300/login",
                    )
                ],
            ),
        )

    def fake_capture_demo(self, **_kwargs):  # type: ignore[no-untyped-def]
        called["capture"] = True
        return BrowserRunResult(
            ok=True,
            mode="demo",
            target_url="http://127.0.0.1:3300",
            artifacts={"summary": summary_path, "step_1.screenshot": screenshot_path},
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.preflight_demo",
        fake_preflight_demo,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_demo",
        fake_capture_demo,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "demo",
            "--url",
            "http://127.0.0.1:3300",
            "--script",
            str(demo_script),
            "--preflight",
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code == 0
    assert called["capture"] is True
    assert "Demo preflight passed." in result.output
    assert str(screenshot_path) in result.output
    assert str(summary_path) not in result.output


def test_render_demo_keep_session_persists_metadata(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    demo_script = tmp_path / "demo-keep-session.yaml"
    demo_script.write_text("version: 1\nsteps: []\n", encoding="utf-8")
    screenshot_path = tmp_path / "demo-keep-session-step.png"
    summary_path = tmp_path / "demo-keep-session-summary.json"
    screenshot_path.write_bytes(b"png")
    summary_path.write_text("{}", encoding="utf-8")
    captured = {"start": False, "wait": False, "stop": False}

    class _FakeSupervisor:
        def __init__(self, config):  # type: ignore[no-untyped-def]
            self.config = config

        def start(self) -> None:
            captured["start"] = True

        def wait_until_ready(self) -> BrowserServeSession:
            captured["wait"] = True
            return BrowserServeSession(
                pid=os.getpid(),
                pgid=None,
                ready_source="ready_url",
                target_url="http://127.0.0.1:4173",
                ready_url="http://127.0.0.1:4173/health",
            )

        def stop(self) -> None:
            captured["stop"] = True

    def fake_capture_demo(self, **_kwargs):  # type: ignore[no-untyped-def]
        return BrowserRunResult(
            ok=True,
            mode="demo",
            target_url="http://127.0.0.1:4173",
            artifacts={"summary": summary_path, "step_1.screenshot": screenshot_path},
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserServerSupervisor",
        _FakeSupervisor,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_demo",
        fake_capture_demo,
    )

    session_id = "keep-demo-1"
    metadata_path = (
        repo / ".codex-autorunner" / "render_sessions" / f"{session_id}.json"
    )
    metadata_path.unlink(missing_ok=True)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "demo",
            "--serve-cmd",
            "python -c \"print('server')\"",
            "--ready-url",
            "http://127.0.0.1:4173/health",
            "--script",
            str(demo_script),
            "--session-id",
            session_id,
            "--keep-session",
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code == 0
    assert captured["start"] is True
    assert captured["wait"] is True
    assert captured["stop"] is False
    assert metadata_path.exists()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["session_id"] == session_id
    assert metadata["target_url"] == "http://127.0.0.1:4173"
    assert metadata["ready_source"] == "ready_url"
    assert "Render session kept: keep-demo-1" in result.output


def test_render_demo_attach_session_reuses_kept_target(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    demo_script = tmp_path / "demo-attach.yaml"
    demo_script.write_text("version: 1\nsteps: []\n", encoding="utf-8")
    screenshot_path = tmp_path / "demo-attach-step.png"
    summary_path = tmp_path / "demo-attach-summary.json"
    screenshot_path.write_bytes(b"png")
    summary_path.write_text("{}", encoding="utf-8")
    session_id = "attach-demo-1"
    attach_target = "http://127.0.0.1:5020"
    seen = {"base_url": None}

    persist_render_session(
        repo_root=repo,
        session_id=session_id,
        config=BrowserServeConfig(
            serve_cmd="python -m http.server 5020",
            ready_url=None,
        ),
        session=BrowserServeSession(
            pid=os.getpid(),
            pgid=None,
            ready_source="ready_url",
            target_url=attach_target,
            ready_url=None,
        ),
    )

    class _FailingSupervisor:
        def __init__(self, _config):  # type: ignore[no-untyped-def]
            raise AssertionError("Attach mode must not start a new serve session.")

    def fake_capture_demo(self, **kwargs):  # type: ignore[no-untyped-def]
        seen["base_url"] = kwargs["base_url"]
        return BrowserRunResult(
            ok=True,
            mode="demo",
            target_url=attach_target,
            artifacts={"summary": summary_path, "step_1.screenshot": screenshot_path},
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserServerSupervisor",
        _FailingSupervisor,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_demo",
        fake_capture_demo,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "demo",
            "--url",
            "http://127.0.0.1:9999",
            "--script",
            str(demo_script),
            "--session-id",
            session_id,
            "--attach-session",
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code == 0
    assert seen["base_url"] == attach_target
    assert str(screenshot_path) in result.output


def test_render_demo_attach_session_missing_error(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    demo_script = tmp_path / "demo-attach-missing.yaml"
    demo_script.write_text("version: 1\nsteps: []\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "demo",
            "--url",
            "http://127.0.0.1:9001",
            "--script",
            str(demo_script),
            "--session-id",
            "missing-session-id",
            "--attach-session",
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code != 0
    assert "session_missing" in result.output
    assert "was not found" in result.output


def test_render_demo_attach_session_stale_error(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    demo_script = tmp_path / "demo-attach-stale.yaml"
    demo_script.write_text("version: 1\nsteps: []\n", encoding="utf-8")
    session_id = "stale-session-id"
    metadata_path = (
        repo / ".codex-autorunner" / "render_sessions" / f"{session_id}.json"
    )

    persist_render_session(
        repo_root=repo,
        session_id=session_id,
        config=BrowserServeConfig(
            serve_cmd="python -m http.server 9011",
            ready_url="http://127.0.0.1:9/health",
        ),
        session=BrowserServeSession(
            pid=os.getpid(),
            pgid=None,
            ready_source="ready_url",
            target_url="http://127.0.0.1:9011",
            ready_url="http://127.0.0.1:9/health",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "demo",
            "--url",
            "http://127.0.0.1:9011",
            "--script",
            str(demo_script),
            "--session-id",
            session_id,
            "--attach-session",
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code != 0
    assert "session_stale" in result.output
    assert "unreachable" in result.output
    assert metadata_path.exists() is False


def test_render_demo_workflow_happy_path_prints_export_and_publish_artifacts(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    workflow_path = tmp_path / "demo-workflow.yaml"
    workflow_path.write_text("services: []\ndemo: {}\n", encoding="utf-8")
    published_one = tmp_path / "outbox" / "demo-video.webm"
    published_two = tmp_path / "outbox" / "demo-workflow-export-manifest.json"
    run_result = SimpleNamespace(
        workflow_path=workflow_path.resolve(),
        target_base_url="http://127.0.0.1:4200",
        primary_artifact_key="video",
        primary_artifact_path=tmp_path / "render" / "demo-video.webm",
        export_manifest_path=tmp_path / "render" / "demo-workflow-export-manifest.json",
        published_artifacts=(published_one, published_two),
    )

    seen: dict[str, object] = {}

    def fake_load_demo_workflow_config(*, workflow_path, **kwargs):  # type: ignore[no-untyped-def]
        seen["workflow_path"] = workflow_path
        seen.update(kwargs)
        return object()

    def fake_run_demo_workflow(_workflow_config):  # type: ignore[no-untyped-def]
        return run_result

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.load_demo_workflow_config",
        fake_load_demo_workflow_config,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.run_demo_workflow",
        fake_run_demo_workflow,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "demo-workflow",
            "--workflow",
            str(workflow_path),
            "--publish-outbox",
            "--outbox-dir",
            str(tmp_path / "override-outbox"),
            "--out-dir",
            str(tmp_path / "override-render"),
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code == 0
    assert f"Workflow config: {workflow_path.resolve()}" in result.output
    assert "Target base URL: http://127.0.0.1:4200" in result.output
    assert "Primary artifact: video ->" in result.output
    assert "Export manifest: " in result.output
    assert "Published artifacts:" in result.output
    assert str(published_one) in result.output
    assert str(published_two) in result.output
    assert seen["publish_outbox_override"] is True
    assert seen["outbox_path_override"] == (tmp_path / "override-outbox")
    assert seen["out_dir_override"] == (tmp_path / "override-render")


def test_render_demo_workflow_failure_surfaces_workflow_category(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    workflow_path = tmp_path / "demo-workflow-failure.yaml"
    workflow_path.write_text("services: []\ndemo: {}\n", encoding="utf-8")

    def fake_load_demo_workflow_config(**_kwargs):  # type: ignore[no-untyped-def]
        raise DemoWorkflowConfigError("config parse blew up")

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.load_demo_workflow_config",
        fake_load_demo_workflow_config,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "render",
            "demo-workflow",
            "--workflow",
            str(workflow_path),
            "--repo",
            str(repo),
        ],
    )

    assert result.exit_code != 0
    assert "Render demo-workflow failed" in result.output
    assert "workflow_config" in result.output
