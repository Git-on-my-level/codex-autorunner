from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Sequence

CATEGORY_WEB = "web_integration"
CATEGORY_CHAT = "chat_integration"
CATEGORY_CLI = "cli_integration"
CATEGORY_FALLBACK = "fallback_robustness"
CATEGORY_OVERALL = "overall"

CATEGORY_ORDER: tuple[str, ...] = (
    CATEGORY_WEB,
    CATEGORY_CHAT,
    CATEGORY_CLI,
    CATEGORY_FALLBACK,
)

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_WEB: "Web Integration",
    CATEGORY_CHAT: "Chat Integration",
    CATEGORY_CLI: "CLI Integration",
    CATEGORY_FALLBACK: "Fallback Robustness",
}

DEFAULT_THRESHOLDS: dict[str, float] = {
    CATEGORY_WEB: 7.0,
    CATEGORY_CHAT: 8.0,
    CATEGORY_CLI: 8.0,
    CATEGORY_FALLBACK: 7.0,
    CATEGORY_OVERALL: 8.0,
}


@dataclass(frozen=True)
class HermesReadinessSignalSpec:
    signal_id: str
    category: str
    description: str
    weight: float
    kind: Literal["static_contains", "pytest"]
    path: Optional[str] = None
    patterns: tuple[str, ...] = ()
    nodeids: tuple[str, ...] = ()


@dataclass(frozen=True)
class HermesReadinessSignalResult:
    signal_id: str
    category: str
    description: str
    weight: float
    passed: bool
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "category": self.category,
            "description": self.description,
            "weight": self.weight,
            "passed": self.passed,
            "details": self.details,
        }


@dataclass(frozen=True)
class HermesReadinessCategoryScore:
    category: str
    label: str
    score: float
    threshold: float
    passed: bool
    earned_weight: float
    total_weight: float
    passed_signals: int
    total_signals: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "label": self.label,
            "score": self.score,
            "threshold": self.threshold,
            "passed": self.passed,
            "earned_weight": self.earned_weight,
            "total_weight": self.total_weight,
            "passed_signals": self.passed_signals,
            "total_signals": self.total_signals,
        }


@dataclass(frozen=True)
class HermesReadinessScorecard:
    categories: dict[str, HermesReadinessCategoryScore]
    overall_score: float
    overall_threshold: float
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "categories": {
                key: value.to_dict() for key, value in self.categories.items()
            },
            "overall_score": self.overall_score,
            "overall_threshold": self.overall_threshold,
            "passed": self.passed,
        }


