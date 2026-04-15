"""Chat integration doctor checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.runtime import DoctorCheck
from .chat_ux_telemetry import get_global_accumulator
from .parity_checker import ParityCheckResult, run_parity_checks

_CHECK_GROUP = "chat.parity_contract"
_DIAGNOSTICS_GROUP = "chat.ux_timing_diagnostics"
_DISCORD_SERVICE = "src/codex_autorunner/integrations/discord/service.py"
_TELEGRAM_TRIGGER_MODE = "src/codex_autorunner/integrations/telegram/trigger_mode.py"
_TELEGRAM_MESSAGES = "src/codex_autorunner/integrations/telegram/handlers/messages.py"


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
