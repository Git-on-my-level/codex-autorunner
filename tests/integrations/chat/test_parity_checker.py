from __future__ import annotations

from pathlib import Path

from codex_autorunner.integrations.chat import parity_checker
from codex_autorunner.integrations.chat.command_contract import (
    COMMAND_CONTRACT,
    CommandContractEntry,
)
from codex_autorunner.integrations.chat.parity_checker import run_parity_checks


def test_parity_checker_passes_on_current_repo_layout() -> None:
    results = run_parity_checks()
    failures = [result for result in results if not result.passed]

    assert failures == []


def test_parity_checker_fails_when_registry_command_is_uncataloged() -> None:
    contract = tuple(entry for entry in COMMAND_CONTRACT if entry.id != "car.bind")
    results_by_id = {
        result.id: result for result in run_parity_checks(contract=contract)
    }

    coverage_check = results_by_id["contract.registry_entries_cataloged"]
    assert not coverage_check.passed
    assert "car:bind" in coverage_check.metadata["missing_discord_paths"]
    assert "bind" in coverage_check.metadata["missing_telegram_commands"]


def test_parity_checker_reports_non_stable_route_gaps_informationally(
    tmp_path: Path,
) -> None:
    repo_root = _write_fixture_repo(tmp_path)
    contract = (
        CommandContractEntry(
            id="car.future.partial",
            path=("car", "future"),
            requires_bound_workspace=False,
            status="partial",
            discord_paths=(("car", "future"),),
            telegram_commands=("future",),
        ),
    )
    results_by_id = {
        result.id: result
        for result in run_parity_checks(
            repo_root=repo_root,
            contract=contract,
        )
    }

    route_check = results_by_id["discord.contract_commands_routed"]
    assert route_check.passed
    assert "car.future.partial" in route_check.metadata["missing_non_stable_ids"]


def test_parity_checker_fails_when_contract_route_is_missing(tmp_path: Path) -> None:
    repo_root = _write_fixture_repo(tmp_path, include_car_model_route=False)

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    route_check = results_by_id["discord.contract_commands_routed"]
    assert not route_check.passed
    assert "car.model" in route_check.metadata["missing_ids"]


def test_parity_checker_fails_when_update_route_is_missing(tmp_path: Path) -> None:
    repo_root = _write_fixture_repo(tmp_path, include_car_update_route=False)

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    route_check = results_by_id["discord.contract_commands_routed"]
    assert not route_check.passed
    assert "car.update" in route_check.metadata["missing_ids"]


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


def test_parity_checker_fails_when_component_fallback_is_missing(
    tmp_path: Path,
) -> None:
    repo_root = _write_fixture_repo(
        tmp_path,
        include_component_unknown_fallback=False,
    )

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    guard_check = results_by_id["discord.interaction_component_guard_paths"]
    assert not guard_check.passed
    assert "component_unknown_fallback" in guard_check.metadata["failed_predicates"]


def test_parity_checker_fails_when_parse_error_response_is_missing(
    tmp_path: Path,
) -> None:
    repo_root = _write_fixture_repo(
        tmp_path,
        include_interaction_parse_failure_response=False,
    )

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    guard_check = results_by_id["discord.interaction_component_guard_paths"]
    assert not guard_check.passed
    assert (
        "interaction_parse_failure_response"
        in guard_check.metadata["failed_predicates"]
    )


def test_parity_checker_fails_when_bind_component_value_guard_missing(
    tmp_path: Path,
) -> None:
    repo_root = _write_fixture_repo(
        tmp_path,
        include_component_bind_value_response=False,
    )

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    guard_check = results_by_id["discord.interaction_component_guard_paths"]
    assert not guard_check.passed
    assert (
        "component_bind_selection_requires_value"
        in guard_check.metadata["failed_predicates"]
    )


