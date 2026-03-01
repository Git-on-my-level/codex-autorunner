from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.report_retention import prune_report_directory
from codex_autorunner.server import create_hub_app

runner = CliRunner()
LATEST_PARITY_REPORT = "latest-cross-surface-parity.md"
MAX_REPORT_CHARS = 8000


@dataclass(frozen=True)
class ParityCheck:
    entrypoint: str
    primitive: str
    passed: bool
    details: str = ""


def _contains_all(text: str, *snippets: str) -> bool:
    return all(snippet in text for snippet in snippets)


def _write_parity_report(*, repo_root: Path, checks: list[ParityCheck]) -> Path:
    lines: list[str] = [
        "# Cross-Surface Parity Report",
        "",
        "This report checks whether CAR primitives are exposed consistently across execution surfaces.",
        "",
        "## Checks",
        "",
    ]
    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        details = check.details.strip()
        if details:
            lines.append(
                f"- [{status}] `{check.entrypoint}` / `{check.primitive}`: {details}"
            )
        else:
            lines.append(f"- [{status}] `{check.entrypoint}` / `{check.primitive}`")

    text = "\n".join(lines).strip() + "\n"
    if len(text) > MAX_REPORT_CHARS:
        text = text[: MAX_REPORT_CHARS - 16] + "\n\n[...TRUNCATED]\n"

    report_dir = repo_root / ".codex-autorunner" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / LATEST_PARITY_REPORT
    report_path.write_text(text, encoding="utf-8")
    prune_report_directory(report_dir)
    return report_path


