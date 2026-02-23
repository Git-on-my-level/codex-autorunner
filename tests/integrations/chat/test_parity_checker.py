from __future__ import annotations

from pathlib import Path

from codex_autorunner.integrations.chat.parity_checker import run_parity_checks


def test_parity_checker_passes_on_current_repo_layout() -> None:
    results = run_parity_checks()
    failures = [result for result in results if not result.passed]

    assert failures == []


def test_parity_checker_fails_when_contract_route_is_missing(tmp_path: Path) -> None:
    repo_root = _write_fixture_repo(tmp_path, include_car_model_route=False)

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    route_check = results_by_id["discord.contract_commands_routed"]
    assert not route_check.passed
    assert "car.model" in route_check.metadata["missing_ids"]


def test_parity_checker_fails_when_known_prefix_can_leak_to_generic_fallback(
    tmp_path: Path,
) -> None:
    repo_root = _write_fixture_repo(
        tmp_path, include_interaction_pma_prefix_guard=False
    )

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    fallback_check = results_by_id["discord.no_generic_fallback_leak"]
    assert not fallback_check.passed
    assert (
        "interaction_pma_prefix_guard" in fallback_check.metadata["failed_predicates"]
    )


def test_parity_checker_fails_when_shared_helper_usage_is_missing(
    tmp_path: Path,
) -> None:
    repo_root = _write_fixture_repo(
        tmp_path,
        include_canonicalize_usage=False,
        include_discord_turn_policy=False,
        include_telegram_turn_policy=False,
    )

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    ingress_check = results_by_id["discord.canonical_command_ingress_usage"]
    turn_policy_check = results_by_id["chat.shared_plain_text_turn_policy_usage"]

    assert not ingress_check.passed
    assert not turn_policy_check.passed


def test_parity_checker_fails_when_telegram_trigger_bridge_is_missing(
    tmp_path: Path,
) -> None:
    repo_root = _write_fixture_repo(tmp_path, include_telegram_trigger_bridge=False)

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    turn_policy_check = results_by_id["chat.shared_plain_text_turn_policy_usage"]
    assert not turn_policy_check.passed
    assert "telegram_trigger_bridge" in turn_policy_check.metadata["failed_predicates"]


def test_parity_checker_fails_when_pma_route_branch_is_missing(tmp_path: Path) -> None:
    repo_root = _write_fixture_repo(
        tmp_path,
        include_pma_status_route_in_normalized=False,
    )

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    route_check = results_by_id["discord.contract_commands_routed"]
    assert not route_check.passed
    assert "pma.status" in route_check.metadata["missing_ids"]


def _write_fixture_repo(
    root: Path,
    *,
    include_car_model_route: bool = True,
    include_interaction_pma_prefix_guard: bool = True,
    include_canonicalize_usage: bool = True,
    include_discord_turn_policy: bool = True,
    include_telegram_turn_policy: bool = True,
    include_telegram_trigger_bridge: bool = True,
    include_pma_status_route_in_normalized: bool = True,
) -> Path:
    discord_service = _build_discord_service_fixture(
        include_car_model_route=include_car_model_route,
        include_interaction_pma_prefix_guard=include_interaction_pma_prefix_guard,
        include_canonicalize_usage=include_canonicalize_usage,
        include_discord_turn_policy=include_discord_turn_policy,
        include_pma_status_route_in_normalized=include_pma_status_route_in_normalized,
    )
    telegram_trigger_mode = _build_telegram_trigger_mode_fixture(
        include_telegram_turn_policy=include_telegram_turn_policy,
    )
    telegram_messages = _build_telegram_messages_fixture(
        include_telegram_trigger_bridge=include_telegram_trigger_bridge,
    )

    _write_text(
        root / "src/codex_autorunner/integrations/discord/service.py",
        discord_service,
    )
    _write_text(
        root / "src/codex_autorunner/integrations/telegram/trigger_mode.py",
        telegram_trigger_mode,
    )
    _write_text(
        root / "src/codex_autorunner/integrations/telegram/handlers/messages.py",
        telegram_messages,
    )

    return root


