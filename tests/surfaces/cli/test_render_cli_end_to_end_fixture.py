from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_autorunner.browser.actions import load_demo_manifest
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


def _patch_playwright_present(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    original_find_spec = optional_dependencies.importlib.util.find_spec

    def fake_find_spec(name: str):  # type: ignore[no-untyped-def]
        if name == "playwright":
            return object()
        return original_find_spec(name)

    monkeypatch.setattr(
        optional_dependencies.importlib.util, "find_spec", fake_find_spec
    )


def test_render_commands_end_to_end_with_fixture_app(
    monkeypatch, tmp_path: Path, repo: Path
) -> None:
    _patch_playwright_present(monkeypatch)

    fixtures_root = Path(__file__).resolve().parents[2] / "fixtures"
    fixture_app = fixtures_root / "browser_fixture_app.py"
    demo_manifest = fixtures_root / "browser_demo_manifest.yaml"

    def fake_capture_screenshot(self, **kwargs):  # type: ignore[no-untyped-def]
        base_url = kwargs["base_url"]
        path = kwargs.get("path", "/")
        with urllib.request.urlopen(f"{base_url}{path}", timeout=2) as response:
            html = response.read().decode("utf-8")
        assert "Fixture Landing" in html

        out_dir = Path(kwargs["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact = out_dir / "fixture-landing.png"
        artifact.write_bytes(b"png")
        return BrowserRunResult(
            ok=True,
            mode="screenshot",
            target_url=f"{base_url}{path}",
            artifacts={"capture": artifact},
        )

    def fake_capture_observe(self, **kwargs):  # type: ignore[no-untyped-def]
        base_url = kwargs["base_url"]
        path = kwargs.get("path", "/")
        with urllib.request.urlopen(f"{base_url}{path}", timeout=2) as response:
            html = response.read().decode("utf-8")
        assert "Sign In" in html

        out_dir = Path(kwargs["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        snapshot = out_dir / "fixture-observe-a11y.json"
        metadata = out_dir / "fixture-observe-meta.json"
        locator_refs = out_dir / "fixture-observe-locators.json"
        run_manifest = out_dir / "fixture-observe-run-manifest.json"
        snapshot.write_text('{"role":"WebArea"}\n', encoding="utf-8")
        metadata.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "captured_url": f"{base_url}{path}",
                    "title": "Fixture Form",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        locator_refs.write_text('{"schema_version":1,"refs":[]}\n', encoding="utf-8")
        run_manifest.write_text(
            '{"schema_version":1,"mode":"observe"}\n', encoding="utf-8"
        )
        return BrowserRunResult(
            ok=True,
            mode="observe",
            target_url=f"{base_url}{path}",
            artifacts={
                "snapshot": snapshot,
                "metadata": metadata,
                "locator_refs": locator_refs,
                "run_manifest": run_manifest,
            },
        )

    def fake_capture_demo(self, **kwargs):  # type: ignore[no-untyped-def]
        script_path = Path(kwargs["script_path"])
        manifest = load_demo_manifest(script_path)
        assert manifest.version == 1

        base_url = kwargs["base_url"]
        with urllib.request.urlopen(f"{base_url}/form", timeout=2) as response:
            html = response.read().decode("utf-8")
        assert "Sign In" in html

        out_dir = Path(kwargs["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        step_capture = out_dir / "fixture-demo-step.png"
        summary = out_dir / "fixture-demo-summary.json"
        step_capture.write_bytes(b"png")
        summary.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "manifest_version": 1,
                    "script_path": str(script_path),
                    "target_url": f"{base_url}/form",
                    "steps": [{"index": 1, "action": "goto", "ok": True}],
                    "artifacts": {
                        "step_1.screenshot": str(step_capture),
                        "summary": str(summary),
                    },
                    "skipped": {},
                    "error": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return BrowserRunResult(
            ok=True,
            mode="demo",
            target_url=f"{base_url}/form",
            artifacts={"summary": summary, "step_1.screenshot": step_capture},
        )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_screenshot",
        fake_capture_screenshot,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_observe",
        fake_capture_observe,
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.render.BrowserRuntime.capture_demo",
        fake_capture_demo,
    )

    runner = CliRunner()

    port_one = _free_port()
    pid_file_one = tmp_path / "fixture-server-screenshot.pid"
    cmd_one = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(fixture_app)),
            "--port",
            str(port_one),
            "--pid-file",
            shlex.quote(str(pid_file_one)),
        ]
    )
    screenshot_result = runner.invoke(
        app,
        [
            "render",
            "screenshot",
            "--serve-cmd",
            cmd_one,
            "--ready-url",
            f"http://127.0.0.1:{port_one}/health",
            "--path",
            "/",
            "--repo",
            str(repo),
        ],
    )
    assert screenshot_result.exit_code == 0
    assert "fixture-landing.png" in screenshot_result.output
    pid_one = int(pid_file_one.read_text(encoding="utf-8").strip())
    _wait_process_gone(pid_one)

    port_two = _free_port()
    pid_file_two = tmp_path / "fixture-server-observe.pid"
    cmd_two = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(fixture_app)),
            "--port",
            str(port_two),
            "--pid-file",
            shlex.quote(str(pid_file_two)),
        ]
    )
    observe_result = runner.invoke(
        app,
        [
            "render",
            "observe",
            "--serve-cmd",
            cmd_two,
            "--ready-url",
            f"http://127.0.0.1:{port_two}/health",
            "--path",
            "/form",
            "--repo",
            str(repo),
        ],
    )
    assert observe_result.exit_code == 0
    assert "fixture-observe-a11y.json" in observe_result.output
    assert "fixture-observe-run-manifest.json" in observe_result.output
    pid_two = int(pid_file_two.read_text(encoding="utf-8").strip())
    _wait_process_gone(pid_two)

    port_three = _free_port()
    pid_file_three = tmp_path / "fixture-server-demo.pid"
    cmd_three = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(fixture_app)),
            "--port",
            str(port_three),
            "--pid-file",
            shlex.quote(str(pid_file_three)),
        ]
    )
    demo_result = runner.invoke(
        app,
        [
            "render",
            "demo",
            "--script",
            str(demo_manifest),
            "--serve-cmd",
            cmd_three,
            "--ready-url",
            f"http://127.0.0.1:{port_three}/health",
            "--path",
            "/form",
            "--repo",
            str(repo),
        ],
    )
    assert demo_result.exit_code == 0
    assert "fixture-demo-step.png" in demo_result.output
    pid_three = int(pid_file_three.read_text(encoding="utf-8").strip())
    _wait_process_gone(pid_three)
