import logging

import typer

from ...core.config import load_repo_config
from ...core.hub import HubSupervisor as _HubSupervisor
from ...flows.ticket_flow import build_ticket_flow_definition
from ...integrations.agents.build_agent_pool import build_agent_pool
from .commands.cleanup import register_cleanup_commands
from .commands.describe import register_describe_commands
from .commands.discord import register_discord_commands
from .commands.dispatch import register_dispatch_commands
from .commands.doctor import (
    _find_hub_server_process,  # noqa: F401
    register_doctor_commands,
)
from .commands.flow import (  # noqa: F401
    _stale_terminal_runs,  # noqa: F401
    register_flow_commands,
)
from .commands.hub import register_hub_commands
from .commands.hub_runs import (
    _archive_flow_run_artifacts,
    _cleanup_stale_flow_runs,
    register_hub_runs_commands,
)
from .commands.hub_tickets import register_hub_tickets_commands
from .commands.inbox import register_inbox_commands
from .commands.protocol import register_protocol_commands
from .commands.repos import register_repos_commands
from .commands.root import _resolve_repo_api_path, register_root_commands  # noqa: F401
from .commands.telegram import register_telegram_commands
from .commands.templates import (
    register_template_index_commands,
    register_templates_commands,
)
from .commands.utils import (
    build_hub_supervisor as _build_hub_supervisor,
)
from .commands.utils import (
    build_server_url as _build_server_url,
)
from .commands.utils import (
    enforce_bind_auth as _enforce_bind_auth,
)
from .commands.utils import (
    fetch_template_with_scan as _fetch_template_with_scan,
)
from .commands.utils import (
    get_car_version,
    parse_bool_text,
    parse_duration,
)
from .commands.utils import (
    guard_unregistered_hub_repo as _guard_unregistered_hub_repo,
)
from .commands.utils import (
    normalize_base_path as _normalize_base_path,
)
from .commands.utils import (
    raise_exit as _raise_exit,
)
from .commands.utils import (
    request_form_json as _request_form_json,
)
from .commands.utils import (
    request_json as _request_json,
)
from .commands.utils import (
    require_hub_config as _require_hub_config,
)
from .commands.utils import (
    require_optional_feature as _require_optional_feature,
)
from .commands.utils import (
    require_repo_config as _require_repo_config,
)
from .commands.utils import (
    require_templates_enabled as _require_templates_enabled,
)
from .commands.utils import (
    resolve_hub_config_path_for_cli as _resolve_hub_config_path_for_cli,
)
from .commands.worktree import register_worktree_commands
from .pma_cli import pma_app as pma_cli_app

HubSupervisor = _HubSupervisor


logger = logging.getLogger("codex_autorunner.cli")

app = typer.Typer(add_completion=False)
hub_app = typer.Typer(add_completion=False)
dispatch_app = typer.Typer(add_completion=False)
inbox_app = typer.Typer(add_completion=False)
hub_runs_app = typer.Typer(add_completion=False)
telegram_app = typer.Typer(add_completion=False)
discord_app = typer.Typer(add_completion=False)
templates_app = typer.Typer(add_completion=False)
repos_app = typer.Typer(add_completion=False)
cleanup_app = typer.Typer(add_completion=False)
worktree_app = typer.Typer(add_completion=False)
hub_tickets_app = typer.Typer(add_completion=False)
doctor_app = typer.Typer(add_completion=False, invoke_without_command=True)
flow_app = typer.Typer(add_completion=False)
ticket_flow_app = typer.Typer(add_completion=False)


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(f"codex-autorunner {get_car_version()}")
    raise typer.Exit(code=0)


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    # Intentionally empty; subcommands implement behavior.
    #
    # `--version` is handled eagerly via `_version_callback`.
    return


def main() -> None:
    """Entrypoint for CLI execution."""
    app()


