from __future__ import annotations

import json
from pathlib import Path

from scripts.web_ui_lab_check import main

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_web_ui_lab_check_writes_latest_summary(tmp_path) -> None:
    exit_code = main(["--diagnostics-root", str(tmp_path)])

    latest_path = tmp_path / "latest.json"
    assert exit_code == 0
    assert latest_path.exists()

    summary = json.loads(latest_path.read_text())
    assert summary["kind"] == "web_ui_lab_fast_check"
    assert summary["status"] == "passed"
    assert summary["scenario_count"] >= 5
    assert summary["failure_count"] == 0
    assert summary["inspect_first"] == str(tmp_path)
    assert summary["reports"]

    first_report = summary["reports"][0]
    assert first_report["scenario_id"]
    assert first_report["route_path"].startswith("/")
    assert first_report["report_path"].endswith("report.json")
    assert first_report["fixture_payload_path"].endswith("fixture_payload.json")


def test_web_ui_lab_check_summary_reads_latest(tmp_path, capsys) -> None:
    assert main(["--diagnostics-root", str(tmp_path)]) == 0

    exit_code = main(["--diagnostics-root", str(tmp_path), "--summary"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Web UI lab fast check: PASSED" in captured.out
    assert "Diagnostics:" in captured.out


def test_web_ui_lab_check_is_exposed_through_make_and_check_script() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    check_script = (REPO_ROOT / "scripts" / "check.sh").read_text()

    assert "web-ui-fast:" in makefile
    assert "$(PYTHON) scripts/web_ui_lab_check.py" in makefile
    assert '"$PYTHON_BIN" scripts/web_ui_lab_check.py' in check_script