DEFAULT_SIGNAL_SPECS_SMOKE: tuple[HermesReadinessSignalSpec, ...] = (
    HermesReadinessSignalSpec(
        signal_id="web.feature_routes_static",
        category=CATEGORY_WEB,
        description="Web PMA target + destination/channel routes are present in source",
        weight=3.0,
        kind="static_contains",
        path="src/codex_autorunner/surfaces/web/routes/pma.py",
        patterns=(
            '@router.post("/targets/add")',
            '@router.post("/targets/remove")',
            '@router.post("/targets/clear")',
            '@router.post("/targets/active")',
        ),
    ),
    HermesReadinessSignalSpec(
        signal_id="web.feature_destination_routes_static",
        category=CATEGORY_WEB,
        description="Web hub destination + channel-directory routes are present in source",
        weight=2.0,
        kind="static_contains",
        path="src/codex_autorunner/surfaces/web/routes/hub_repos.py",
        patterns=(
            '@router.get("/hub/repos/{repo_id}/destination")',
            '@router.post("/hub/repos/{repo_id}/destination")',
            '@router.get("/hub/chat/channels")',
        ),
    ),
    HermesReadinessSignalSpec(
        signal_id="web.tests_target_management",
        category=CATEGORY_WEB,
        description="Web PMA target management API behavior",
        weight=3.0,
        kind="pytest",
        nodeids=(
            "tests/test_pma_routes.py::test_pma_targets_add_list_remove_clear",
            "tests/test_pma_routes.py::test_pma_targets_active_get_and_set",
            "tests/test_pma_routes.py::test_pma_targets_reject_invalid_ref",
        ),
    ),
    HermesReadinessSignalSpec(
        signal_id="web.tests_destination_channels",
        category=CATEGORY_WEB,
        description="Web destination mutation + channel directory behavior",
        weight=2.0,
        kind="pytest",
        nodeids=(
            "tests/surfaces/web/test_hub_destination_and_channels.py::test_hub_destination_routes_show_set_and_persist",
            "tests/surfaces/web/test_hub_destination_and_channels.py::test_hub_channel_directory_route_lists_and_filters",
        ),
    ),
    HermesReadinessSignalSpec(
        signal_id="chat.tests_telegram_target_matrix",
        category=CATEGORY_CHAT,
        description="Telegram target parser/command matrix behavior",
        weight=4.0,
        kind="pytest",
        nodeids=(
            "tests/test_telegram_pma_routing.py::test_pma_target_ref_matrix_telegram_matches_canonical_parser",
            "tests/test_telegram_pma_routing.py::test_pma_target_add_list_rm_clear_mutates_store",
        ),
    ),
    HermesReadinessSignalSpec(
        signal_id="chat.tests_discord_target_matrix",
        category=CATEGORY_CHAT,
        description="Discord target parser/command matrix behavior",
        weight=4.0,
        kind="pytest",
        nodeids=(
            "tests/integrations/discord/test_pma_commands.py::test_pma_target_ref_matrix_discord_matches_canonical_parser",
            "tests/integrations/discord/test_pma_commands.py::test_pma_target_add_list_remove_and_clear",
        ),
    ),
    HermesReadinessSignalSpec(
        signal_id="chat.tests_cross_surface_parity",
        category=CATEGORY_CHAT,
        description="Cross-surface target sequence parity (Telegram vs CLI)",
        weight=2.0,
        kind="pytest",
        nodeids=(
            "tests/test_telegram_pma_routing.py::test_pma_target_ref_sequence_matches_cli_store_payload",
        ),
    ),
    HermesReadinessSignalSpec(
        signal_id="cli.tests_target_commands",
        category=CATEGORY_CLI,
        description="CLI PMA target add/list/remove/clear + invalid ref handling",
        weight=5.0,
        kind="pytest",
        nodeids=(
            "tests/test_pma_cli.py::test_pma_targets_add_list_rm_clear",
            "tests/test_pma_cli.py::test_pma_targets_add_rejects_invalid_ref",
        ),
    ),
    HermesReadinessSignalSpec(
        signal_id="cli.tests_target_matrix",
        category=CATEGORY_CLI,
        description="CLI target parser matrix parity with canonical parser",
        weight=5.0,
        kind="pytest",
        nodeids=(
            "tests/test_pma_cli.py::test_pma_target_ref_matrix_cli_matches_canonical_parser",
        ),
    ),
    HermesReadinessSignalSpec(
        signal_id="fallback.feature_outcome_fields_static",
        category=CATEGORY_FALLBACK,
        description="Web PMA route exposes structured delivery outcomes",
        weight=2.0,
        kind="static_contains",
        path="src/codex_autorunner/surfaces/web/routes/pma.py",
        patterns=(
            "delivery_outcome",
            "dispatch_delivery_outcome",
        ),
    ),
    HermesReadinessSignalSpec(
        signal_id="fallback.tests_partial_success",
        category=CATEGORY_FALLBACK,
        description="Partial-success delivery keeps successful targets and records failures",
        weight=4.0,
        kind="pytest",
        nodeids=(
            "tests/integrations/test_pma_delivery_routing.py::test_pma_delivery_partial_failure_isolation",
            "tests/integrations/test_pma_delivery_routing.py::test_pma_delivery_mirror_includes_errors_when_targets_fail",
        ),
    ),
    HermesReadinessSignalSpec(
        signal_id="fallback.tests_duplicate_skip_failure",
        category=CATEGORY_FALLBACK,
        description="Duplicate-skip and failure coexistence semantics",
        weight=2.0,
        kind="pytest",
        nodeids=(
            "tests/integrations/test_pma_delivery_routing.py::test_pma_delivery_duplicate_skip_and_failure_can_coexist",
        ),
    ),
    HermesReadinessSignalSpec(
        signal_id="fallback.tests_web_non_implicit_active",
        category=CATEGORY_FALLBACK,
        description="Web PMA chat does not implicitly reassign active target",
        weight=2.0,
        kind="pytest",
        nodeids=(
            "tests/test_pma_routes.py::test_pma_chat_does_not_implicitly_set_web_active_target",
        ),
    ),
)