def test_parity_checker_fails_when_flow_runs_component_value_guard_missing(
    tmp_path: Path,
) -> None:
    repo_root = _write_fixture_repo(
        tmp_path,
        include_component_flow_runs_value_response=False,
    )

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    guard_check = results_by_id["discord.interaction_component_guard_paths"]
    assert not guard_check.passed
    assert (
        "component_flow_runs_selection_requires_value"
        in guard_check.metadata["failed_predicates"]
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


def test_parity_checker_fails_when_direct_pma_route_branch_is_missing(
    tmp_path: Path,
) -> None:
    repo_root = _write_fixture_repo(
        tmp_path,
        include_pma_status_route_in_direct=False,
    )

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    route_check = results_by_id["discord.contract_commands_routed"]
    assert not route_check.passed
    assert "pma.status" in route_check.metadata["missing_ids"]


def test_parity_checker_accepts_equivalent_canonicalize_and_guard_shapes(
    tmp_path: Path,
) -> None:
    repo_root = _write_fixture_repo(
        tmp_path,
        use_canonicalize_temporary_names=True,
        use_early_ingress_none_guard_in_normalized=True,
    )

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    assert results_by_id["discord.canonical_command_ingress_usage"].passed
    assert results_by_id["discord.no_generic_fallback_leak"].passed


def test_parity_checker_accepts_truthy_ingress_guard_and_named_context(
    tmp_path: Path,
) -> None:
    repo_root = _write_fixture_repo(
        tmp_path,
        use_truthy_ingress_guard_in_normalized=True,
        use_named_plain_text_context=True,
    )

    results_by_id = {
        result.id: result for result in run_parity_checks(repo_root=repo_root)
    }

    assert results_by_id["discord.no_generic_fallback_leak"].passed
    assert results_by_id["chat.shared_plain_text_turn_policy_usage"].passed


def test_parity_checker_skips_when_source_files_are_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        parity_checker,
        "_resolve_source_path",
        lambda **_kwargs: None,
    )
    results = run_parity_checks(repo_root=None)
    assert all(result.passed for result in results)
    assert all(result.metadata.get("skipped") is True for result in results)


