"""Chat integration doctor checks."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.runtime import DoctorCheck
from .chat_ux_telemetry import get_global_accumulator
from .parity_checker import ParityCheckResult, run_parity_checks
from .ux_regression_contract import (
    REQUIRED_CHAT_UX_LATENCY_BUDGET_IDS,
    REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS,
)

_CHECK_GROUP = "chat.parity_contract"
_DIAGNOSTICS_GROUP = "chat.ux_timing_diagnostics"
_LAB_CHECK_GROUP = "chat.surface_lab"
_DISCORD_SERVICE = "src/codex_autorunner/integrations/discord/service.py"
_TELEGRAM_TRIGGER_MODE = "src/codex_autorunner/integrations/telegram/trigger_mode.py"
_TELEGRAM_MESSAGES = "src/codex_autorunner/integrations/telegram/handlers/messages.py"
_LAB_SCENARIO_ROOT = Path("tests/chat_surface_lab/scenarios")
_LAB_ARTIFACT_MANIFESTS_PATH = Path("tests/chat_surface_lab/artifact_manifests.py")
_LAB_EVIDENCE_ARTIFACTS_PATH = Path("tests/chat_surface_lab/evidence_artifacts.py")
_LAB_REQUIRED_SCENARIO_IDS = (
    "first_visible_feedback",
    "progress_anchor_reuse",
    "queued_visibility",
    "interrupt_optimistic_acceptance",
    "interrupt_confirmation",
    "timeout_when_terminal_missing",
    "duplicate_delivery",
    "restart_recovery",
    "restart_window_duplicate_delivery",
)
_LAB_REQUIRED_ARTIFACT_KINDS = (
    "accessibility_snapshot_json",
    "artifact_manifest_json",
    "normalized_transcript_json",
    "rendered_html",
    "screenshot_png",
    "structured_log_json",
    "surface_timeline_json",
    "timing_report_json",
)
_LAB_REQUIRED_ARTIFACT_FILENAMES = (
    "manifest.json",
    "transcript.json",
    "timeline.json",
    "transcript.html",
    "logs.json",
    "timing_report.json",
    "screenshot.png",
    "a11y_snapshot.json",
)


@dataclass(frozen=True)
class ChatSurfaceLabContractStatus:
    skipped: bool = False
    skip_reason: str = ""
    scenario_count: int = 0
    parse_errors: tuple[str, ...] = ()
    missing_required_scenarios: tuple[str, ...] = ()
    missing_regression_links: tuple[str, ...] = ()
    unknown_regression_links: tuple[str, ...] = ()
    missing_latency_budget_links: tuple[str, ...] = ()
    unknown_latency_budget_links: tuple[str, ...] = ()
    missing_latency_budget_assertions: tuple[str, ...] = ()
    missing_artifact_kinds: tuple[str, ...] = ()
    missing_artifact_filenames: tuple[str, ...] = ()


def chat_doctor_checks(repo_root: Path | None = None) -> list[DoctorCheck]:
    """Run chat parity-contract checks used by `car doctor`."""
    checks: list[DoctorCheck] = []

    for result in run_parity_checks(repo_root=repo_root):
        check_id = _CHECK_GROUP
        if result.passed:
            checks.append(
                DoctorCheck(
                    name=f"Chat parity contract ({result.id})",
                    passed=True,
                    message=result.message,
                    severity="info",
                    check_id=check_id,
                )
            )
            continue

        message, fix = _failure_details(result)
        checks.append(
            DoctorCheck(
                name=f"Chat parity contract ({result.id})",
                passed=False,
                message=message,
                check_id=check_id,
                fix=fix,
            )
        )

    return checks


def chat_surface_lab_doctor_checks(repo_root: Path | None = None) -> list[DoctorCheck]:
    status = _collect_chat_surface_lab_contract_status(repo_root=repo_root)
    if status.skipped:
        return [
            DoctorCheck(
                name="Chat surface lab contract",
                passed=True,
                message=(
                    "Skipped chat-surface lab contract checks: "
                    f"{status.skip_reason or 'repo checkout unavailable'}."
                ),
                severity="info",
                check_id=f"{_LAB_CHECK_GROUP}.skipped",
            )
        ]

    scenario_issues: list[str] = []
    if status.parse_errors:
        scenario_issues.append(
            f"parse_errors={', '.join(status.parse_errors[:3])}"
            + ("..." if len(status.parse_errors) > 3 else "")
        )
    if status.missing_required_scenarios:
        scenario_issues.append(
            "missing_scenarios=" + ", ".join(status.missing_required_scenarios)
        )

    contract_issues: list[str] = []
    if status.missing_regression_links:
        contract_issues.append(
            "missing_regression_links=" + ", ".join(status.missing_regression_links)
        )
    if status.unknown_regression_links:
        contract_issues.append(
            "unknown_regression_links=" + ", ".join(status.unknown_regression_links)
        )
    if status.missing_latency_budget_links:
        contract_issues.append(
            "missing_latency_budget_links="
            + ", ".join(status.missing_latency_budget_links)
        )
    if status.unknown_latency_budget_links:
        contract_issues.append(
            "unknown_latency_budget_links="
            + ", ".join(status.unknown_latency_budget_links)
        )
    if status.missing_latency_budget_assertions:
        contract_issues.append(
            "missing_latency_budget_assertions="
            + ", ".join(status.missing_latency_budget_assertions)
        )

    artifact_issues: list[str] = []
    if status.missing_artifact_kinds:
        artifact_issues.append(
            "missing_artifact_kinds=" + ", ".join(status.missing_artifact_kinds)
        )
    if status.missing_artifact_filenames:
        artifact_issues.append(
            "missing_artifact_filenames=" + ", ".join(status.missing_artifact_filenames)
        )

    checks = [
        DoctorCheck(
            name="Chat surface lab scenario corpus",
            passed=not scenario_issues,
            message=(
                f"Scenario corpus loaded ({status.scenario_count} files)."
                if not scenario_issues
                else "Chat surface lab scenario corpus is incomplete ("
                + "; ".join(scenario_issues)
                + ")."
            ),
            severity="info" if not scenario_issues else "error",
            check_id=f"{_LAB_CHECK_GROUP}.scenario_corpus",
            fix=(
                "Repair/add JSON fixtures under `tests/chat_surface_lab/scenarios/` "
                "to restore required scenario IDs, then run `make test-chat-surface-lab`."
                if scenario_issues
                else None
            ),
        ),
        DoctorCheck(
            name="Chat surface lab contract linkage",
            passed=not contract_issues,
            message=(
                "Scenario corpus links cover required regression and latency contracts."
                if not contract_issues
                else "Chat surface lab contract linkage drift detected ("
                + "; ".join(contract_issues)
                + ")."
            ),
            severity="info" if not contract_issues else "error",
            check_id=f"{_LAB_CHECK_GROUP}.contract_links",
            fix=(
                "Update `contract_links` and `latency_budgets` in "
                "`tests/chat_surface_lab/scenarios/*.json` so they cover "
                "`REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS` and "
                "`REQUIRED_CHAT_UX_LATENCY_BUDGET_IDS`."
                if contract_issues
                else None
            ),
        ),
        DoctorCheck(
            name="Chat surface lab artifact contract",
            passed=not artifact_issues,
            message=(
                "Artifact kind and filename contract matches expected lab schema."
                if not artifact_issues
                else "Chat surface lab artifact contract drift detected ("
                + "; ".join(artifact_issues)
                + ")."
            ),
            severity="info" if not artifact_issues else "error",
            check_id=f"{_LAB_CHECK_GROUP}.artifact_contract",
            fix=(
                "Restore artifact kind enums in "
                "`tests/chat_surface_lab/artifact_manifests.py` and stable filenames in "
                "`tests/chat_surface_lab/evidence_artifacts.py`."
                if artifact_issues
                else None
            ),
        ),
    ]
    return checks


def chat_ux_timing_diagnostic_checks() -> list[DoctorCheck]:
    acc = get_global_accumulator()
    lines = acc.format_diagnostic_lines()
    summaries = acc.platform_summaries()
    message = "\n".join(lines)

    if acc.snapshot_count == 0:
        return [
            DoctorCheck(
                name="Chat UX timing accumulator",
                passed=True,
                message=message,
                severity="info",
                check_id=_DIAGNOSTICS_GROUP,
            )
        ]

    checks: list[DoctorCheck] = []
    checks.append(
        DoctorCheck(
            name="Chat UX timing accumulator",
            passed=True,
            message=message,
            severity="info",
            check_id=_DIAGNOSTICS_GROUP,
        )
    )

    for ps in summaries:
        slow_deltas = [
            ds for ds in ps.deltas if ds.p95_ms is not None and ds.p95_ms > 3000
        ]
        if slow_deltas:
            slow_labels = ", ".join(
                f"{ds.label}(p95={ds.p95_ms:.0f}ms)" for ds in slow_deltas
            )
            checks.append(
                DoctorCheck(
                    name=f"Chat UX slow path [{ps.platform}]",
                    passed=False,
                    message=f"High-p95 deltas detected: {slow_labels}",
                    check_id=f"{_DIAGNOSTICS_GROUP}.{ps.platform}.slow_path",
                    fix="Investigate slow ack/feedback/interrupt paths via log search for "
                    "`chat_ux_timing` events and compare against latency budgets in "
                    "`ux_regression_contract.py`.",
                )
            )

    return checks


def _failure_details(result: ParityCheckResult) -> tuple[str, str]:
    if result.id == "contract.registry_entries_cataloged":
        missing_discord = _metadata_list(result.metadata, "missing_discord_paths")
        missing_telegram = _metadata_list(result.metadata, "missing_telegram_commands")
        stable_missing_surface = _metadata_list(
            result.metadata, "stable_missing_surface_mapping"
        )
        problems: list[str] = []
        if missing_discord:
            problems.append(f"discord={', '.join(missing_discord)}")
        if missing_telegram:
            problems.append(f"telegram={', '.join(missing_telegram)}")
        if stable_missing_surface:
            problems.append(f"stable_surface={', '.join(stable_missing_surface)}")
        details = "; ".join(problems) if problems else "<unknown>"
        return (
            "Chat parity contract failed: command registry coverage is incomplete "
            f"({details}).",
            "Update `COMMAND_CONTRACT` so every user-facing Telegram/Discord command "
            "is cataloged with explicit status and stable entries define both "
            "Telegram and Discord mappings.",
        )

    if result.id == "discord.contract_commands_routed":
        missing_ids = _metadata_list(result.metadata, "missing_ids")
        missing_text = ", ".join(missing_ids) if missing_ids else "<unknown>"
        return (
            "Chat parity contract failed: missing Discord command route handling for "
            f"{missing_text}.",
            "Add explicit command-path branches for the missing contract commands in "
            f"`{_DISCORD_SERVICE}` before generic fallbacks.",
        )

    if result.id == "discord.no_generic_fallback_leak":
        failed = _metadata_list(result.metadata, "failed_predicates")
        failed_text = ", ".join(failed) if failed else "<unknown>"
        return (
            "Chat parity contract failed: known Discord command prefixes can leak to "
            f"generic fallback (missing guards: {failed_text}).",
            "Restore command-prefix guards and command-specific fallbacks in "
            f"`{_DISCORD_SERVICE}` so known commands do not hit the generic fallback path.",
        )

    if result.id == "discord.canonical_command_ingress_usage":
        failed = _metadata_list(result.metadata, "failed_predicates")
        failed_text = ", ".join(failed) if failed else "<unknown>"
        return (
            "Chat parity contract failed: shared helper usage for command ingress is "
            f"missing in Discord (missing checks: {failed_text}).",
            "Use shared `canonicalize_command_ingress(...)` in both normalized and "
            f"interaction command paths in `{_DISCORD_SERVICE}`.",
        )

    if result.id == "chat.shared_plain_text_turn_policy_usage":
        failed = _metadata_list(result.metadata, "failed_predicates")
        failed_text = ", ".join(failed) if failed else "<unknown>"
        return (
            "Chat parity contract failed: shared helper usage for plain-text turn "
            f"policy is incomplete (missing checks: {failed_text}).",
            "Route Telegram and Discord trigger paths through "
            "`should_trigger_plain_text_turn(...)` with `PlainTextTurnContext` in "
            f"`{_TELEGRAM_TRIGGER_MODE}`, `{_TELEGRAM_MESSAGES}`, and `{_DISCORD_SERVICE}`.",
        )

    if result.id == "chat.ux_latency_budget_contract_complete":
        missing = _metadata_list(result.metadata, "missing_required_budget_ids")
        invalid = _metadata_list(result.metadata, "invalid_budget_ids")
        duplicates = _metadata_list(result.metadata, "duplicate_budget_ids")
        problems = []
        if missing:
            problems.append(f"missing={', '.join(missing)}")
        if invalid:
            problems.append(f"invalid={', '.join(invalid)}")
        if duplicates:
            problems.append(f"duplicate={', '.join(duplicates)}")
        detail = "; ".join(problems) if problems else "<unknown>"
        return (
            "Chat parity contract failed: UX latency budgets are incomplete "
            f"({detail}).",
            "Restore the required entries in "
            "`src/codex_autorunner/integrations/chat/ux_regression_contract.py` "
            "so first-visible, queue-visible, first-progress, and interrupt-visible "
            "gates remain declared.",
        )

    if result.id == "chat.ux_regression_contract_complete":
        missing = _metadata_list(result.metadata, "missing_required_scenario_ids")
        paths = _metadata_list(result.metadata, "missing_test_paths")
        surface = _metadata_list(result.metadata, "scenarios_missing_surface_coverage")
        budgets = _metadata_list(result.metadata, "scenarios_with_missing_budget_refs")
        empty = _metadata_list(result.metadata, "scenarios_with_empty_tests")
        duplicates = _metadata_list(result.metadata, "duplicate_scenario_ids")
        problems = []
        if missing:
            problems.append(f"missing={', '.join(missing)}")
        if paths:
            problems.append(f"missing_paths={', '.join(paths)}")
        if surface:
            problems.append(f"surface={', '.join(surface)}")
        if budgets:
            problems.append(f"budget_refs={', '.join(budgets)}")
        if empty:
            problems.append(f"empty={', '.join(empty)}")
        if duplicates:
            problems.append(f"duplicate={', '.join(duplicates)}")
        detail = "; ".join(problems) if problems else "<unknown>"
        return (
            "Chat parity contract failed: UX regression coverage declarations are "
            f"incomplete ({detail}).",
            "Update "
            "`src/codex_autorunner/integrations/chat/ux_regression_contract.py` "
            "so each required UX scenario points at concrete regression tests and "
            "shared Discord/Telegram scenarios declare both surfaces.",
        )

    return (
        f"Chat parity contract failed: {result.message}",
        "Run parity checks and align command routing/guard helpers across chat surfaces.",
    )


def _metadata_list(metadata: dict[str, Any], key: str) -> list[str]:
    raw = metadata.get(key)
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _collect_chat_surface_lab_contract_status(
    repo_root: Path | None = None,
) -> ChatSurfaceLabContractStatus:
    scenario_root = _resolve_repo_file_path(
        repo_root=repo_root,
        repo_relative_path=_LAB_SCENARIO_ROOT,
        require_directory=True,
    )
    manifests_path = _resolve_repo_file_path(
        repo_root=repo_root,
        repo_relative_path=_LAB_ARTIFACT_MANIFESTS_PATH,
    )
    artifacts_path = _resolve_repo_file_path(
        repo_root=repo_root,
        repo_relative_path=_LAB_EVIDENCE_ARTIFACTS_PATH,
    )
    if scenario_root is None or manifests_path is None or artifacts_path is None:
        missing = []
        if scenario_root is None:
            missing.append(str(_LAB_SCENARIO_ROOT))
        if manifests_path is None:
            missing.append(str(_LAB_ARTIFACT_MANIFESTS_PATH))
        if artifacts_path is None:
            missing.append(str(_LAB_EVIDENCE_ARTIFACTS_PATH))
        return ChatSurfaceLabContractStatus(
            skipped=True,
            skip_reason="missing checkout paths: " + ", ".join(missing),
        )

    parse_errors: list[str] = []
    scenario_ids: set[str] = set()
    linked_regression_ids: set[str] = set()
    linked_latency_budget_ids: set[str] = set()
    asserted_latency_budget_ids: set[str] = set()

    scenario_files = sorted(path for path in scenario_root.rglob("*.json"))
    for path in scenario_files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parse_errors.append(f"{path.name}:{exc}")
            continue
        if not isinstance(raw, dict):
            parse_errors.append(f"{path.name}:scenario JSON must be an object")
            continue
        scenario_id = str(raw.get("scenario_id") or "").strip()
        if not scenario_id:
            parse_errors.append(f"{path.name}:missing scenario_id")
            continue
        scenario_ids.add(scenario_id)

        execution_mode = str(raw.get("execution_mode") or "surface_harness").strip()
        contract_links = raw.get("contract_links")
        if isinstance(contract_links, dict):
            linked_regression_ids.update(
                _coerce_string_list(contract_links.get("regression_ids"))
            )
            linked_latency_budget_ids.update(
                _coerce_string_list(contract_links.get("latency_budget_ids"))
            )

        if execution_mode != "reference_only":
            budgets = raw.get("latency_budgets")
            if isinstance(budgets, list):
                for item in budgets:
                    if not isinstance(item, dict):
                        continue
                    budget_id = str(item.get("budget_id") or "").strip()
                    if budget_id:
                        asserted_latency_budget_ids.add(budget_id)

    required_scenarios = set(_LAB_REQUIRED_SCENARIO_IDS)
    known_regression_ids = set(REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS)
    known_latency_budget_ids = set(REQUIRED_CHAT_UX_LATENCY_BUDGET_IDS)
    missing_required_scenarios = tuple(sorted(required_scenarios - scenario_ids))
    missing_regression_links = tuple(
        sorted(known_regression_ids - linked_regression_ids)
    )
    unknown_regression_links = tuple(
        sorted(linked_regression_ids - known_regression_ids)
    )
    missing_latency_budget_links = tuple(
        sorted(known_latency_budget_ids - linked_latency_budget_ids)
    )
    unknown_latency_budget_links = tuple(
        sorted(linked_latency_budget_ids - known_latency_budget_ids)
    )
    missing_latency_budget_assertions = tuple(
        sorted(known_latency_budget_ids - asserted_latency_budget_ids)
    )

    artifact_kind_values = _parse_artifact_kind_values(manifests_path)
    declared_filenames = _parse_evidence_artifact_filenames(artifacts_path)
    missing_artifact_kinds = tuple(
        sorted(set(_LAB_REQUIRED_ARTIFACT_KINDS) - artifact_kind_values)
    )
    missing_artifact_filenames = tuple(
        sorted(set(_LAB_REQUIRED_ARTIFACT_FILENAMES) - declared_filenames)
    )

    return ChatSurfaceLabContractStatus(
        skipped=False,
        scenario_count=len(scenario_files),
        parse_errors=tuple(sorted(parse_errors)),
        missing_required_scenarios=missing_required_scenarios,
        missing_regression_links=missing_regression_links,
        unknown_regression_links=unknown_regression_links,
        missing_latency_budget_links=missing_latency_budget_links,
        unknown_latency_budget_links=unknown_latency_budget_links,
        missing_latency_budget_assertions=missing_latency_budget_assertions,
        missing_artifact_kinds=missing_artifact_kinds,
        missing_artifact_filenames=missing_artifact_filenames,
    )


def _resolve_repo_file_path(
    *,
    repo_root: Path | None,
    repo_relative_path: Path,
    require_directory: bool = False,
) -> Path | None:
    candidates: list[Path] = []
    if repo_root is not None:
        candidates.append(repo_root / repo_relative_path)
    else:
        module_path = Path(__file__).resolve()
        for parent in module_path.parents:
            if not (parent / ".git").exists():
                continue
            candidates.append(parent / repo_relative_path)
            break
    for candidate in candidates:
        if require_directory:
            if candidate.exists() and candidate.is_dir():
                return candidate
            continue
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _coerce_string_list(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    values: list[str] = []
    for item in raw:
        value = str(item).strip()
        if value:
            values.append(value)
    return tuple(values)


def _parse_artifact_kind_values(path: Path) -> set[str]:
    values: set[str] = set()
    try:
        module = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return values
    for node in module.body:
        if not isinstance(node, ast.ClassDef) or node.name != "ArtifactKind":
            continue
        for class_item in node.body:
            if not isinstance(class_item, ast.Assign):
                continue
            constant = class_item.value
            if isinstance(constant, ast.Constant) and isinstance(constant.value, str):
                values.add(constant.value)
    return values


def _parse_evidence_artifact_filenames(path: Path) -> set[str]:
    filenames: set[str] = set()
    try:
        module = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return filenames

    for node in ast.walk(module):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name) or not target.id.endswith("_path"):
                    continue
                value = node.value
                if (
                    isinstance(value, ast.BinOp)
                    and isinstance(value.op, ast.Div)
                    and isinstance(value.right, ast.Constant)
                    and isinstance(value.right.value, str)
                ):
                    filenames.add(value.right.value)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "capture_screenshot":
                continue
            for keyword in node.keywords:
                if keyword.arg != "output_name":
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    filenames.add(value.value)
    return filenames
