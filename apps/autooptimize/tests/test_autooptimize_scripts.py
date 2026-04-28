from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = APP_ROOT / "scripts"


def _app_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["CAR_APP_STATE_DIR"] = str(tmp_path / "state")
    env["CAR_APP_ARTIFACT_DIR"] = str(tmp_path / "artifacts")
    return env


def _run_script(
    script_name: str,
    *args: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script_name), *args],
        cwd=APP_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_autooptimize_end_to_end_scripts(tmp_path: Path) -> None:
    env = _app_env(tmp_path)
    run_path = Path(env["CAR_APP_STATE_DIR"]) / "run.json"
    iterations_path = Path(env["CAR_APP_STATE_DIR"]) / "iterations.jsonl"
    summary_md_path = Path(env["CAR_APP_ARTIFACT_DIR"]) / "summary.md"
    summary_svg_path = Path(env["CAR_APP_ARTIFACT_DIR"]) / "summary.svg"

    init_result = _run_script(
        "init_run.py",
        "--goal",
        "Reduce p95 latency",
        "--metric",
        "p95 latency",
        "--direction",
        "lower",
        "--unit",
        "ms",
        "--guard-command",
        "python3 -m pytest tests/core/apps",
        "--max-iterations",
        "4",
        "--max-consecutive-discards",
        "2",
        env=env,
    )
    assert init_result.returncode == 0, init_result.stderr
    assert run_path.exists()

    baseline_result = _run_script(
        "record_baseline.py",
        "--value",
        "120",
        "--unit",
        "ms",
        "--summary",
        "main branch baseline",
        env=env,
    )
    assert baseline_result.returncode == 0, baseline_result.stderr

    keep_result = _run_script(
        "record_iteration.py",
        "--iteration",
        "1",
        "--ticket",
        "TICKET-301-autooptimize-iteration.md",
        "--hypothesis",
        "Cache app manifest reads",
        "--value",
        "96",
        "--unit",
        "ms",
        "--decision",
        "keep",
        "--guard-status",
        "pass",
        "--milestone",
        "manifest-cache",
        "--summary",
        "Lower latency without regressions",
        env=env,
    )
    assert keep_result.returncode == 0, keep_result.stderr

    discard_result = _run_script(
        "record_iteration.py",
        "--iteration",
        "2",
        "--ticket",
        "TICKET-302-autooptimize-iteration.md",
        "--hypothesis",
        "Inline discovery cache",
        "--value",
        "111",
        "--unit",
        "ms",
        "--decision",
        "discard",
        "--guard-status",
        "fail",
        "--summary",
        "Regression under test load",
        env=env,
    )
    assert discard_result.returncode == 0, discard_result.stderr

    run_payload = _read_json(run_path)
    assert run_payload["baseline"]["value"] == 120.0
    assert run_payload["best"]["value"] == 96.0
    assert run_payload["best"]["iteration"] == 1

    rows = _read_jsonl(iterations_path)
    assert [row["iteration"] for row in rows] == [1, 2]

    status_result = _run_script("status.py", "--json", env=env)
    assert status_result.returncode == 0, status_result.stderr
    status_payload = json.loads(status_result.stdout)
    assert status_payload["iterations_count"] == 2
    assert status_payload["last_decision"] == "discard"
    assert status_payload["improvement_absolute"] == 24.0
    assert status_payload["guard_status_counts"] == {"fail": 1, "pass": 1}

    validate_result = _run_script("validate_state.py", env=env)
    assert validate_result.returncode == 0, validate_result.stderr

    render_result = _run_script("render_summary_card.py", env=env)
    assert render_result.returncode == 0, render_result.stderr
    assert summary_md_path.exists()
    assert summary_svg_path.exists()

    summary_md = summary_md_path.read_text(encoding="utf-8")
    summary_svg = summary_svg_path.read_text(encoding="utf-8")
    assert "AutoOptimize Summary" in summary_md
    assert "Improvement vs baseline: 24.000 ms (20.00%)" in summary_md
    assert "Guard status counts: fail=1, pass=1" in summary_md
    assert "manifest-cache" in summary_md
    assert "AutoOptimize" in summary_svg
    assert "Guard status: fail=1 pass=1" in summary_svg


