"""Static source-inspection parity checks for chat command handling."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .command_contract import COMMAND_CONTRACT, CommandContractEntry

_DISCORD_SERVICE_PATH = Path("src/codex_autorunner/integrations/discord/service.py")
_TELEGRAM_TRIGGER_MODE_PATH = Path(
    "src/codex_autorunner/integrations/telegram/trigger_mode.py"
)
_TELEGRAM_MESSAGES_PATH = Path(
    "src/codex_autorunner/integrations/telegram/handlers/messages.py"
)


@dataclass(frozen=True)
class ParityCheckResult:
    id: str
    passed: bool
    message: str
    metadata: dict[str, Any]


def run_parity_checks(
    *,
    repo_root: Path | None = None,
    contract: Sequence[CommandContractEntry] = COMMAND_CONTRACT,
) -> tuple[ParityCheckResult, ...]:
    root = repo_root or _default_repo_root()
    discord_service_text = _read_text(root / _DISCORD_SERVICE_PATH)
    telegram_trigger_mode_text = _read_text(root / _TELEGRAM_TRIGGER_MODE_PATH)
    telegram_messages_text = _read_text(root / _TELEGRAM_MESSAGES_PATH)

    return (
        _check_discord_contract_commands_routed(
            contract=contract,
            discord_service_text=discord_service_text,
        ),
        _check_discord_known_commands_not_in_generic_fallback(
            discord_service_text=discord_service_text,
        ),
        _check_discord_canonicalize_command_ingress_usage(
            discord_service_text=discord_service_text,
        ),
        _check_shared_plain_text_turn_policy_usage(
            discord_service_text=discord_service_text,
            telegram_trigger_mode_text=telegram_trigger_mode_text,
            telegram_messages_text=telegram_messages_text,
        ),
    )


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _check_discord_contract_commands_routed(
    *,
    contract: Sequence[CommandContractEntry],
    discord_service_text: str,
) -> ParityCheckResult:
    missing_ids: list[str] = []

    for entry in contract:
        if _is_command_routed_in_discord_service(entry, discord_service_text):
            continue
        missing_ids.append(entry.id)

    passed = not missing_ids
    if passed:
        message = "All contract commands are routed in Discord command handling."
    else:
        message = "Missing Discord route handling for one or more contract commands."

    return ParityCheckResult(
        id="discord.contract_commands_routed",
        passed=passed,
        message=message,
        metadata={
            "expected_ids": [entry.id for entry in contract],
            "missing_ids": missing_ids,
        },
    )


def _is_command_routed_in_discord_service(
    entry: CommandContractEntry,
    discord_service_text: str,
) -> bool:
    prefix = entry.path[0] if entry.path else ""

    if prefix == "car":
        pattern = (
            r"(?:if|elif)\s+command_path\s*==\s*"
            + _tuple_literal_pattern(entry.path)
            + r"\s*:"
        )
        return re.search(pattern, discord_service_text) is not None

    if prefix == "pma":
        subcommand = entry.path[1] if len(entry.path) > 1 else ""
        pattern = (
            r'(?:if|elif)\s+subcommand\s*==\s*"' + re.escape(subcommand) + r'"\s*:'
        )
        return len(re.findall(pattern, discord_service_text)) >= 2

    return False


def _check_discord_known_commands_not_in_generic_fallback(
    *,
    discord_service_text: str,
) -> ParityCheckResult:
    checks = {
        "normalized_car_prefix_guard": (
            'if ingress is not None and ingress.command_path[:1] == ("car",):'
            in discord_service_text
        ),
        "normalized_pma_prefix_guard": (
            'elif ingress is not None and ingress.command_path[:1] == ("pma",):'
            in discord_service_text
        ),
        "interaction_car_prefix_guard": (
            'if ingress.command_path[:1] == ("car",):' in discord_service_text
        ),
        "interaction_pma_prefix_guard": (
            'if ingress.command_path[:1] == ("pma",):' in discord_service_text
        ),
        "generic_fallback_present": (
            "Command not implemented yet for Discord." in discord_service_text
        ),
        "car_specific_fallback_present": (
            "Unknown car subcommand:" in discord_service_text
        ),
        "pma_specific_fallback_present": (
            "Unknown PMA subcommand. Use on, off, or status." in discord_service_text
        ),
    }

    failed_predicates = [key for key, passed in checks.items() if not passed]
    passed = not failed_predicates

    if passed:
        message = "Known Discord command prefixes are guarded before generic fallback."
    else:
        message = "Discord fallback guard structure is incomplete for known commands."

    return ParityCheckResult(
        id="discord.no_generic_fallback_leak",
        passed=passed,
        message=message,
        metadata={
            "failed_predicates": failed_predicates,
            "predicates": checks,
        },
    )


def _check_discord_canonicalize_command_ingress_usage(
    *,
    discord_service_text: str,
) -> ParityCheckResult:
    checks = {
        "import_present": (
            "integrations.chat.command_ingress import canonicalize_command_ingress"
            in discord_service_text
        ),
        "normalized_interaction_call_present": bool(
            re.search(
                (
                    r"canonicalize_command_ingress\(\s*"
                    r"command=payload_data\.get\(\"command\"\)\s*,\s*"
                    r"options=payload_data\.get\(\"options\"\)\s*,?\s*\)"
                ),
                discord_service_text,
            )
        ),
        "interaction_call_present": bool(
            re.search(
                (
                    r"canonicalize_command_ingress\(\s*"
                    r"command_path=command_path\s*,\s*"
                    r"options=options\s*,?\s*\)"
                ),
                discord_service_text,
            )
        ),
    }

    failed_predicates = [key for key, passed in checks.items() if not passed]
    passed = not failed_predicates

    if passed:
        message = "Discord ingress paths use shared canonical command normalization."
    else:
        message = "Discord canonical command normalization is missing in expected ingress paths."

    return ParityCheckResult(
        id="discord.canonical_command_ingress_usage",
        passed=passed,
        message=message,
        metadata={
            "failed_predicates": failed_predicates,
            "predicates": checks,
        },
    )


def _check_shared_plain_text_turn_policy_usage(
    *,
    discord_service_text: str,
    telegram_trigger_mode_text: str,
    telegram_messages_text: str,
) -> ParityCheckResult:
    checks = {
        "discord_shared_policy_call": _contains_all(
            discord_service_text,
            "should_trigger_plain_text_turn(",
            "PlainTextTurnContext(",
            'mode="always"',
        ),
        "telegram_shared_policy_call": _contains_all(
            telegram_trigger_mode_text,
            "should_trigger_plain_text_turn(",
            "PlainTextTurnContext(",
            'mode="mentions"',
        ),
        "telegram_trigger_bridge": _contains_all(
            telegram_messages_text,
            "from ..trigger_mode import should_trigger_run",
            "should_trigger_run(",
        ),
    }

    failed_predicates = [key for key, passed in checks.items() if not passed]
    passed = not failed_predicates

    if passed:
        message = (
            "Telegram and Discord trigger paths use the shared plain-text turn policy."
        )
    else:
        message = "Shared plain-text turn policy usage is missing in Telegram/Discord trigger paths."

    return ParityCheckResult(
        id="chat.shared_plain_text_turn_policy_usage",
        passed=passed,
        message=message,
        metadata={
            "failed_predicates": failed_predicates,
            "predicates": checks,
        },
    )


def _contains_all(text: str, *snippets: str) -> bool:
    return all(snippet in text for snippet in snippets)


def _tuple_literal_pattern(path: tuple[str, ...]) -> str:
    parts = [r'"' + re.escape(part) + r'"' for part in path]
    if len(parts) == 1:
        return r"\(\s*" + parts[0] + r"\s*,\s*\)"
    return r"\(\s*" + r"\s*,\s*".join(parts) + r"\s*\)"
