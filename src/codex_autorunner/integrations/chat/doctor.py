"""Chat integration doctor checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.runtime import DoctorCheck
from .parity_checker import ParityCheckResult, run_parity_checks

_CHECK_GROUP = "chat.parity_contract"
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


def _failure_details(result: ParityCheckResult) -> tuple[str, str]:
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

    return (
        f"Chat parity contract failed: {result.message}",
        "Run parity checks and align command routing/guard helpers across chat surfaces.",
    )


def _metadata_list(metadata: dict[str, Any], key: str) -> list[str]:
    raw = metadata.get(key)
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]
