import json
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app
from codex_autorunner.core.hub_diagnostics import hub_endpoint_path, hub_pid_path

runner = CliRunner()


def _write_endpoint(hub_root: Path, *, pid: int = 4321) -> None:
    runtime_dir = hub_pid_path(hub_root).parent
    runtime_dir.mkdir(parents=True, exist_ok=True)
    hub_pid_path(hub_root).write_text(f"{pid}\n", encoding="utf-8")
    hub_endpoint_path(hub_root).write_text(
        json.dumps(
            {
                "url": "http://127.0.0.1:4517/car",
                "host": "127.0.0.1",
                "port": 4517,
                "base_path": "/car",
            }
        ),
        encoding="utf-8",
    )


def test_hub_endpoint_prints_running_url(tmp_path: Path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)
    _write_endpoint(hub_root)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    result = runner.invoke(app, ["hub", "endpoint", "--path", str(hub_root)])

    assert result.exit_code == 0
    assert result.output.strip() == "http://127.0.0.1:4517/car"


def test_hub_endpoint_reports_missing_or_stale_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)
    _write_endpoint(hub_root)

    def _raise_stale(_pid: int, _sig: int) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr("os.kill", _raise_stale)

    result = runner.invoke(app, ["hub", "endpoint", "--path", str(hub_root)])

    assert result.exit_code == 1
    assert "Hub endpoint unavailable" in result.output