def _write_fixture_repo(
    root: Path,
    *,
    include_car_model_route: bool = True,
    include_car_update_route: bool = True,
    include_interaction_pma_prefix_guard: bool = True,
    include_canonicalize_usage: bool = True,
    include_discord_turn_policy: bool = True,
    include_telegram_turn_policy: bool = True,
    include_telegram_trigger_bridge: bool = True,
    include_pma_status_route_in_normalized: bool = True,
    include_pma_status_route_in_direct: bool = True,
    include_component_unknown_fallback: bool = True,
    include_interaction_parse_failure_response: bool = True,
    include_component_missing_custom_id_response: bool = True,
    include_component_bind_value_response: bool = True,
    include_component_flow_runs_value_response: bool = True,
    use_canonicalize_temporary_names: bool = False,
    use_early_ingress_none_guard_in_normalized: bool = False,
    use_truthy_ingress_guard_in_normalized: bool = False,
    use_named_plain_text_context: bool = False,
) -> Path:
    discord_service = _build_discord_service_fixture(
        include_car_model_route=include_car_model_route,
        include_car_update_route=include_car_update_route,
        include_interaction_pma_prefix_guard=include_interaction_pma_prefix_guard,
        include_canonicalize_usage=include_canonicalize_usage,
        include_discord_turn_policy=include_discord_turn_policy,
        include_pma_status_route_in_normalized=include_pma_status_route_in_normalized,
        include_pma_status_route_in_direct=include_pma_status_route_in_direct,
        include_component_unknown_fallback=include_component_unknown_fallback,
        include_interaction_parse_failure_response=include_interaction_parse_failure_response,
        include_component_missing_custom_id_response=include_component_missing_custom_id_response,
        include_component_bind_value_response=include_component_bind_value_response,
        include_component_flow_runs_value_response=include_component_flow_runs_value_response,
        use_canonicalize_temporary_names=use_canonicalize_temporary_names,
        use_early_ingress_none_guard_in_normalized=use_early_ingress_none_guard_in_normalized,
        use_truthy_ingress_guard_in_normalized=use_truthy_ingress_guard_in_normalized,
    )
    telegram_trigger_mode = _build_telegram_trigger_mode_fixture(
        include_telegram_turn_policy=include_telegram_turn_policy,
        use_named_plain_text_context=use_named_plain_text_context,
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
    include_car_update_route: bool,
    include_interaction_pma_prefix_guard: bool,
    include_canonicalize_usage: bool,
    include_discord_turn_policy: bool,
    include_pma_status_route_in_normalized: bool,
    include_pma_status_route_in_direct: bool,
    include_component_unknown_fallback: bool,
    include_interaction_parse_failure_response: bool,
    include_component_missing_custom_id_response: bool,
    include_component_bind_value_response: bool,
    include_component_flow_runs_value_response: bool,
    use_canonicalize_temporary_names: bool,
    use_early_ingress_none_guard_in_normalized: bool,
    use_truthy_ingress_guard_in_normalized: bool,
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
        if include_canonicalize_usage and not use_canonicalize_temporary_names
        else (
            """
    command_payload = payload_data.get("command")
    options_payload = payload_data.get("options")
    ingress = canonicalize_command_ingress(
        command=command_payload,
        options=options_payload,
    )
"""
            if include_canonicalize_usage
            else "\n    ingress = None\n"
        )
    )

    interaction_ingress = (
        """
    ingress = canonicalize_command_ingress(
        command_path=command_path,
        options=options,
    )
"""
        if include_canonicalize_usage and not use_canonicalize_temporary_names
        else (
            """
    path_payload = command_path
    options_payload = options
    ingress = canonicalize_command_ingress(
        command_path=path_payload,
        options=options_payload,
    )
"""
            if include_canonicalize_usage
            else "\n    ingress = None\n"
        )
    )

    normalized_prefix_guard = (
        """
        if ingress is not None and ingress.command_path[:1] == ("car",):
            return
        elif ingress is not None and ingress.command_path[:1] == ("pma",):
            return
"""
        if not use_early_ingress_none_guard_in_normalized
        else """
        if ingress is None:
            return
        if ingress.command_path[:1] == ("car",):
            return
        elif ingress.command_path[:1] == ("pma",):
            return
"""
    )
    if use_truthy_ingress_guard_in_normalized:
        normalized_prefix_guard = """
        if ingress and ingress.command_path[:1] == ("car",):
            return
        elif ingress and ingress.command_path[:1] == ("pma",):
            return
"""

    interaction_pma_guard = (
        '        if ingress.command_path[:1] == ("pma",):\n            return\n'
        if include_interaction_pma_prefix_guard
        else ""
    )

    car_model_route = (
        '    if command_path == ("car", "model"):\n        return\n'
        if include_car_model_route
        else ""
    )
    car_update_route = (
        '    if command_path == ("car", "update"):\n        return\n'
        if include_car_update_route
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
    direct_pma_status_branch = (
        """
    elif subcommand == "status":
        return
"""
        if include_pma_status_route_in_direct
        else ""
    )
    component_unknown_fallback = (
        """
        _respond_ephemeral(
            "interaction-id",
            "interaction-token",
            f"Unknown component: {custom_id}",
        )
"""
        if include_component_unknown_fallback
        else ""
    )
    interaction_parse_failure_response = (
        """
        _respond_ephemeral(
            "interaction-id",
            "interaction-token",
            "I could not parse this interaction. Please retry the command.",
        )
"""
        if include_interaction_parse_failure_response
        else ""
    )
    component_missing_custom_id_response = (
        """
            _respond_ephemeral(
                "interaction-id",
                "interaction-token",
                "I could not identify this interaction action. Please retry.",
            )
"""
        if include_component_missing_custom_id_response
        else ""
    )
    component_bind_value_response = (
        """
                _respond_ephemeral(
                    "interaction-id",
                    "interaction-token",
                    "Please select a repository and try again.",
                )
"""
        if include_component_bind_value_response
        else ""
    )
    component_flow_runs_value_response = (
        """
                _respond_ephemeral(
                    "interaction-id",
                    "interaction-token",
                    "Please select a run and try again.",
                )
"""
        if include_component_flow_runs_value_response
        else ""
    )

    return (
        "from ...integrations.chat.turn_policy import PlainTextTurnContext, should_trigger_plain_text_turn\n"
        + import_line
        + """

def _handle_normalized_interaction(payload_data: dict[str, object]) -> None:
    interaction_id = "interaction-id"
    interaction_token = "interaction-token"

    if payload_data.get("type") == "component":
        custom_id = payload_data.get("component_id")
        if not custom_id:
"""
        + component_missing_custom_id_response
        + """
            return
        _handle_component_interaction_normalized(custom_id, payload_data.get("values"))
        return
"""
        + normalized_ingress
        + """
    if ingress is None:
"""
        + interaction_parse_failure_response
        + """
        return

    try:
"""
        + normalized_prefix_guard
        + """
        _respond_ephemeral(
            "interaction-id",
            "interaction-token",
            "Command not implemented yet for Discord.",
        )
    except Exception as exc:
        log_event(
            logger,
            40,
            "discord.interaction.unhandled_error",
            exc=exc,
        )
        _respond_ephemeral(
            "interaction-id",
            "interaction-token",
            "An unexpected error occurred. Please try again later.",
        )


def _handle_interaction(command_path: tuple[str, ...], options: dict[str, object]) -> None:
"""
        + interaction_ingress
        + """
    if ingress is None:
"""
        + interaction_parse_failure_response
        + """
        return

    try:
        if ingress.command_path[:1] == ("car",):
            return
"""
        + interaction_pma_guard
        + """
        _respond_ephemeral(
            "interaction-id",
            "interaction-token",
            "Command not implemented yet for Discord.",
        )
    except Exception as exc:
        log_event(
            logger,
            40,
            "discord.interaction.unhandled_error",
            exc=exc,
        )
        _respond_ephemeral(
            "interaction-id",
            "interaction-token",
            "An unexpected error occurred. Please try again later.",
        )


def _handle_component_interaction_normalized(custom_id: str, values: list[str] | None) -> None:
    try:
        if custom_id == "bind_select":
            if not values:
"""
        + component_bind_value_response
        + """
                return
            return
        if custom_id == "flow_runs_select":
            values = []
            if not values:
"""
        + component_flow_runs_value_response
        + """
                return
            return
        if custom_id.startswith("flow:"):
            return
"""
        + component_unknown_fallback
        + """
    except Exception as exc:
        log_event(
            logger,
            40,
            "discord.component.normalized.unhandled_error",
            exc=exc,
        )
        _respond_ephemeral(
            "interaction-id",
            "interaction-token",
            "An unexpected error occurred. Please try again later.",
        )


def _handle_car_command(command_path: tuple[str, ...]) -> None:
    if command_path == ("car", "status"):
        return
    if command_path == ("car", "new"):
        return
    if command_path == ("car", "agent"):
        return
"""
        + car_model_route
        + car_update_route
        + """
    _ = "Unknown car subcommand: x"


def _handle_pma_command(command_path: tuple[str, ...]) -> None:
    subcommand = command_path[1] if len(command_path) > 1 else "status"
    if subcommand == "on":
        return
    elif subcommand == "off":
        return
"""
        + direct_pma_status_branch
        + """
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


def _build_telegram_trigger_mode_fixture(
    *,
    include_telegram_turn_policy: bool,
    use_named_plain_text_context: bool,
) -> str:
    if include_telegram_turn_policy:
        if use_named_plain_text_context:
            return """from ..chat.turn_policy import PlainTextTurnContext, should_trigger_plain_text_turn


def should_trigger_run(message, *, text: str, bot_username: str | None) -> bool:
    context = PlainTextTurnContext(
        text=text,
        chat_type=message.chat_type,
        bot_username=bot_username,
    )
    return should_trigger_plain_text_turn(
        mode=\"mentions\",
        context=context,
    )
"""
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
