from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from codex_autorunner.core.hermes_readiness import (
    CATEGORY_CHAT,
    CATEGORY_CLI,
    CATEGORY_FALLBACK,
    CATEGORY_WEB,
    HermesReadinessSignalResult,
    compute_scorecard,
)


def _all_pass_signals() -> list[HermesReadinessSignalResult]:
    return [
        HermesReadinessSignalResult(
            signal_id="web",
            category=CATEGORY_WEB,
            description="web",
            weight=1.0,
            passed=True,
            details="ok",
        ),
        HermesReadinessSignalResult(
            signal_id="chat",
            category=CATEGORY_CHAT,
            description="chat",
            weight=1.0,
            passed=True,
            details="ok",
        ),
        HermesReadinessSignalResult(
            signal_id="cli",
            category=CATEGORY_CLI,
            description="cli",
            weight=1.0,
            passed=True,
            details="ok",
        ),
        HermesReadinessSignalResult(
            signal_id="fallback",
            category=CATEGORY_FALLBACK,
            description="fallback",
            weight=1.0,
            passed=True,
            details="ok",
        ),
    ]


def test_compute_scorecard_passes_when_thresholds_met() -> None:
    scorecard = compute_scorecard(_all_pass_signals())
    assert scorecard.passed is True
    assert scorecard.overall_score == 10.0
    assert scorecard.categories[CATEGORY_WEB].score == 10.0
    assert scorecard.categories[CATEGORY_CHAT].score == 10.0
    assert scorecard.categories[CATEGORY_CLI].score == 10.0
    assert scorecard.categories[CATEGORY_FALLBACK].score == 10.0


def test_compute_scorecard_fails_when_category_below_threshold() -> None:
    signals = _all_pass_signals()
    signals[0] = HermesReadinessSignalResult(
        signal_id="web",
        category=CATEGORY_WEB,
        description="web",
        weight=1.0,
        passed=False,
        details="missing",
    )
    scorecard = compute_scorecard(signals)
    assert scorecard.passed is False
    assert scorecard.categories[CATEGORY_WEB].passed is False
    assert scorecard.categories[CATEGORY_WEB].score == 0.0


def test_readiness_command_fails_when_signals_below_threshold(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "hermes_readiness_scorecard.py"
    payload_path = tmp_path / "signals.json"

    payload = {
        "signals": [
            {
                "signal_id": "web.fail",
                "category": CATEGORY_WEB,
                "description": "web failing signal",
                "weight": 1.0,
                "passed": False,
                "details": "missing route",
            },
            {
                "signal_id": "chat.pass",
                "category": CATEGORY_CHAT,
                "description": "chat passing signal",
                "weight": 1.0,
                "passed": True,
                "details": "ok",
            },
            {
                "signal_id": "cli.pass",
                "category": CATEGORY_CLI,
                "description": "cli passing signal",
                "weight": 1.0,
                "passed": True,
                "details": "ok",
            },
            {
                "signal_id": "fallback.pass",
                "category": CATEGORY_FALLBACK,
                "description": "fallback passing signal",
                "weight": 1.0,
                "passed": True,
                "details": "ok",
            },
        ]
    }
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--signals-json",
            str(payload_path),
            "--json",
        ],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 1
    output = json.loads(completed.stdout)
    assert output["scorecard"]["passed"] is False