def default_signal_specs(*, ci_smoke: bool = False) -> list[HermesReadinessSignalSpec]:
    if ci_smoke:
        return list(DEFAULT_SIGNAL_SPECS_SMOKE)
    return list(DEFAULT_SIGNAL_SPECS_SMOKE)


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if candidate:
            return candidate
    return ""


def _run_static_contains(
    spec: HermesReadinessSignalSpec, *, repo_root: Path
) -> HermesReadinessSignalResult:
    if not spec.path:
        return HermesReadinessSignalResult(
            signal_id=spec.signal_id,
            category=spec.category,
            description=spec.description,
            weight=spec.weight,
            passed=False,
            details="missing path for static check",
        )
    target_path = (repo_root / spec.path).resolve()
    if not target_path.exists():
        return HermesReadinessSignalResult(
            signal_id=spec.signal_id,
            category=spec.category,
            description=spec.description,
            weight=spec.weight,
            passed=False,
            details=f"missing file: {target_path}",
        )
    source = target_path.read_text(encoding="utf-8")
    missing_patterns = [pattern for pattern in spec.patterns if pattern not in source]
    if missing_patterns:
        return HermesReadinessSignalResult(
            signal_id=spec.signal_id,
            category=spec.category,
            description=spec.description,
            weight=spec.weight,
            passed=False,
            details="missing patterns: " + ", ".join(missing_patterns),
        )
    return HermesReadinessSignalResult(
        signal_id=spec.signal_id,
        category=spec.category,
        description=spec.description,
        weight=spec.weight,
        passed=True,
        details="ok",
    )