def test_record_iteration_force_replaces_duplicate_iteration(tmp_path: Path) -> None:
    env = _app_env(tmp_path)
    run_path = Path(env["CAR_APP_STATE_DIR"]) / "run.json"
    iterations_path = Path(env["CAR_APP_STATE_DIR"]) / "iterations.jsonl"

    assert (
        _run_script(
            "init_run.py",
            "--goal",
            "Reduce p95 latency",
            "--metric",
            "p95 latency",
            "--direction",
            "lower",
            "--unit",
            "ms",
            env=env,
        ).returncode
        == 0
    )
    assert (
        _run_script(
            "record_baseline.py",
            "--value",
            "100",
            "--unit",
            "ms",
            env=env,
        ).returncode
        == 0
    )
    assert (
        _run_script(
            "record_iteration.py",
            "--iteration",
            "1",
            "--ticket",
            "TICKET-301.md",
            "--hypothesis",
            "First attempt",
            "--value",
            "90",
            "--unit",
            "ms",
            "--decision",
            "keep",
            env=env,
        ).returncode
        == 0
    )

    duplicate_result = _run_script(
        "record_iteration.py",
        "--iteration",
        "1",
        "--ticket",
        "TICKET-301.md",
        "--hypothesis",
        "Replacement attempt",
        "--value",
        "98",
        "--unit",
        "ms",
        "--decision",
        "discard",
        env=env,
    )
    assert duplicate_result.returncode != 0
    assert "already exists" in duplicate_result.stderr

    force_result = _run_script(
        "record_iteration.py",
        "--iteration",
        "1",
        "--ticket",
        "TICKET-301.md",
        "--hypothesis",
        "Replacement attempt",
        "--value",
        "98",
        "--unit",
        "ms",
        "--decision",
        "discard",
        "--force",
        env=env,
    )
    assert force_result.returncode == 0, force_result.stderr

    rows = _read_jsonl(iterations_path)
    assert len(rows) == 1
    assert rows[0]["hypothesis"] == "Replacement attempt"
    assert rows[0]["decision"] == "discard"

    run_payload = _read_json(run_path)
    assert run_payload["best"]["source"] == "baseline"
    assert run_payload["best"]["value"] == 100.0


def test_validate_state_reports_invalid_best_without_traceback(tmp_path: Path) -> None:
    env = _app_env(tmp_path)
    run_path = Path(env["CAR_APP_STATE_DIR"]) / "run.json"

    assert (
        _run_script(
            "init_run.py",
            "--goal",
            "Reduce p95 latency",
            "--metric",
            "p95 latency",
            "--direction",
            "lower",
            "--unit",
            "ms",
            env=env,
        ).returncode
        == 0
    )
    assert (
        _run_script(
            "record_baseline.py",
            "--value",
            "120",
            "--unit",
            "ms",
            env=env,
        ).returncode
        == 0
    )
    assert (
        _run_script(
            "record_iteration.py",
            "--iteration",
            "1",
            "--ticket",
            "TICKET-301.md",
            "--hypothesis",
            "Keepable change",
            "--value",
            "90",
            "--unit",
            "ms",
            "--decision",
            "keep",
            env=env,
        ).returncode
        == 0
    )

    run_payload = _read_json(run_path)
    run_payload["best"]["value"] = "oops"
    run_path.write_text(json.dumps(run_payload, indent=2, sort_keys=True) + "\n")

    validate_result = _run_script("validate_state.py", env=env)

    assert validate_result.returncode == 1
    assert "best.value must be numeric" in validate_result.stderr
    assert "Traceback" not in validate_result.stderr
