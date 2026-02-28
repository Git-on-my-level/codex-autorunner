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
    HermesReadinessSignalSpec,
    collect_signal_results,
    compute_scorecard,
    default_signal_specs,
    load_signal_results,
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


def test_default_signal_specs_behavior_weights_dominate_static_checks() -> None:
    specs = default_signal_specs(ci_smoke=True)
    for category in (CATEGORY_WEB, CATEGORY_FALLBACK):
        static_weight = sum(
            spec.weight
            for spec in specs
            if spec.category == category and spec.kind == "static_contains"
        )
        behavior_weight = sum(
            spec.weight
            for spec in specs
            if spec.category == category and spec.kind == "pytest"
        )
        assert behavior_weight > static_weight


def test_behavior_regression_can_fail_scorecard_even_if_static_signal_passes() -> None:
    signals = [
        HermesReadinessSignalResult(
            signal_id="web.contract.static",
            category=CATEGORY_WEB,
            description="web static contract presence",
            weight=1.0,
            passed=True,
            details="ok",
        ),
        HermesReadinessSignalResult(
            signal_id="web.behavior.fail",
            category=CATEGORY_WEB,
            description="web behavior regression",
            weight=9.0,
            passed=False,
            details="FAILED tests/test_pma_routes.py::test_pma_targets_active_get_and_set",
        ),
        HermesReadinessSignalResult(
            signal_id="chat.pass",
            category=CATEGORY_CHAT,
            description="chat",
            weight=1.0,
            passed=True,
            details="ok",
        ),
        HermesReadinessSignalResult(
            signal_id="cli.pass",
            category=CATEGORY_CLI,
            description="cli",
            weight=1.0,
            passed=True,
            details="ok",
        ),
        HermesReadinessSignalResult(
            signal_id="fallback.pass",
            category=CATEGORY_FALLBACK,
            description="fallback",
            weight=1.0,
            passed=True,
            details="ok",
        ),
    ]
    scorecard = compute_scorecard(signals)
    assert scorecard.categories[CATEGORY_WEB].score == 1.0
    assert scorecard.categories[CATEGORY_WEB].passed is False
    assert scorecard.passed is False


def test_collect_signal_results_pytest_failure_details_include_nodeids() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    missing_nodeid = "tests/does_not_exist.py::test_missing"
    spec = HermesReadinessSignalSpec(
        signal_id="test.failure.details",
        category=CATEGORY_WEB,
        description="failing pytest node should include nodeids",
        weight=1.0,
        kind="pytest",
        nodeids=(missing_nodeid,),
    )
    [result] = collect_signal_results([spec], repo_root=repo_root)
    assert result.passed is False
    assert missing_nodeid in result.details
    assert "pytest -q" in result.details


def test_load_signal_results_supports_list_payload(tmp_path: Path) -> None:
    payload_path = tmp_path / "signals-list.json"
    payload_path.write_text(
        json.dumps([signal.to_dict() for signal in _all_pass_signals()]),
        encoding="utf-8",
    )
    loaded = load_signal_results(payload_path)
    assert len(loaded) == 4
    scorecard = compute_scorecard(loaded)
    assert scorecard.passed is True


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