def test_cross_surface_parity_report(hub_env) -> None:
    repo_root = hub_env.repo_root
    checks: list[ParityCheck] = []

    describe_result = runner.invoke(
        app, ["describe", "--repo", str(repo_root), "--json"]
    )
    cli_describe_ok = describe_result.exit_code == 0
    checks.append(
        ParityCheck(
            entrypoint="cli",
            primitive="describe",
            passed=cli_describe_ok,
            details=f"exit_code={describe_result.exit_code}",
        )
    )

    describe_payload = {}
    if cli_describe_ok:
        describe_payload = json.loads(describe_result.output)

    context_feature_ok = bool(
        isinstance(describe_payload.get("features"), dict)
        and describe_payload["features"].get("ticket_frontmatter_context_includes")
        is True
    )
    checks.append(
        ParityCheck(
            entrypoint="cli",
            primitive="ticket_context_frontmatter",
            passed=context_feature_ok,
            details="feature flag exposed in car describe",
        )
    )

    template_apply_ok = bool(
        isinstance(describe_payload.get("templates"), dict)
        and "car templates apply <repo_id>:<path>[@<ref>]"
        in (
            describe_payload["templates"].get("commands", {}).get("apply", [])
            if isinstance(describe_payload["templates"].get("commands"), dict)
            else []
        )
    )
    checks.append(
        ParityCheck(
            entrypoint="cli",
            primitive="templates_apply",
            passed=template_apply_ok,
            details="apply command surfaced in describe output",
        )
    )

    app_instance = create_hub_app(hub_env.hub_root)
    with TestClient(app_instance) as client:
        base = f"/repos/{hub_env.repo_id}"
        web_describe = client.get(f"{base}/system/describe")
        web_describe_ok = web_describe.status_code == 200 and isinstance(
            web_describe.json().get("schema_id"), str
        )
        checks.append(
            ParityCheck(
                entrypoint="web",
                primitive="describe",
                passed=web_describe_ok,
                details=f"status={web_describe.status_code}",
            )
        )

        openapi = client.get(f"{base}/openapi.json").json()
        paths = set(openapi.get("paths", {}).keys())

        web_templates_ok = {
            "/api/templates/repos",
            "/api/templates/fetch",
            "/api/templates/apply",
        }.issubset(paths)
        checks.append(
            ParityCheck(
                entrypoint="web",
                primitive="templates_list_show_apply",
                passed=web_templates_ok,
                details="template routes present in OpenAPI",
            )
        )

        web_ticket_flow_ok = "/api/flows/ticket_flow/tickets" in paths
        checks.append(
            ParityCheck(
                entrypoint="web",
                primitive="ticket_flow_execution",
                passed=web_ticket_flow_ok,
                details="ticket_flow tickets API present",
            )
        )

        web_openapi_describe_ok = "/system/describe" in paths
        checks.append(
            ParityCheck(
                entrypoint="web",
                primitive="describe_route_registered",
                passed=web_openapi_describe_ok,
                details="/system/describe exposed in OpenAPI",
            )
        )

    runtime_path = Path(
        "src/codex_autorunner/integrations/telegram/handlers/commands_runtime.py"
    )
    spec_path = Path(
        "src/codex_autorunner/integrations/telegram/handlers/commands_spec.py"
    )
    trigger_mode_path = Path(
        "src/codex_autorunner/integrations/telegram/trigger_mode.py"
    )
    telegram_messages_path = Path(
        "src/codex_autorunner/integrations/telegram/handlers/messages.py"
    )
    runtime_text = runtime_path.read_text(encoding="utf-8")
    spec_text = spec_path.read_text(encoding="utf-8")
    trigger_mode_text = trigger_mode_path.read_text(encoding="utf-8")
    telegram_messages_text = telegram_messages_path.read_text(encoding="utf-8")

    telegram_shell_passthrough = "def _handle_bang_shell(" in runtime_text
    checks.append(
        ParityCheck(
            entrypoint="telegram",
            primitive="describe_templates_via_cli_passthrough",
            passed=telegram_shell_passthrough,
            details="available via !<shell command> when telegram_bot.shell.enabled=true",
        )
    )

    telegram_ticket_flow = '"flow": CommandSpec(' in spec_text
    checks.append(
        ParityCheck(
            entrypoint="telegram",
            primitive="ticket_flow_execution",
            passed=telegram_ticket_flow,
            details="/flow command registered",
        )
    )

    telegram_pma_command = "/pma" in runtime_text or "_handle_pma" in runtime_text
    checks.append(
        ParityCheck(
            entrypoint="telegram",
            primitive="pma_command",
            passed=telegram_pma_command,
            details="/pma command available",
        )
    )

    telegram_mentions_path = _contains_all(
        telegram_messages_text,
        "from ..trigger_mode import should_trigger_run",
        "should_trigger_run(",
    )
    checks.append(
        ParityCheck(
            entrypoint="telegram",
            primitive="mentions_trigger_path",
            passed=telegram_mentions_path,
            details="message trigger path routes mentions mode through trigger_mode",
        )
    )

    telegram_shared_turn_policy = _contains_all(
        trigger_mode_text,
        "should_trigger_plain_text_turn(",
        "PlainTextTurnContext(",
        'mode="mentions"',
    )
    checks.append(
        ParityCheck(
            entrypoint="telegram",
            primitive="shared_turn_policy",
            passed=telegram_shared_turn_policy,
            details="mentions trigger delegates to shared chat turn policy",
        )
    )

    discord_commands_path = Path(
        "src/codex_autorunner/integrations/discord/commands.py"
    )
    discord_service_path = Path("src/codex_autorunner/integrations/discord/service.py")
    discord_commands_text = (
        discord_commands_path.read_text(encoding="utf-8")
        if discord_commands_path.exists()
        else ""
    )
    discord_service_text = (
        discord_service_path.read_text(encoding="utf-8")
        if discord_service_path.exists()
        else ""
    )

    discord_pma_registered = (
        '"pma"' in discord_commands_text or "'pma'" in discord_commands_text
    )
    checks.append(
        ParityCheck(
            entrypoint="discord",
            primitive="pma_command_registration",
            passed=discord_pma_registered,
            details="/pma slash command registered in command tree",
        )
    )

    discord_pma_handler = "_handle_pma_command" in discord_service_text
    checks.append(
        ParityCheck(
            entrypoint="discord",
            primitive="pma_command_handler",
            passed=discord_pma_handler,
            details="PMA command handler implemented in service",
        )
    )

    discord_car_agent_support = _contains_all(
        discord_commands_text,
        '"name": "agent"',
        '"description": "View or set the agent"',
    ) and _contains_all(
        discord_service_text,
        'if command_path == ("car", "agent"):',
        "await self._handle_car_agent(",
    )
    checks.append(
        ParityCheck(
            entrypoint="discord",
            primitive="car_agent_support",
            passed=discord_car_agent_support,
            details="/car agent registered and routed to handler",
        )
    )

    discord_shared_command_ingress = (
        "integrations.chat.command_ingress import canonicalize_command_ingress"
        in discord_service_text
        and discord_service_text.count("canonicalize_command_ingress(") >= 2
    )
    checks.append(
        ParityCheck(
            entrypoint="discord",
            primitive="shared_command_ingress",
            passed=discord_shared_command_ingress,
            details="Discord interaction handling uses shared command ingress normalization",
        )
    )

    discord_shared_turn_policy = _contains_all(
        discord_service_text,
        "should_trigger_plain_text_turn(",
        "PlainTextTurnContext(",
        'mode="always"',
    )
    checks.append(
        ParityCheck(
            entrypoint="discord",
            primitive="shared_turn_policy",
            passed=discord_shared_turn_policy,
            details="direct-chat path uses shared plain-text turn policy",
        )
    )

    discord_shell_passthrough = _contains_all(
        discord_service_text,
        "def _handle_bang_shell(",
        'if text.startswith("!")',
    )
    checks.append(
        ParityCheck(
            entrypoint="discord",
            primitive="shell_passthrough",
            passed=discord_shell_passthrough,
            details="available via !<shell command> when discord_bot.shell.enabled=true",
        )
    )

    chat_doctor_text = Path(
        "src/codex_autorunner/integrations/chat/doctor.py"
    ).read_text(encoding="utf-8")
    cli_doctor_text = Path(
        "src/codex_autorunner/surfaces/cli/commands/doctor.py"
    ).read_text(encoding="utf-8")
    doctor_chat_parity_guardrail = _contains_all(
        chat_doctor_text,
        "chat.parity_contract",
        "run_parity_checks(",
    ) and _contains_all(cli_doctor_text, "chat_doctor_checks(", "--dev", "if dev")
    checks.append(
        ParityCheck(
            entrypoint="doctor",
            primitive="chat_parity_contract_guardrail",
            passed=doctor_chat_parity_guardrail,
            details="doctor gates chat parity contract checks behind --dev",
        )
    )

    report_path = _write_parity_report(repo_root=repo_root, checks=checks)

    assert report_path == (
        repo_root / ".codex-autorunner" / "reports" / LATEST_PARITY_REPORT
    )
    report_text = report_path.read_text(encoding="utf-8")
    assert "Cross-Surface Parity Report" in report_text
    assert len(report_text) <= MAX_REPORT_CHARS

    critical_failures = [
        check
        for check in checks
        if check.entrypoint in {"cli", "web", "telegram", "discord", "doctor"}
        and not check.passed
    ]
    assert not critical_failures