def _run_pytest_signal(
    spec: HermesReadinessSignalSpec,
    *,
    repo_root: Path,
    python_executable: str,
) -> HermesReadinessSignalResult:
    if not spec.nodeids:
        return HermesReadinessSignalResult(
            signal_id=spec.signal_id,
            category=spec.category,
            description=spec.description,
            weight=spec.weight,
            passed=False,
            details="missing nodeids for pytest check",
        )
    cmd = [python_executable, "-m", "pytest", "-q", *spec.nodeids]
    completed = subprocess.run(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(
        chunk for chunk in [completed.stdout.strip(), completed.stderr.strip()] if chunk
    )
    summary = _last_nonempty_line(output) or f"exit={completed.returncode}"
    return HermesReadinessSignalResult(
        signal_id=spec.signal_id,
        category=spec.category,
        description=spec.description,
        weight=spec.weight,
        passed=completed.returncode == 0,
        details=summary,
    )


def collect_signal_results(
    specs: Sequence[HermesReadinessSignalSpec],
    *,
    repo_root: Path,
    python_executable: str = sys.executable,
) -> list[HermesReadinessSignalResult]:
    results: list[HermesReadinessSignalResult] = []
    for spec in specs:
        if spec.kind == "static_contains":
            results.append(_run_static_contains(spec, repo_root=repo_root))
            continue
        if spec.kind == "pytest":
            results.append(
                _run_pytest_signal(
                    spec, repo_root=repo_root, python_executable=python_executable
                )
            )
            continue
        results.append(
            HermesReadinessSignalResult(
                signal_id=spec.signal_id,
                category=spec.category,
                description=spec.description,
                weight=spec.weight,
                passed=False,
                details=f"unsupported signal kind: {spec.kind}",
            )
        )
    return results


def compute_scorecard(
    signals: Sequence[HermesReadinessSignalResult],
    *,
    thresholds: Optional[Mapping[str, float]] = None,
) -> HermesReadinessScorecard:
    resolved_thresholds = dict(DEFAULT_THRESHOLDS)
    if thresholds is not None:
        for key, value in thresholds.items():
            resolved_thresholds[key] = float(value)

    categories: dict[str, HermesReadinessCategoryScore] = {}
    for category in CATEGORY_ORDER:
        category_signals = [signal for signal in signals if signal.category == category]
        total_weight = sum(max(signal.weight, 0.0) for signal in category_signals)
        earned_weight = sum(
            max(signal.weight, 0.0) for signal in category_signals if signal.passed
        )
        score = round((earned_weight / total_weight) * 10.0, 1) if total_weight else 0.0
        threshold = float(resolved_thresholds.get(category, 0.0))
        category_passed = score >= threshold
        categories[category] = HermesReadinessCategoryScore(
            category=category,
            label=CATEGORY_LABELS.get(category, category),
            score=score,
            threshold=threshold,
            passed=category_passed,
            earned_weight=earned_weight,
            total_weight=total_weight,
            passed_signals=sum(1 for signal in category_signals if signal.passed),
            total_signals=len(category_signals),
        )

    overall_score = round(
        sum(category.score for category in categories.values()) / len(CATEGORY_ORDER), 1
    )
    overall_threshold = float(resolved_thresholds.get(CATEGORY_OVERALL, 0.0))
    passed = overall_score >= overall_threshold and all(
        category.passed for category in categories.values()
    )
    return HermesReadinessScorecard(
        categories=categories,
        overall_score=overall_score,
        overall_threshold=overall_threshold,
        passed=passed,
    )


def load_signal_results(path: Path) -> list[HermesReadinessSignalResult]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries: Any
    if isinstance(payload, dict):
        entries = payload.get("signals")
    else:
        entries = payload
    if not isinstance(entries, list):
        raise ValueError("signals payload must be a list or object with 'signals' list")

    results: list[HermesReadinessSignalResult] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("each signal entry must be an object")
        signal_id = str(entry.get("signal_id") or "").strip()
        category = str(entry.get("category") or "").strip()
        description = str(entry.get("description") or "").strip()
        if not signal_id or not category:
            raise ValueError("each signal entry requires signal_id and category")
        results.append(
            HermesReadinessSignalResult(
                signal_id=signal_id,
                category=category,
                description=description,
                weight=float(entry.get("weight") or 1.0),
                passed=bool(entry.get("passed")),
                details=str(entry.get("details") or ""),
            )
        )
    return results


def render_scorecard_text(
    scorecard: HermesReadinessScorecard,
    *,
    signals: Optional[Sequence[HermesReadinessSignalResult]] = None,
) -> str:
    lines = ["Hermes Readiness Scorecard"]
    for category in CATEGORY_ORDER:
        category_score = scorecard.categories[category]
        status = "PASS" if category_score.passed else "FAIL"
        lines.append(
            (
                f"- {category_score.label}: {category_score.score:.1f}/10 "
                f"(threshold {category_score.threshold:.1f}) [{status}]"
            )
        )
    overall_status = "PASS" if scorecard.passed else "FAIL"
    lines.append(
        (
            f"- Overall: {scorecard.overall_score:.1f}/10 "
            f"(threshold {scorecard.overall_threshold:.1f}) [{overall_status}]"
        )
    )
    if signals:
        lines.append("")
        lines.append("Signals:")
        for signal in signals:
            status = "PASS" if signal.passed else "FAIL"
            lines.append(
                f"- [{status}] {signal.signal_id}: {signal.description} ({signal.details})"
            )
    return "\n".join(lines)


# Referenced by script entrypoints outside src/; keep explicit symbol uses so
# heuristic dead-code checks treat these exported APIs as live.
_PUBLIC_API_REFERENCES = (
    default_signal_specs,
    collect_signal_results,
    compute_scorecard,
    load_signal_results,
    render_scorecard_text,
)


__all__ = [
    "CATEGORY_CHAT",
    "CATEGORY_CLI",
    "CATEGORY_FALLBACK",
    "CATEGORY_ORDER",
    "CATEGORY_OVERALL",
    "CATEGORY_WEB",
    "DEFAULT_SIGNAL_SPECS_SMOKE",
    "DEFAULT_THRESHOLDS",
    "HermesReadinessCategoryScore",
    "HermesReadinessScorecard",
    "HermesReadinessSignalResult",
    "HermesReadinessSignalSpec",
    "collect_signal_results",
    "compute_scorecard",
    "default_signal_specs",
    "load_signal_results",
    "render_scorecard_text",
]