app.add_typer(hub_app, name="hub")
register_hub_commands(
    hub_app,
    require_hub_config=_require_hub_config,
    raise_exit=_raise_exit,
    build_supervisor=_build_hub_supervisor,
    enforce_bind_auth=_enforce_bind_auth,
    build_server_url=_build_server_url,
    request_json=_request_json,
    normalize_base_path=_normalize_base_path,
)
hub_app.add_typer(dispatch_app, name="dispatch")
register_dispatch_commands(
    dispatch_app,
    require_hub_config_func=_require_hub_config,
    build_server_url_func=_build_server_url,
    request_json_func=_request_json,
    request_form_json_func=_request_form_json,
)
hub_app.add_typer(inbox_app, name="inbox")
hub_app.add_typer(hub_runs_app, name="runs")
register_hub_runs_commands(
    hub_runs_app,
    require_hub_config=_require_hub_config,
    load_repo_config=load_repo_config,
    parse_bool_text_func=parse_bool_text,
    parse_duration_func=parse_duration,
)
hub_app.add_typer(worktree_app, name="worktree")
register_worktree_commands(
    worktree_app,
    require_hub_config=_require_hub_config,
    raise_exit=_raise_exit,
    build_supervisor=_build_hub_supervisor,
)
hub_app.add_typer(hub_tickets_app, name="tickets")
app.add_typer(telegram_app, name="telegram")
register_telegram_commands(
    telegram_app,
    raise_exit=_raise_exit,
    require_optional_feature=_require_optional_feature,
)
app.add_typer(discord_app, name="discord")
register_discord_commands(
    discord_app,
    raise_exit=_raise_exit,
    require_optional_feature=_require_optional_feature,
)
app.add_typer(templates_app, name="templates")
# UX alias: allow singular form (`car template ...`) in addition to `car templates ...`.
app.add_typer(templates_app, name="template")
app.add_typer(cleanup_app, name="cleanup")
register_templates_commands(
    templates_app,
    require_repo_config=_require_repo_config,
    require_templates_enabled=_require_templates_enabled,
    raise_exit=_raise_exit,
    resolve_hub_config_path_for_cli=_resolve_hub_config_path_for_cli,
)

register_template_index_commands(
    templates_app,
    require_repo_config=_require_repo_config,
    require_hub_config=_require_hub_config,
    raise_exit=_raise_exit,
    resolve_hub_config_path_for_cli=_resolve_hub_config_path_for_cli,
)
templates_app.add_typer(repos_app, name="repos")
register_repos_commands(
    repos_app,
    raise_exit=_raise_exit,
)
register_inbox_commands(
    inbox_app,
    require_hub_config=_require_hub_config,
    build_server_url=_build_server_url,
    request_json=_request_json,
    raise_exit=_raise_exit,
)
register_cleanup_commands(
    cleanup_app,
    require_repo_config=_require_repo_config,
)
app.add_typer(doctor_app, name="doctor")
register_doctor_commands(doctor_app)
protocol_app = typer.Typer(add_completion=False)
app.add_typer(protocol_app, name="protocol")
register_protocol_commands(protocol_app)
app.add_typer(flow_app, name="flow")
app.add_typer(ticket_flow_app, name="ticket-flow")
flow_app.add_typer(ticket_flow_app, name="ticket_flow")
app.add_typer(pma_cli_app, name="pma")
register_root_commands(app)
register_describe_commands(
    app,
    require_repo_config=_require_repo_config,
    raise_exit=_raise_exit,
)


FLOW_COMMANDS = register_flow_commands(
    flow_app,
    ticket_flow_app,
    require_repo_config=_require_repo_config,
    raise_exit=_raise_exit,
    build_agent_pool=build_agent_pool,
    build_ticket_flow_definition=build_ticket_flow_definition,
    guard_unregistered_hub_repo=_guard_unregistered_hub_repo,
    parse_bool_text=parse_bool_text,
    parse_duration=parse_duration,
    cleanup_stale_flow_runs=_cleanup_stale_flow_runs,
    archive_flow_run_artifacts=_archive_flow_run_artifacts,
)

# Legacy CLI module compatibility exports.
PreflightCheck = FLOW_COMMANDS["PreflightCheck"]
PreflightReport = FLOW_COMMANDS["PreflightReport"]


def _resumable_run(records):  # pragma: no cover - compatibility path
    return FLOW_COMMANDS["ticket_flow_resumable_run"](records)


def _ticket_flow_preflight(engine, ticket_dir):
    return FLOW_COMMANDS["_ticket_flow_preflight"](engine, ticket_dir)


def _print_preflight_report(report: PreflightReport) -> None:
    return FLOW_COMMANDS["ticket_flow_print_preflight_report"](report)


def ticket_flow_start(*args, **kwargs):
    return FLOW_COMMANDS["ticket_flow_start"](*args, **kwargs)


register_hub_tickets_commands(
    hub_tickets_app,
    require_hub_config_func=_require_hub_config,
    require_repo_config_func=_require_repo_config,
    require_templates_enabled_func=_require_templates_enabled,
    fetch_template_with_scan_func=_fetch_template_with_scan,
    build_hub_supervisor=_build_hub_supervisor,
    ticket_flow_preflight=lambda *args, **kwargs: _ticket_flow_preflight(
        *args, **kwargs
    ),
    print_preflight_report=lambda report: _print_preflight_report(report),
    ticket_flow_start=lambda *args, **kwargs: ticket_flow_start(*args, **kwargs),
)
if __name__ == "__main__":
    app()