def _build_discord_service_fixture(
    *,
    include_car_model_route: bool,
    include_interaction_pma_prefix_guard: bool,
    include_canonicalize_usage: bool,
    include_discord_turn_policy: bool,
    include_pma_status_route_in_normalized: bool,
) -> str:
    import_line = (
        "from ...integrations.chat.command_ingress import canonicalize_command_ingress\n"
        if include_canonicalize_usage
        else ""
    )

    normalized_ingress = (
        """
    ingress = canonicalize_command_ingress(
        command=payload_data.get("command"),
        options=payload_data.get("options"),
    )
"""
        if include_canonicalize_usage
        else "\n    ingress = None\n"
    )

    interaction_ingress = (
        """
    ingress = canonicalize_command_ingress(
        command_path=command_path,
        options=options,
    )
"""
        if include_canonicalize_usage
        else "\n    ingress = None\n"
    )

    interaction_pma_guard = (
        '    if ingress.command_path[:1] == ("pma",):\n        return\n'
        if include_interaction_pma_prefix_guard
        else ""
    )

    car_model_route = (
        '    if command_path == ("car", "model"):\n        return\n'
        if include_car_model_route
        else ""
    )

    discord_turn_policy = (
        """

def _handle_message_event(text: str) -> None:
    if not should_trigger_plain_text_turn(
        mode="always",
        context=PlainTextTurnContext(text=text),
    ):
        return
"""
        if include_discord_turn_policy
        else ""
    )

    normalized_pma_status_branch = (
        """
    elif subcommand == "status":
        return
"""
        if include_pma_status_route_in_normalized
        else ""
    )

    return (
        "from ...integrations.chat.turn_policy import PlainTextTurnContext, should_trigger_plain_text_turn\n"
        + import_line
        + """

def _handle_normalized_interaction(payload_data: dict[str, object]) -> None:
"""
        + normalized_ingress
        + """
    if ingress is not None and ingress.command_path[:1] == ("car",):
        return
    elif ingress is not None and ingress.command_path[:1] == ("pma",):
        return
    _ = "Command not implemented yet for Discord."


def _handle_interaction(command_path: tuple[str, ...], options: dict[str, object]) -> None:
"""
        + interaction_ingress
        + """
    if ingress.command_path[:1] == ("car",):
        return
"""
        + interaction_pma_guard
        + """
    _ = "Command not implemented yet for Discord."


def _handle_car_command(command_path: tuple[str, ...]) -> None:
    if command_path == ("car", "status"):
        return
    if command_path == ("car", "agent"):
        return
"""
        + car_model_route
        + """
    _ = "Unknown car subcommand: x"


def _handle_pma_command(command_path: tuple[str, ...]) -> None:
    subcommand = command_path[1] if len(command_path) > 1 else "status"
    if subcommand == "on":
        return
    elif subcommand == "off":
        return
    elif subcommand == "status":
        return
    _ = "Unknown PMA subcommand. Use on, off, or status."


def _handle_pma_command_from_normalized(command: str) -> None:
    subcommand = command.split(":")[-1] if ":" in command else "status"
    if subcommand == "on":
        return
    elif subcommand == "off":
        return
"""
        + normalized_pma_status_branch
        + """
    _ = "Unknown PMA subcommand. Use on, off, or status."
"""
        + discord_turn_policy
    )


def _build_telegram_trigger_mode_fixture(*, include_telegram_turn_policy: bool) -> str:
    if include_telegram_turn_policy:
        return """from ..chat.turn_policy import PlainTextTurnContext, should_trigger_plain_text_turn


def should_trigger_run(message, *, text: str, bot_username: str | None) -> bool:
    return should_trigger_plain_text_turn(
        mode=\"mentions\",
        context=PlainTextTurnContext(
            text=text,
            chat_type=message.chat_type,
            bot_username=bot_username,
        ),
    )
"""

    return """def should_trigger_run(message, *, text: str, bot_username: str | None) -> bool:
    return bool(text)
"""


def _build_telegram_messages_fixture(*, include_telegram_trigger_bridge: bool) -> str:
    if include_telegram_trigger_bridge:
        return """from ..trigger_mode import should_trigger_run


def handle_message(message) -> None:
    if should_trigger_run(message, text=\"hi\", bot_username=None):
        return
"""

    return """def handle_message(message) -> None:
    text = getattr(message, "text", "")
    if text:
        return
"""


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
