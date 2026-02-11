import asyncio
import importlib.metadata
import ipaddress
import json
import logging
import os
import shlex
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, Optional

import httpx
import typer
import uvicorn
import yaml

from ...agents.registry import validate_agent_id
from ...bootstrap import seed_hub_files, seed_repo_files
from ...core.config import (
    CONFIG_FILENAME,
    ConfigError,
    HubConfig,
    RepoConfig,
    _normalize_base_path,
    collect_env_overrides,
    derive_repo_config,
    find_nearest_hub_config_path,
    load_hub_config,
    load_repo_config,
)
from ...core.flows import FlowController, FlowStore
from ...core.flows.models import FlowRunRecord, FlowRunStatus
from ...core.flows.ux_helpers import build_flow_status_snapshot, ensure_worker
from ...core.flows.worker_process import (
    check_worker_health,
    clear_worker_metadata,
    register_worker_metadata,
)
from ...core.git_utils import GitError, run_git
from ...core.hub import HubSupervisor
from ...core.logging_utils import log_event, setup_rotating_logger
from ...core.optional_dependencies import require_optional_dependencies
from ...core.runtime import (
    DoctorReport,
    RuntimeContext,
    clear_stale_lock,
    doctor,
    hub_worktree_doctor_checks,
    pma_doctor_checks,
)
from ...core.state import RunnerState, load_state, now_iso, save_state, state_lock
from ...core.templates import (
    NetworkUnavailableError,
    RefNotFoundError,
    RepoNotConfiguredError,
    TemplateNotFoundError,
    fetch_template,
    get_scan_record,
    inject_provenance,
    parse_template_ref,
    scan_lock,
)
from ...core.usage import (
    UsageError,
    default_codex_home,
    parse_iso_datetime,
    summarize_hub_usage,
    summarize_repo_usage,
)
from ...core.utils import (
    RepoNotFoundError,
    atomic_write,
    default_editor,
    find_repo_root,
    is_within,
    resolve_executable,
)
from ...flows.ticket_flow import build_ticket_flow_definition
from ...integrations.agents import build_backend_orchestrator
from ...integrations.agents.wiring import (
    build_agent_backend_factory,
    build_app_server_supervisor_factory,
)
from ...integrations.telegram.adapter import TelegramAPIError, TelegramBotClient
from ...integrations.telegram.doctor import telegram_doctor_checks
from ...integrations.telegram.service import (
    TelegramBotConfig,
    TelegramBotConfigError,
    TelegramBotLockError,
    TelegramBotService,
)
from ...integrations.telegram.state import TelegramStateStore
from ...integrations.templates.scan_agent import (
    TemplateScanError,
    TemplateScanRejectedError,
    format_template_scan_rejection,
    run_template_scan,
)
from ...manifest import load_manifest
from ...tickets import AgentPool
from ...tickets.bulk import bulk_clear_model_pin, bulk_set_agent
from ...tickets.doctor import format_or_doctor_tickets
from ...tickets.files import (
    list_ticket_paths,
    read_ticket,
    safe_relpath,
    ticket_is_done,
)
from ...tickets.frontmatter import split_markdown_frontmatter
from ...tickets.import_pack import (
    TicketPackImportError,
    import_ticket_pack,
    load_template_frontmatter,
)
from ...tickets.lint import (
    lint_ticket_directory,
    parse_ticket_index,
)
from ...voice import VoiceConfig
from ..web.app import create_hub_app
from .pma_cli import pma_app as pma_cli_app
from .template_repos import TemplatesConfigError, load_template_repos_manager

logger = logging.getLogger("codex_autorunner.cli")

app = typer.Typer(add_completion=False)
hub_app = typer.Typer(add_completion=False)
dispatch_app = typer.Typer(add_completion=False)
telegram_app = typer.Typer(add_completion=False)
templates_app = typer.Typer(add_completion=False)
repos_app = typer.Typer(add_completion=False)
worktree_app = typer.Typer(add_completion=False)
hub_tickets_app = typer.Typer(add_completion=False)
flow_app = typer.Typer(add_completion=False)
ticket_flow_app = typer.Typer(add_completion=False)


def _car_version() -> str:
    try:
        return importlib.metadata.version("codex-autorunner")
    except Exception:
        return "unknown"


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(f"codex-autorunner {_car_version()}")
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


def _raise_exit(message: str, *, cause: Optional[BaseException] = None) -> NoReturn:
    typer.echo(message, err=True)
    if cause is not None:
        raise typer.Exit(code=1) from cause
    raise typer.Exit(code=1)


def _require_repo_config(repo: Optional[Path], hub: Optional[Path]) -> RuntimeContext:
    try:
        repo_root = find_repo_root(repo or Path.cwd())
    except RepoNotFoundError as exc:
        _raise_exit("No .git directory found for repo commands.", cause=exc)
    try:
        config = load_repo_config(repo_root, hub_path=hub)
        backend_orchestrator = build_backend_orchestrator(repo_root, config)
        return RuntimeContext(
            repo_root,
            config=config,
            backend_orchestrator=backend_orchestrator,
        )
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)


def _require_hub_config(path: Optional[Path]) -> HubConfig:
    try:
        return load_hub_config(path or Path.cwd())
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)


def _require_templates_enabled(config: RepoConfig) -> None:
    if not config.templates.enabled:
        _raise_exit(
            "Templates are disabled. Set templates.enabled=true in the hub config to enable."
        )


def _find_template_repo(config: RepoConfig, repo_id: str):
    for repo in config.templates.repos:
        if repo.id == repo_id:
            return repo
    return None


def _fetch_template_with_scan(template: str, ctx: RuntimeContext, hub: Optional[Path]):
    try:
        parsed = parse_template_ref(template)
    except ValueError as exc:
        _raise_exit(str(exc), cause=exc)

    repo_cfg = _find_template_repo(ctx.config, parsed.repo_id)
    if repo_cfg is None:
        _raise_exit(f"Template repo not configured: {parsed.repo_id}")

    hub_config_path = _resolve_hub_config_path_for_cli(ctx.repo_root, hub)
    if hub_config_path is None:
        try:
            hub_config = load_hub_config(ctx.repo_root)
            hub_root = hub_config.root
        except ConfigError as exc:
            _raise_exit(str(exc), cause=exc)
    else:
        hub_root = hub_config_path.parent.parent.resolve()

    try:
        fetched = fetch_template(
            repo=repo_cfg, hub_root=hub_root, template_ref=template
        )
    except NetworkUnavailableError as exc:
        _raise_exit(
            f"{str(exc)}\n"
            "Hint: Fetch once while online to seed the cache. "
            "If this template is untrusted, scanning may also require a working agent backend."
        )
    except (
        RepoNotConfiguredError,
        RefNotFoundError,
        TemplateNotFoundError,
        GitError,
    ) as exc:
        _raise_exit(str(exc), cause=exc)

    scan_record = None
    if not fetched.trusted:
        with scan_lock(hub_root, fetched.blob_sha):
            scan_record = get_scan_record(hub_root, fetched.blob_sha)
            if scan_record is None:
                try:
                    scan_record = asyncio.run(
                        run_template_scan(ctx=ctx, template=fetched)
                    )
                except TemplateScanRejectedError as exc:
                    _raise_exit(str(exc), cause=exc)
                except TemplateScanError as exc:
                    _raise_exit(str(exc), cause=exc)
            elif scan_record.decision != "approve":
                _raise_exit(format_template_scan_rejection(scan_record))

    return fetched, scan_record, hub_root


def _resolve_ticket_dir(repo_root: Path, ticket_dir: Optional[Path]) -> Path:
    if ticket_dir is None:
        return repo_root / ".codex-autorunner" / "tickets"
    if ticket_dir.is_absolute():
        return ticket_dir
    return repo_root / ticket_dir


def _collect_ticket_indices(ticket_dir: Path) -> list[int]:
    indices: list[int] = []
    if not ticket_dir.exists() or not ticket_dir.is_dir():
        return indices
    for path in ticket_dir.iterdir():
        if not path.is_file():
            continue
        idx = parse_ticket_index(path.name)
        if idx is None:
            continue
        indices.append(idx)
    return indices


def _next_available_ticket_index(existing: list[int]) -> int:
    if not existing:
        return 1
    seen = set(existing)
    candidate = 1
    while candidate in seen:
        candidate += 1
    return candidate


def _ticket_filename(index: int, *, suffix: str, width: int) -> str:
    return f"TICKET-{index:0{width}d}{suffix}.md"


def _normalize_ticket_suffix(suffix: Optional[str]) -> str:
    if not suffix:
        return ""
    cleaned = suffix.strip()
    if not cleaned:
        return ""
    if "/" in cleaned or "\\" in cleaned:
        _raise_exit("Ticket suffix may not include path separators.")
    if not cleaned.startswith("-"):
        return f"-{cleaned}"
    return cleaned


def _apply_agent_override(content: str, agent: str) -> str:
    fm_yaml, body = split_markdown_frontmatter(content)
    if fm_yaml is None:
        _raise_exit("Template is missing YAML frontmatter; cannot set agent.")
    try:
        data = yaml.safe_load(fm_yaml)
    except yaml.YAMLError as exc:
        _raise_exit(f"Template frontmatter is invalid YAML: {exc}")
    if not isinstance(data, dict):
        _raise_exit("Template frontmatter must be a YAML mapping to set agent.")
    data["agent"] = agent
    rendered = yaml.safe_dump(data, sort_keys=False).rstrip()
    return f"---\n{rendered}\n---{body}"


def _build_server_url(
    config, path: str, *, base_path_override: Optional[str] = None
) -> str:
    base_path = (
        _normalize_base_path(base_path_override)
        if base_path_override is not None
        else (config.server_base_path or "")
    )
    if base_path.endswith("/") and path.startswith("/"):
        base_path = base_path[:-1]
    return f"http://{config.server_host}:{config.server_port}{base_path}{path}"


def _resolve_hub_config_path_for_cli(
    repo_root: Path, hub: Optional[Path]
) -> Optional[Path]:
    if hub:
        candidate = hub
        if candidate.is_dir():
            candidate = candidate / CONFIG_FILENAME
        return candidate if candidate.exists() else None
    return find_nearest_hub_config_path(repo_root)


def _resolve_hub_repo_root(config: HubConfig, repo_id: str) -> Path:
    manifest = load_manifest(config.manifest_path, config.root)
    entry = manifest.get(repo_id)
    if entry is None:
        _raise_exit(f"Repo id not found in hub manifest: {repo_id}")
    repo_root = (config.root / entry.path).resolve()
    if not repo_root.exists():
        _raise_exit(f"Repo path does not exist: {repo_root}")
    return repo_root


def _parse_renumber(value: Optional[str]) -> Optional[dict[str, int]]:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    parts = [part.strip() for part in cleaned.split(",") if part.strip()]
    pairs: dict[str, int] = {}
    for part in parts:
        if "=" not in part:
            _raise_exit("Renumber format must be start=<n>,step=<n>.")
        key, raw = [segment.strip() for segment in part.split("=", 1)]
        if key not in ("start", "step"):
            _raise_exit("Renumber keys must be start and step.")
        try:
            value_int = int(raw)
        except ValueError as exc:
            _raise_exit(f"Renumber {key} must be an integer.", cause=exc)
        pairs[key] = value_int
    if "start" not in pairs or "step" not in pairs:
        _raise_exit("Renumber requires both start=<n> and step=<n>.")
    if pairs["start"] < 1 or pairs["step"] < 1:
        _raise_exit("Renumber start/step must be >= 1.")
    return pairs


def _guard_unregistered_hub_repo(repo_root: Path, hub: Optional[Path]) -> None:
    hub_config_path = _resolve_hub_config_path_for_cli(repo_root, hub)
    if hub_config_path is None:
        return
    try:
        hub_config = load_hub_config(hub_config_path)
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)

    repo_root = repo_root.resolve()
    under_repos = is_within(hub_config.repos_root, repo_root)
    under_worktrees = is_within(hub_config.worktrees_root, repo_root)
    if not (under_repos or under_worktrees):
        return

    manifest = load_manifest(hub_config.manifest_path, hub_config.root)
    if manifest.get_by_path(hub_config.root, repo_root) is not None:
        return

    lines = [
        "Repo not registered in hub manifest. Run car hub scan or create via car hub worktree create.",
        f"Detected hub root: {hub_config.root}",
        f"Repo path: {repo_root}",
        "Runs won't show up in the hub UI until registered.",
    ]
    if under_worktrees:
        lines.append(
            "Hint: Worktree names should look like <base_repo_id>--<branch> under "
            f"{hub_config.worktrees_root}"
        )
    _raise_exit("\n".join(lines))


def _resolve_repo_api_path(repo_root: Path, hub: Optional[Path], path: str) -> str:
    if not path.startswith("/"):
        path = f"/{path}"
    hub_config_path = _resolve_hub_config_path_for_cli(repo_root, hub)
    if hub_config_path is None:
        return path
    hub_root = hub_config_path.parent.parent.resolve()
    manifest_rel: Optional[str] = None
    try:
        raw = yaml.safe_load(hub_config_path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            hub_cfg = raw.get("hub")
            if isinstance(hub_cfg, dict):
                manifest_value = hub_cfg.get("manifest")
                if isinstance(manifest_value, str) and manifest_value.strip():
                    manifest_rel = manifest_value.strip()
    except (OSError, yaml.YAMLError, KeyError, ValueError) as exc:
        logger.debug("Failed to read hub config for manifest: %s", exc)
        manifest_rel = None
    manifest_path = hub_root / (manifest_rel or ".codex-autorunner/manifest.yml")
    if not manifest_path.exists():
        return path
    try:
        manifest = load_manifest(manifest_path, hub_root)
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Failed to load manifest: %s", exc)
        return path
    repo_root = repo_root.resolve()
    for entry in manifest.repos:
        candidate = (hub_root / entry.path).resolve()
        if candidate == repo_root:
            return f"/repos/{entry.id}{path}"
    return path


def _resolve_auth_token(env_name: str) -> Optional[str]:
    if not env_name:
        return None
    value = os.environ.get(env_name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _require_auth_token(env_name: Optional[str]) -> Optional[str]:
    if not env_name:
        return None
    token = _resolve_auth_token(env_name)
    if not token:
        _raise_exit(
            f"server.auth_token_env is set to {env_name}, but the environment variable is missing."
        )
    return token


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _enforce_bind_auth(host: str, token_env: str) -> None:
    if _is_loopback_host(host):
        return
    if _resolve_auth_token(token_env):
        return
    _raise_exit(
        "Refusing to bind to a non-loopback host without server.auth_token_env set."
    )


def _request_json(
    method: str,
    url: str,
    payload: Optional[dict] = None,
    token_env: Optional[str] = None,
) -> dict:
    headers = None
    if token_env:
        token = _require_auth_token(token_env)
        headers = {"Authorization": f"Bearer {token}"}
    response = httpx.request(
        method,
        url,
        json=payload,
        timeout=2.0,
        headers=headers,
        follow_redirects=True,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        preview = ""
        try:
            preview = (response.text or "")[:200].strip()
        except Exception:
            preview = ""
        hint = f" body_preview={preview!r}" if preview else ""
        raise httpx.HTTPError(
            f"Non-JSON response from {response.url!s} (status={response.status_code}).{hint}"
        ) from exc
    return data if isinstance(data, dict) else {}


def _request_form_json(
    method: str,
    url: str,
    form: Optional[dict] = None,
    token_env: Optional[str] = None,
    *,
    force_multipart: bool = False,
) -> dict:
    headers = None
    if token_env:
        token = _require_auth_token(token_env)
        headers = {"Authorization": f"Bearer {token}"}
    data = form
    files = None
    if force_multipart:
        data = form or {}
        files = []
    response = httpx.request(
        method,
        url,
        data=data,
        files=files,
        timeout=5.0,
        headers=headers,
        follow_redirects=True,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        preview = ""
        try:
            preview = (response.text or "")[:200].strip()
        except Exception:
            preview = ""
        hint = f" body_preview={preview!r}" if preview else ""
        raise httpx.HTTPError(
            f"Non-JSON response from {response.url!s} (status={response.status_code}).{hint}"
        ) from exc
    return data if isinstance(data, dict) else {}


def _require_optional_feature(
    *, feature: str, deps: list[tuple[str, str]], extra: Optional[str] = None
) -> None:
    try:
        require_optional_dependencies(feature=feature, deps=deps, extra=extra)
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)


app.add_typer(hub_app, name="hub")
hub_app.add_typer(dispatch_app, name="dispatch")
hub_app.add_typer(worktree_app, name="worktree")
hub_app.add_typer(hub_tickets_app, name="tickets")
app.add_typer(telegram_app, name="telegram")
app.add_typer(templates_app, name="templates")
templates_app.add_typer(repos_app, name="repos")
app.add_typer(flow_app, name="flow")
app.add_typer(ticket_flow_app, name="ticket-flow")
flow_app.add_typer(ticket_flow_app, name="ticket_flow")
app.add_typer(pma_cli_app, name="pma")


def _has_nested_git(path: Path) -> bool:
    try:
        for child in path.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            if (child / ".git").exists():
                return True
            if _has_nested_git(child):
                return True
    except OSError:
        return False
    return False


@app.command()
def init(
    path: Optional[Path] = typer.Argument(None, help="Repo path; defaults to CWD"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files"),
    git_init: bool = typer.Option(False, "--git-init", help="Run git init if missing"),
    mode: str = typer.Option(
        "auto",
        "--mode",
        help="Initialization mode: repo, hub, or auto (default)",
    ),
):
    """Initialize a repo for Codex autorunner."""
    start_path = (path or Path.cwd()).resolve()
    mode = (mode or "auto").lower()
    if mode not in ("auto", "repo", "hub"):
        _raise_exit("Invalid mode; expected repo, hub, or auto")

    git_required = True
    target_root: Optional[Path] = None
    selected_mode = mode

    # First try to treat this as a repo init if requested or auto-detected via .git.
    if mode in ("auto", "repo"):
        try:
            target_root = find_repo_root(start_path)
            selected_mode = "repo"
        except RepoNotFoundError:
            target_root = None

    # If no git root was found, decide between hub or repo-with-git-init.
    if target_root is None:
        target_root = start_path
        if mode in ("hub",) or (mode == "auto" and _has_nested_git(target_root)):
            selected_mode = "hub"
            git_required = False
        elif git_init:
            selected_mode = "repo"
            try:
                proc = run_git(["init"], target_root, check=False)
            except GitError as exc:
                _raise_exit(f"git init failed: {exc}")
            if proc.returncode != 0:
                detail = (
                    proc.stderr or proc.stdout or ""
                ).strip() or f"exit {proc.returncode}"
                _raise_exit(f"git init failed: {detail}")
        else:
            _raise_exit("No .git directory found; rerun with --git-init to create one")

    ca_dir = target_root / ".codex-autorunner"
    ca_dir.mkdir(parents=True, exist_ok=True)

    hub_config_path = find_nearest_hub_config_path(target_root)
    try:
        if selected_mode == "hub":
            seed_hub_files(target_root, force=force)
            typer.echo(f"Initialized hub at {ca_dir}")
        else:
            seed_repo_files(target_root, force=force, git_required=git_required)
            typer.echo(f"Initialized repo at {ca_dir}")
            if hub_config_path is None:
                seed_hub_files(target_root, force=force)
                typer.echo(f"Initialized hub at {ca_dir}")
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    typer.echo("Init complete")


@app.command()
def status(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", "--path", help="Hub root path"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
):
    """Show autorunner status."""
    engine = _require_repo_config(repo, hub)
    state = load_state(engine.state_path)
    repo_key = str(engine.repo_root)
    session_id = state.repo_to_session.get(repo_key) or state.repo_to_session.get(
        f"{repo_key}:codex"
    )
    opencode_session_id = state.repo_to_session.get(f"{repo_key}:opencode")
    session_record = state.sessions.get(session_id) if session_id else None
    opencode_record = (
        state.sessions.get(opencode_session_id) if opencode_session_id else None
    )

    if output_json:
        hub_config_path = _resolve_hub_config_path_for_cli(engine.repo_root, hub)
        payload = {
            "repo": str(engine.repo_root),
            "hub": (
                str(hub_config_path.parent.parent.resolve())
                if hub_config_path
                else None
            ),
            "status": state.status,
            "last_run_id": state.last_run_id,
            "last_exit_code": state.last_exit_code,
            "last_run_started_at": state.last_run_started_at,
            "last_run_finished_at": state.last_run_finished_at,
            "runner_pid": state.runner_pid,
            "session_id": session_id,
            "session_record": (
                {
                    "repo_path": session_record.repo_path,
                    "created_at": session_record.created_at,
                    "last_seen_at": session_record.last_seen_at,
                    "status": session_record.status,
                    "agent": session_record.agent,
                }
                if session_record
                else None
            ),
            "opencode_session_id": opencode_session_id,
            "opencode_record": (
                {
                    "repo_path": opencode_record.repo_path,
                    "created_at": opencode_record.created_at,
                    "last_seen_at": opencode_record.last_seen_at,
                    "status": opencode_record.status,
                    "agent": opencode_record.agent,
                }
                if opencode_record
                else None
            ),
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.echo(f"Repo: {engine.repo_root}")
    typer.echo(f"Status: {state.status}")
    typer.echo(f"Last run id: {state.last_run_id}")
    typer.echo(f"Last exit code: {state.last_exit_code}")
    typer.echo(f"Last start: {state.last_run_started_at}")
    typer.echo(f"Last finish: {state.last_run_finished_at}")
    typer.echo(f"Runner pid: {state.runner_pid}")
    if not session_id and not opencode_session_id:
        typer.echo("Terminal session: none")
    if session_id:
        detail = ""
        if session_record:
            detail = f" (status={session_record.status}, last_seen={session_record.last_seen_at})"
        typer.echo(f"Terminal session (codex): {session_id}{detail}")
    if opencode_session_id and opencode_session_id != session_id:
        detail = ""
        if opencode_record:
            detail = f" (status={opencode_record.status}, last_seen={opencode_record.last_seen_at})"
        typer.echo(f"Terminal session (opencode): {opencode_session_id}{detail}")


@templates_app.command("fetch")
def templates_fetch(
    template: str = typer.Argument(
        ..., help="Template ref formatted as REPO_ID:PATH[@REF]"
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", help="Write template content to a file"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Fetch a template from a configured templates repo."""
    ctx = _require_repo_config(repo, hub)
    _require_templates_enabled(ctx.config)
    fetched, scan_record, _hub_root = _fetch_template_with_scan(template, ctx, hub)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(fetched.content, encoding="utf-8")
        typer.echo(f"Wrote template to {out}", err=True)

    if output_json:
        payload = {
            "content": fetched.content,
            "repo_id": fetched.repo_id,
            "path": fetched.path,
            "ref": fetched.ref,
            "commit_sha": fetched.commit_sha,
            "blob_sha": fetched.blob_sha,
            "trusted": fetched.trusted,
            "scan_decision": scan_record.to_dict() if scan_record else None,
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    if out is None:
        typer.echo(fetched.content, nl=False)


@templates_app.command("apply")
def templates_apply(
    template: str = typer.Argument(
        ..., help="Template ref formatted as REPO_ID:PATH[@REF]"
    ),
    ticket_dir: Optional[Path] = typer.Option(
        None,
        "--ticket-dir",
        help="Ticket directory (default .codex-autorunner/tickets)",
    ),
    at: Optional[int] = typer.Option(None, "--at", help="Explicit ticket index"),
    next_index: bool = typer.Option(
        True, "--next/--no-next", help="Use next available index (default)"
    ),
    suffix: Optional[str] = typer.Option(
        None, "--suffix", help="Optional filename suffix (e.g. -foo)"
    ),
    set_agent: Optional[str] = typer.Option(
        None, "--set-agent", help="Override frontmatter agent"
    ),
    provenance: bool = typer.Option(
        False,
        "--provenance/--no-provenance",
        help="Embed template provenance in ticket",
    ),
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Apply a template by writing it into the ticket directory."""
    ctx = _require_repo_config(repo, hub)
    _require_templates_enabled(ctx.config)

    fetched, scan_record, _hub_root = _fetch_template_with_scan(template, ctx, hub)

    resolved_dir = _resolve_ticket_dir(ctx.repo_root, ticket_dir)
    if resolved_dir.exists() and not resolved_dir.is_dir():
        _raise_exit(f"Ticket dir is not a directory: {resolved_dir}")
    try:
        resolved_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _raise_exit(f"Unable to create ticket dir: {exc}")

    if at is None and not next_index:
        _raise_exit("Specify --at or leave --next enabled to pick an index.")
    if at is not None and at < 1:
        _raise_exit("Ticket index must be >= 1.")

    existing_indices = _collect_ticket_indices(resolved_dir)
    if at is None:
        index = _next_available_ticket_index(existing_indices)
    else:
        index = at
        if index in existing_indices:
            _raise_exit(
                f"Ticket index {index} already exists. Choose another index or open a gap."
            )

    normalized_suffix = _normalize_ticket_suffix(suffix)
    width = max(3, max([len(str(i)) for i in existing_indices + [index]]))
    filename = _ticket_filename(index, suffix=normalized_suffix, width=width)
    path = resolved_dir / filename
    if path.exists():
        _raise_exit(f"Ticket already exists: {path}")

    content = fetched.content
    if set_agent:
        if set_agent != "user":
            try:
                validate_agent_id(set_agent)
            except ValueError as exc:
                _raise_exit(str(exc), cause=exc)
        content = _apply_agent_override(content, set_agent)

    if provenance:
        content = inject_provenance(content, fetched, scan_record)

    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        _raise_exit(f"Failed to write ticket: {exc}")

    metadata = {
        "repo_id": fetched.repo_id,
        "path": fetched.path,
        "ref": fetched.ref,
        "commit_sha": fetched.commit_sha,
        "blob_sha": fetched.blob_sha,
        "trusted": fetched.trusted,
        "scan": scan_record.to_dict() if scan_record else None,
    }
    typer.echo(
        "Created ticket "
        f"{path} (index={index}, template={fetched.repo_id}:{fetched.path}@{fetched.ref})"
    )
    typer.echo(json.dumps(metadata, indent=2))


@repos_app.command("list")
def repos_list(
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
):
    """List configured template repos."""
    manager = load_template_repos_manager(hub)
    repos = manager.list_repos()

    if output_json:
        payload = {"repos": repos}
        typer.echo(json.dumps(payload, indent=2))
        return

    if not repos:
        typer.echo("No template repos configured.")
        return

    typer.echo(f"Template repos ({len(repos)}):")
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        repo_id = repo.get("id", "")
        url = repo.get("url", "")
        trusted = repo.get("trusted", False)
        default_ref = repo.get("default_ref", "main")
        trusted_text = "trusted" if trusted else "untrusted"
        typer.echo(f"  - {repo_id}: {url} [{trusted_text}] (default_ref={default_ref})")


@repos_app.command("add")
def repos_add(
    repo_id: str = typer.Argument(..., help="Unique repo ID"),
    url: str = typer.Argument(..., help="Git repo URL or path"),
    trusted: Optional[bool] = typer.Option(
        None, "--trusted/--untrusted", help="Trust level (default: untrusted)"
    ),
    default_ref: str = typer.Option("main", "--default-ref", help="Default git ref"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Add a template repo to the hub config."""
    manager = load_template_repos_manager(hub)
    try:
        manager.add_repo(repo_id, url, trusted, default_ref)
    except TemplatesConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    except OSError as exc:
        _raise_exit(f"Failed to write hub config: {exc}", cause=exc)

    try:
        manager.save()
    except OSError as exc:
        _raise_exit(f"Failed to write hub config: {exc}", cause=exc)

    typer.echo(f"Added template repo '{repo_id}' to hub config.")


@repos_app.command("remove")
def repos_remove(
    repo_id: str = typer.Argument(..., help="Repo ID to remove"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Remove a template repo from the hub config."""
    manager = load_template_repos_manager(hub)
    try:
        manager.remove_repo(repo_id)
    except TemplatesConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    except OSError as exc:
        _raise_exit(f"Failed to write hub config: {exc}", cause=exc)

    try:
        manager.save()
    except OSError as exc:
        _raise_exit(f"Failed to write hub config: {exc}", cause=exc)

    typer.echo(f"Removed template repo '{repo_id}' from hub config.")


@repos_app.command("trust")
def repos_trust(
    repo_id: str = typer.Argument(..., help="Repo ID to trust"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Mark a template repo as trusted (skip scanning)."""
    manager = load_template_repos_manager(hub)
    try:
        manager.set_trusted(repo_id, True)
    except TemplatesConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    except OSError as exc:
        _raise_exit(f"Failed to write hub config: {exc}", cause=exc)

    try:
        manager.save()
    except OSError as exc:
        _raise_exit(f"Failed to write hub config: {exc}", cause=exc)

    typer.echo(f"Marked repo '{repo_id}' as trusted.")


@repos_app.command("untrust")
def repos_untrust(
    repo_id: str = typer.Argument(..., help="Repo ID to untrust"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Mark a template repo as untrusted (require scanning)."""
    manager = load_template_repos_manager(hub)
    try:
        manager.set_trusted(repo_id, False)
    except TemplatesConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    except OSError as exc:
        _raise_exit(f"Failed to write hub config: {exc}", cause=exc)

    try:
        manager.save()
    except OSError as exc:
        _raise_exit(f"Failed to write hub config: {exc}", cause=exc)

    typer.echo(f"Marked repo '{repo_id}' as untrusted.")


@app.command()
def sessions(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
):
    """List active terminal sessions."""
    engine = _require_repo_config(repo, hub)
    config = engine.config
    path = _resolve_repo_api_path(engine.repo_root, hub, "/api/sessions")
    url = _build_server_url(config, path)
    auth_token = _resolve_auth_token(config.server_auth_token_env)
    if auth_token:
        url = f"{url}?include_abs_paths=1"
    payload = None
    source = "server"
    try:
        payload = _request_json("GET", url, token_env=config.server_auth_token_env)
    except (
        httpx.HTTPError,
        httpx.ConnectError,
        httpx.TimeoutException,
        OSError,
    ) as exc:
        logger.debug(
            "Failed to fetch sessions from server, falling back to state: %s", exc
        )
        state = load_state(engine.state_path)
        payload = {
            "sessions": [
                {
                    "session_id": session_id,
                    "repo_path": record.repo_path,
                    "created_at": record.created_at,
                    "last_seen_at": record.last_seen_at,
                    "status": record.status,
                    "alive": None,
                }
                for session_id, record in state.sessions.items()
            ],
            "repo_to_session": dict(state.repo_to_session),
        }
        source = "state"

    if output_json:
        if source != "server":
            payload["source"] = source
        typer.echo(json.dumps(payload, indent=2))
        return

    sessions_payload = payload.get("sessions", []) if isinstance(payload, dict) else []
    typer.echo(f"Sessions ({source}): {len(sessions_payload)}")
    for entry in sessions_payload:
        if not isinstance(entry, dict):
            continue
        session_id = entry.get("session_id") or "unknown"
        repo_path = entry.get("abs_repo_path") or entry.get("repo_path") or "unknown"
        status = entry.get("status") or "unknown"
        last_seen = entry.get("last_seen_at") or "unknown"
        alive = entry.get("alive")
        alive_text = "unknown" if alive is None else str(bool(alive))
        typer.echo(
            f"- {session_id}: repo={repo_path} status={status} last_seen={last_seen} alive={alive_text}"
        )


@app.command("stop-session")
def stop_session(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    session_id: Optional[str] = typer.Option(
        None, "--session", help="Session id to stop"
    ),
):
    """Stop a terminal session by id or repo path."""
    engine = _require_repo_config(repo, hub)
    config = engine.config
    payload: dict[str, str] = {}
    if session_id:
        payload["session_id"] = session_id
    else:
        payload["repo_path"] = str(engine.repo_root)

    path = _resolve_repo_api_path(engine.repo_root, hub, "/api/sessions/stop")
    url = _build_server_url(config, path)
    try:
        response = _request_json(
            "POST", url, payload, token_env=config.server_auth_token_env
        )
        stopped_id = response.get("session_id", payload.get("session_id", ""))
        typer.echo(f"Stopped session {stopped_id}")
        return
    except (
        httpx.HTTPError,
        httpx.ConnectError,
        httpx.TimeoutException,
        OSError,
    ) as exc:
        logger.debug(
            "Failed to stop session via server, falling back to state: %s", exc
        )

    with state_lock(engine.state_path):
        state = load_state(engine.state_path)
        target_id = payload.get("session_id")
        if not target_id:
            repo_lookup = payload.get("repo_path")
            if repo_lookup:
                target_id = (
                    state.repo_to_session.get(repo_lookup)
                    or state.repo_to_session.get(f"{repo_lookup}:codex")
                    or state.repo_to_session.get(f"{repo_lookup}:opencode")
                )
        if not target_id:
            _raise_exit("Session not found (server unavailable)")
        state.sessions.pop(target_id, None)
        state.repo_to_session = {
            repo_key: sid
            for repo_key, sid in state.repo_to_session.items()
            if sid != target_id
        }
        save_state(engine.state_path, state)
    typer.echo(f"Stopped session {target_id} (state only)")


@app.command()
def usage(
    repo: Optional[Path] = typer.Option(
        None, "--repo", help="Repo or hub path; defaults to CWD"
    ),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    codex_home: Optional[Path] = typer.Option(
        None, "--codex-home", help="Override CODEX_HOME (defaults to env or ~/.codex)"
    ),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help="ISO timestamp filter, e.g. 2025-12-01 or 2025-12-01T12:00Z",
    ),
    until: Optional[str] = typer.Option(
        None, "--until", help="Upper bound ISO timestamp filter"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
):
    """Show Codex/OpenCode token usage for a repo or hub by reading local session logs."""
    try:
        since_dt = parse_iso_datetime(since)
        until_dt = parse_iso_datetime(until)
    except UsageError as exc:
        _raise_exit(str(exc), cause=exc)

    codex_root = (codex_home or default_codex_home()).expanduser()

    repo_root: Optional[Path] = None
    try:
        repo_root = find_repo_root(repo or Path.cwd())
    except RepoNotFoundError:
        repo_root = None

    if repo_root and (repo_root / ".codex-autorunner" / "state.sqlite3").exists():
        engine = _require_repo_config(repo, hub)
    else:
        try:
            config = load_hub_config(hub or repo or Path.cwd())
        except ConfigError as exc:
            _raise_exit(str(exc), cause=exc)
        manifest = load_manifest(config.manifest_path, config.root)
        repo_map = [(entry.id, (config.root / entry.path)) for entry in manifest.repos]
        per_repo, unmatched = summarize_hub_usage(
            repo_map,
            codex_root,
            since=since_dt,
            until=until_dt,
        )
        if output_json:
            payload = {
                "mode": "hub",
                "hub_root": str(config.root),
                "codex_home": str(codex_root),
                "since": since,
                "until": until,
                "repos": {
                    repo_id: summary.to_dict() for repo_id, summary in per_repo.items()
                },
                "unmatched": unmatched.to_dict(),
            }
            typer.echo(json.dumps(payload, indent=2))
            return

        typer.echo(f"Hub: {config.root}")
        typer.echo(f"CODEX_HOME: {codex_root}")
        typer.echo(f"Repos: {len(per_repo)}")
        for repo_id, summary in per_repo.items():
            typer.echo(
                f"- {repo_id}: total={summary.totals.total_tokens} "
                f"(input={summary.totals.input_tokens}, cached={summary.totals.cached_input_tokens}, "
                f"output={summary.totals.output_tokens}, reasoning={summary.totals.reasoning_output_tokens}) "
                f"events={summary.events}"
            )
        if unmatched.events or unmatched.totals.total_tokens:
            typer.echo(
                f"- unmatched: total={unmatched.totals.total_tokens} events={unmatched.events}"
            )
        return

    summary = summarize_repo_usage(
        engine.repo_root,
        codex_root,
        since=since_dt,
        until=until_dt,
    )

    if output_json:
        payload = {
            "mode": "repo",
            "repo": str(engine.repo_root),
            "codex_home": str(codex_root),
            "since": since,
            "until": until,
            "usage": summary.to_dict(),
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.echo(f"Repo: {engine.repo_root}")
    typer.echo(f"CODEX_HOME: {codex_root}")
    typer.echo(
        f"Totals: total={summary.totals.total_tokens} "
        f"(input={summary.totals.input_tokens}, cached={summary.totals.cached_input_tokens}, "
        f"output={summary.totals.output_tokens}, reasoning={summary.totals.reasoning_output_tokens})"
    )
    typer.echo(f"Events counted: {summary.events}")
    if summary.latest_rate_limits:
        primary = summary.latest_rate_limits.get("primary", {}) or {}
        secondary = summary.latest_rate_limits.get("secondary", {}) or {}
        typer.echo(
            f"Latest rate limits: primary_used={primary.get('used_percent')}%/{primary.get('window_minutes')}m, "
            f"secondary_used={secondary.get('used_percent')}%/{secondary.get('window_minutes')}m"
        )


@app.command()
def kill(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Force-kill a running autorunner and clear stale lock/state."""
    engine = _require_repo_config(repo, hub)
    pid = engine.kill_running_process()
    with state_lock(engine.state_path):
        state = load_state(engine.state_path)
        new_state = RunnerState(
            last_run_id=state.last_run_id,
            status="error",
            last_exit_code=137,
            last_run_started_at=state.last_run_started_at,
            last_run_finished_at=now_iso(),
            autorunner_agent_override=state.autorunner_agent_override,
            autorunner_model_override=state.autorunner_model_override,
            autorunner_effort_override=state.autorunner_effort_override,
            autorunner_approval_policy=state.autorunner_approval_policy,
            autorunner_sandbox_mode=state.autorunner_sandbox_mode,
            autorunner_workspace_write_network=state.autorunner_workspace_write_network,
            runner_pid=None,
            sessions=state.sessions,
            repo_to_session=state.repo_to_session,
        )
        save_state(engine.state_path, new_state)
    clear_stale_lock(engine.lock_path)
    if pid:
        typer.echo(f"Sent SIGTERM to pid {pid}")
    else:
        typer.echo("No active autorunner process found; cleared stale lock if any.")


@app.command()
def resume(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Resume a paused/running ticket flow (now uses ticket_flow).

    This command now uses ticket_flow for execution. For full control over
    flows, use 'car flow' commands instead.
    """
    # Note: Resume is now handled by 'car flow ticket_flow/start' which
    # will reuse an active/paused run automatically.
    typer.echo("The 'resume' command has been deprecated in favor of ticket_flow.")
    typer.echo("Use 'car flow ticket_flow/start' to resume existing flows.")
    raise typer.Exit(code=0)


@app.command()
def log(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    run_id: Optional[int] = typer.Option(None, "--run", help="Show a specific run"),
    tail: Optional[int] = typer.Option(None, "--tail", help="Tail last N lines"),
):
    """Show autorunner log output."""
    engine = _require_repo_config(repo, hub)
    if not engine.log_path.exists():
        _raise_exit("Log file not found; run init")

    if run_id is not None:
        block = engine.read_run_block(run_id)
        if not block:
            _raise_exit("run not found")
        typer.echo(block)
        return

    if tail is not None:
        typer.echo(engine.tail_log(tail))
    else:
        state = load_state(engine.state_path)
        last_id = state.last_run_id
        if last_id is None:
            typer.echo("No runs recorded yet")
            return
        block = engine.read_run_block(last_id)
        if not block:
            typer.echo("No run block found (log may have rotated)")
            return
        typer.echo(block)


@app.command()
def edit(
    target: str = typer.Argument(..., help="active_context|decisions|spec"),
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Open one of the docs in $EDITOR."""
    engine = _require_repo_config(repo, hub)
    config = engine.config
    key = target.lower()
    if key not in ("active_context", "decisions", "spec"):
        _raise_exit("Invalid target; choose active_context, decisions, or spec")
    path = config.doc_path(key)
    ui_cfg = config.raw.get("ui") if isinstance(config.raw, dict) else {}
    ui_cfg = ui_cfg if isinstance(ui_cfg, dict) else {}
    config_editor = ui_cfg.get("editor") if isinstance(ui_cfg, dict) else None
    if not isinstance(config_editor, str) or not config_editor.strip():
        config_editor = "vi"
    editor = (
        os.environ.get("VISUAL")
        or os.environ.get("EDITOR")
        or default_editor(fallback=config_editor)
    )
    editor_parts = shlex.split(editor)
    if not editor_parts:
        editor_parts = [editor]
    typer.echo(f"Opening {path} with {' '.join(editor_parts)}")
    subprocess.run([*editor_parts, str(path)])


@app.command("doctor")
def doctor_cmd(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo or hub path"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON for scripting"),
):
    """Validate repo or hub setup."""
    try:
        start_path = repo or Path.cwd()
        report = doctor(start_path)

        hub_config = load_hub_config(start_path)
        repo_config: Optional[RepoConfig] = None
        repo_root: Optional[Path] = None
        try:
            repo_root = find_repo_root(start_path)
            repo_config = derive_repo_config(hub_config, repo_root)
        except RepoNotFoundError:
            repo_config = None

        telegram_checks = telegram_doctor_checks(
            repo_config or hub_config, repo_root=repo_root
        )
        pma_checks = pma_doctor_checks(hub_config, repo_root=repo_root)
        hub_worktree_checks = hub_worktree_doctor_checks(hub_config)

        report = DoctorReport(
            checks=report.checks + telegram_checks + pma_checks + hub_worktree_checks
        )
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    if json_output:
        typer.echo(json.dumps(report.to_dict(), indent=2))
        if report.has_errors():
            raise typer.Exit(code=1)
        return
    for check in report.checks:
        line = f"- {check.status.upper()}: {check.message}"
        if check.fix:
            line = f"{line} Fix: {check.fix}"
        typer.echo(line)
    if report.has_errors():
        _raise_exit("Doctor check failed")
    typer.echo("Doctor check passed")


@app.command()
def serve(
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
    host: Optional[str] = typer.Option(None, "--host", help="Host to bind"),
    port: Optional[int] = typer.Option(None, "--port", help="Port to bind"),
    base_path: Optional[str] = typer.Option(
        None, "--base-path", help="Base path for the server"
    ),
):
    """Start the hub web server and UI API."""
    try:
        config = load_hub_config(path or Path.cwd())
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    bind_host = host or config.server_host
    bind_port = port or config.server_port
    normalized_base = (
        _normalize_base_path(base_path)
        if base_path is not None
        else config.server_base_path
    )
    _enforce_bind_auth(bind_host, config.server_auth_token_env)
    typer.echo(f"Serving hub on http://{bind_host}:{bind_port}{normalized_base or ''}")
    uvicorn.run(
        create_hub_app(config.root, base_path=normalized_base),
        host=bind_host,
        port=bind_port,
        root_path="",
        access_log=config.server_access_log,
    )


@hub_app.command("create")
def hub_create(
    repo_id: str = typer.Argument(..., help="Base repo id to create and initialize"),
    repo_path: Optional[Path] = typer.Option(
        None,
        "--repo-path",
        help="Custom repo path relative to hub repos_root",
    ),
    path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
    force: bool = typer.Option(False, "--force", help="Allow existing directory"),
    git_init: bool = typer.Option(
        True, "--git-init/--no-git-init", help="Run git init in the new repo"
    ),
):
    """Create a new base git repo under the hub and initialize codex-autorunner files.

    For worktrees, use `car hub worktree create`.
    """
    config = _require_hub_config(path)
    supervisor = HubSupervisor(
        config,
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
        agent_id_validator=validate_agent_id,
    )
    try:
        snapshot = supervisor.create_repo(
            repo_id, repo_path, git_init=git_init, force=force
        )
    except Exception as exc:
        _raise_exit(str(exc), cause=exc)
    typer.echo(f"Created repo {snapshot.id} at {snapshot.path}")


@hub_app.command("clone")
def hub_clone(
    git_url: str = typer.Option(
        ..., "--git-url", help="Git URL or local path to clone"
    ),
    repo_id: Optional[str] = typer.Option(
        None, "--id", help="Repo id to register (defaults from git URL)"
    ),
    repo_path: Optional[Path] = typer.Option(
        None,
        "--repo-path",
        help="Custom repo path relative to hub repos_root",
    ),
    path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
    force: bool = typer.Option(False, "--force", help="Allow existing directory"),
):
    """Clone a git repo under the hub and initialize codex-autorunner files."""
    config = _require_hub_config(path)
    supervisor = HubSupervisor(
        config,
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        agent_id_validator=validate_agent_id,
    )
    try:
        snapshot = supervisor.clone_repo(
            git_url=git_url, repo_id=repo_id, repo_path=repo_path, force=force
        )
    except Exception as exc:
        _raise_exit(str(exc), cause=exc)
    typer.echo(
        f"Cloned repo {snapshot.id} at {snapshot.path} (status={snapshot.status.value})"
    )


def _worktree_snapshot_payload(snapshot) -> dict:
    return {
        "id": snapshot.id,
        "worktree_of": snapshot.worktree_of,
        "branch": snapshot.branch,
        "path": str(snapshot.path),
        "initialized": snapshot.initialized,
        "exists_on_disk": snapshot.exists_on_disk,
        "status": snapshot.status.value,
    }


@worktree_app.command("create")
def hub_worktree_create(
    base_repo_id: str = typer.Argument(..., help="Base repo id to branch from"),
    branch: str = typer.Argument(..., help="Branch name for the new worktree"),
    hub: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
    force: bool = typer.Option(False, "--force", help="Allow existing directory"),
    start_point: Optional[str] = typer.Option(
        None, "--start-point", help="Optional git ref to branch from"
    ),
):
    """Create a new hub-managed worktree."""
    config = _require_hub_config(hub)
    supervisor = HubSupervisor(
        config,
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
        agent_id_validator=validate_agent_id,
    )
    try:
        snapshot = supervisor.create_worktree(
            base_repo_id=base_repo_id,
            branch=branch,
            force=force,
            start_point=start_point,
        )
    except Exception as exc:
        _raise_exit(str(exc), cause=exc)
    typer.echo(
        f"Created worktree {snapshot.id} (branch={snapshot.branch}) at {snapshot.path}"
    )


@worktree_app.command("list")
def hub_worktree_list(
    hub: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
):
    """List hub-managed worktrees."""
    config = _require_hub_config(hub)
    supervisor = HubSupervisor(
        config,
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        agent_id_validator=validate_agent_id,
    )
    snapshots = [
        snapshot
        for snapshot in supervisor.list_repos(use_cache=False)
        if snapshot.kind == "worktree"
    ]
    payload = [_worktree_snapshot_payload(snapshot) for snapshot in snapshots]
    if output_json:
        typer.echo(json.dumps({"worktrees": payload}, indent=2))
        return
    if not payload:
        typer.echo("No worktrees found.")
        return
    typer.echo(f"Worktrees ({len(payload)}):")
    for item in payload:
        typer.echo(
            "  - {id} (base={worktree_of}, branch={branch}, status={status}, initialized={initialized}, exists={exists_on_disk}, path={path})".format(
                **item
            )
        )


@worktree_app.command("scan")
def hub_worktree_scan(
    hub: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
):
    """Scan hub root and list discovered worktrees."""
    config = _require_hub_config(hub)
    supervisor = HubSupervisor(
        config,
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        agent_id_validator=validate_agent_id,
    )
    snapshots = [snap for snap in supervisor.scan() if snap.kind == "worktree"]
    payload = [_worktree_snapshot_payload(snapshot) for snapshot in snapshots]
    if output_json:
        typer.echo(json.dumps({"worktrees": payload}, indent=2))
        return
    if not payload:
        typer.echo("No worktrees found.")
        return
    typer.echo(f"Worktrees ({len(payload)}):")
    for item in payload:
        typer.echo(
            "  - {id} (base={worktree_of}, branch={branch}, status={status}, initialized={initialized}, exists={exists_on_disk}, path={path})".format(
                **item
            )
        )


@worktree_app.command("cleanup")
def hub_worktree_cleanup(
    worktree_repo_id: str = typer.Argument(..., help="Worktree repo id to remove"),
    hub: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
    delete_branch: bool = typer.Option(
        False, "--delete-branch", help="Delete the local branch"
    ),
    delete_remote: bool = typer.Option(
        False, "--delete-remote", help="Delete the remote branch"
    ),
    archive: bool = typer.Option(
        True, "--archive/--no-archive", help="Archive worktree snapshot"
    ),
    force_archive: bool = typer.Option(
        False, "--force-archive", help="Continue cleanup if archive fails"
    ),
    archive_note: Optional[str] = typer.Option(
        None, "--archive-note", help="Optional archive note"
    ),
):
    """Cleanup a hub-managed worktree."""
    config = _require_hub_config(hub)
    supervisor = HubSupervisor(
        config,
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        agent_id_validator=validate_agent_id,
    )
    try:
        supervisor.cleanup_worktree(
            worktree_repo_id=worktree_repo_id,
            delete_branch=delete_branch,
            delete_remote=delete_remote,
            archive=archive,
            force_archive=force_archive,
            archive_note=archive_note,
        )
    except Exception as exc:
        _raise_exit(str(exc), cause=exc)
    typer.echo("ok")


@hub_app.command("serve")
def hub_serve(
    path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
    host: Optional[str] = typer.Option(None, "--host", help="Host to bind"),
    port: Optional[int] = typer.Option(None, "--port", help="Port to bind"),
    base_path: Optional[str] = typer.Option(
        None, "--base-path", help="Base path for the server"
    ),
):
    """Start the hub supervisor server."""
    config = _require_hub_config(path)
    normalized_base = (
        _normalize_base_path(base_path)
        if base_path is not None
        else config.server_base_path
    )
    bind_host = host or config.server_host
    bind_port = port or config.server_port
    _enforce_bind_auth(bind_host, config.server_auth_token_env)
    typer.echo(f"Serving hub on http://{bind_host}:{bind_port}{normalized_base or ''}")
    uvicorn.run(
        create_hub_app(config.root, base_path=normalized_base),
        host=bind_host,
        port=bind_port,
        root_path="",
        access_log=config.server_access_log,
    )


@hub_app.command("scan")
def hub_scan(path: Optional[Path] = typer.Option(None, "--path", help="Hub root path")):
    """Trigger discovery/init and print repo statuses."""
    config = _require_hub_config(path)
    supervisor = HubSupervisor(
        config,
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        agent_id_validator=validate_agent_id,
    )
    snapshots = supervisor.scan()
    typer.echo(f"Scanned hub at {config.root} (repos_root={config.repos_root})")
    for snap in snapshots:
        typer.echo(
            f"- {snap.id}: {snap.status.value}, initialized={snap.initialized}, exists={snap.exists_on_disk}"
        )


@hub_app.command("snapshot")
def hub_snapshot(
    path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
    output_json: bool = typer.Option(
        True, "--json/--no-json", help="Emit JSON output (default: true)"
    ),
    pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output"),
    base_path: Optional[str] = typer.Option(
        None, "--base-path", help="Override hub server base path (e.g. /car)"
    ),
):
    """Return a compact hub snapshot (repos + inbox items)."""
    config = _require_hub_config(path)
    repos_url = _build_server_url(config, "/hub/repos", base_path_override=base_path)
    messages_url = _build_server_url(
        config, "/hub/messages?limit=50", base_path_override=base_path
    )

    try:
        repos_response = _request_json(
            "GET", repos_url, token_env=config.server_auth_token_env
        )
        messages_response = _request_json(
            "GET", messages_url, token_env=config.server_auth_token_env
        )
    except (
        httpx.HTTPError,
        httpx.ConnectError,
        httpx.TimeoutException,
        OSError,
    ) as exc:
        logger.debug("Failed to fetch hub snapshot from server: %s", exc)
        _raise_exit(
            "Failed to connect to hub server. Ensure 'car hub serve' is running.\n"
            f"Attempted:\n- {repos_url}\n- {messages_url}\n"
            "If the hub UI is served under a base path (commonly /car), either set "
            "`server.base_path` in the hub config or pass `--base-path /car`.",
            cause=exc,
        )

    repos_payload = repos_response if isinstance(repos_response, dict) else {}
    messages_payload = messages_response if isinstance(messages_response, dict) else {}

    repos = repos_payload.get("repos", []) if isinstance(repos_payload, dict) else []
    messages_items = (
        messages_payload.get("items", []) if isinstance(messages_payload, dict) else []
    )

    def _summarize_repo(repo: dict) -> dict:
        if not isinstance(repo, dict):
            return {}
        ticket_flow = (
            repo.get("ticket_flow") if isinstance(repo.get("ticket_flow"), dict) else {}
        )
        failure = ticket_flow.get("failure") if isinstance(ticket_flow, dict) else None
        failure_summary = (
            ticket_flow.get("failure_summary")
            if isinstance(ticket_flow, dict)
            else None
        )
        pr_url = ticket_flow.get("pr_url") if isinstance(ticket_flow, dict) else None
        final_review_status = (
            ticket_flow.get("final_review_status")
            if isinstance(ticket_flow, dict)
            else None
        )
        return {
            "id": repo.get("id"),
            "display_name": repo.get("display_name"),
            "status": repo.get("status"),
            "initialized": repo.get("initialized"),
            "exists_on_disk": repo.get("exists_on_disk"),
            "last_run_id": repo.get("last_run_id"),
            "last_run_started_at": repo.get("last_run_started_at"),
            "last_run_finished_at": repo.get("last_run_finished_at"),
            "failure": failure,
            "failure_summary": failure_summary,
            "pr_url": pr_url,
            "final_review_status": final_review_status,
        }

    def _summarize_message(msg: dict) -> dict:
        if not isinstance(msg, dict):
            return {}
        dispatch = msg.get("dispatch", {})
        if not isinstance(dispatch, dict):
            dispatch = {}
        body = dispatch.get("body", "")
        title = dispatch.get("title", "")
        truncated_body = (body[:200] + "...") if len(body) > 200 else body
        return {
            "item_type": msg.get("item_type"),
            "next_action": msg.get("next_action"),
            "repo_id": msg.get("repo_id"),
            "repo_display_name": msg.get("repo_display_name"),
            "run_id": msg.get("run_id"),
            "run_created_at": msg.get("run_created_at"),
            "status": msg.get("status"),
            "seq": msg.get("seq"),
            "dispatch": {
                "mode": dispatch.get("mode"),
                "title": title,
                "body": truncated_body,
                "is_handoff": dispatch.get("is_handoff"),
            },
            "files_count": (
                len(msg.get("files", [])) if isinstance(msg.get("files"), list) else 0
            ),
        }

    snapshot = {
        "last_scan_at": (
            repos_payload.get("last_scan_at")
            if isinstance(repos_payload, dict)
            else None
        ),
        "repos": [_summarize_repo(repo) for repo in repos],
        "inbox_items": [_summarize_message(msg) for msg in messages_items],
    }

    if not output_json:
        typer.echo(
            f"Hub Snapshot (repos={len(snapshot['repos'])}, inbox={len(snapshot['inbox_items'])})"
        )
        for repo in snapshot["repos"]:
            pr_url = repo.get("pr_url")
            final_review_status = repo.get("final_review_status")
            typer.echo(
                f"- {repo.get('id')}: status={repo.get('status')}, "
                f"initialized={repo.get('initialized')}, exists={repo.get('exists_on_disk')}, "
                f"final_review={final_review_status}, pr_url={pr_url}"
            )
        for msg in snapshot["inbox_items"]:
            typer.echo(
                f"- Inbox: repo={msg.get('repo_id')}, run_id={msg.get('run_id')}, "
                f"title={msg.get('dispatch', {}).get('title')}"
            )
        return

    indent = 2 if pretty else None
    typer.echo(json.dumps(snapshot, indent=indent))


def _print_ticket_import_report(report) -> None:
    typer.echo(f"Repo: {report.repo_id}")
    typer.echo(f"Ticket dir: {report.ticket_dir}")
    typer.echo(f"Zip: {report.zip_path}")
    typer.echo(f"Dry run: {report.dry_run}")
    if report.renumber:
        typer.echo(
            f"Renumber: start={report.renumber.get('start')}, step={report.renumber.get('step')}"
        )
    if report.assign_agent:
        typer.echo(f"Assign agent: {report.assign_agent}")
    if report.clear_model_pin:
        typer.echo("Clear model pin: true")
    if report.apply_template:
        typer.echo(f"Template: {report.apply_template}")
    if getattr(report, "strip_depends_on", False):
        typer.echo("Strip depends_on: true")
    depends_summary = getattr(report, "depends_on_summary", None)
    if isinstance(depends_summary, dict) and depends_summary.get("has_depends_on"):
        typer.echo(
            f"Depends_on: mode={depends_summary.get('reconcile_mode')} "
            f"tickets={depends_summary.get('tickets_with_depends_on')} "
            f"edges={depends_summary.get('dependency_edges')} "
            f"reconciled={bool(depends_summary.get('reconciled'))}"
        )
        for warning in depends_summary.get("ordering_conflicts", []) or []:
            typer.echo(f"  Ordering impact: {warning}")
        for warning in depends_summary.get("ambiguous_reasons", []) or []:
            typer.echo(f"  Depends_on warning: {warning}")
    if report.lint:
        typer.echo("Lint: enabled")
    if report.errors:
        typer.echo("Errors:")
        for err in report.errors:
            typer.echo(f"- {err}")
    if report.lint_errors:
        typer.echo("Lint errors:")
        for err in report.lint_errors:
            typer.echo(f"- {err}")
    typer.echo(f"Tickets ready: {report.created}")
    for item in report.items:
        status = item.status.upper()
        target = item.target or "-"
        typer.echo(f"- {status}: {item.source} -> {target}")
        for err in item.errors:
            typer.echo(f"    {err}")
        for warning in getattr(item, "warnings", []) or []:
            typer.echo(f"    Warning: {warning}")


def _print_ticket_bulk_report(
    *,
    repo_id: str,
    ticket_dir: Path,
    action: str,
    updated: int,
    skipped: int,
    errors: list[str],
    lint_errors: list[str],
) -> None:
    typer.echo(f"Repo: {repo_id}")
    typer.echo(f"Ticket dir: {ticket_dir}")
    typer.echo(f"Action: {action}")
    typer.echo(f"Updated: {updated}")
    typer.echo(f"Skipped: {skipped}")
    if errors:
        typer.echo("Errors:")
        for err in errors:
            typer.echo(f"- {err}")
    if lint_errors:
        typer.echo("Lint errors:")
        for err in lint_errors:
            typer.echo(f"- {err}")


def _print_ticket_doctor_report(action: str, report, *, check_mode: bool) -> None:
    typer.echo(f"Action: {action}")
    typer.echo(f"Checked: {report.checked}")
    typer.echo(f"Changed: {report.changed}")
    if report.changed_files:
        typer.echo("Changed files:")
        for rel in report.changed_files:
            typer.echo(f"- {rel}")
    if report.warnings:
        typer.echo("Warnings:")
        for warning in report.warnings:
            typer.echo(f"- {warning}")
    if report.errors:
        typer.echo("Errors:")
        for err in report.errors:
            typer.echo(f"- {err}")
    if check_mode and report.changed:
        typer.echo("Check mode detected formatting/doctor drift.")


def _render_ticket_markdown(frontmatter: dict, body: str) -> str:
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False).rstrip()
    return f"---\n{fm_yaml}\n---\n\n{body.rstrip()}\n"


def _append_setup_pack_final_tickets(
    *,
    ticket_dir: Path,
    review_agent: str,
    pr_agent: str,
) -> list[str]:
    ticket_dir.mkdir(parents=True, exist_ok=True)
    existing_indices = _collect_ticket_indices(ticket_dir)
    next_index = (max(existing_indices) + 1) if existing_indices else 1
    width = max(
        3,
        len(str(next_index)),
        len(str(next_index + 1)),
        *(len(str(i)) for i in existing_indices),
    )

    review_path = ticket_dir / f"TICKET-{next_index:0{width}d}-final-review.md"
    review_frontmatter = {
        "agent": review_agent,
        "done": False,
        "title": "Final review",
        "goal": "Review the implementation for regressions, risks, and test coverage.",
        "ticket_kind": "final_review",
    }
    review_body = (
        "Run a focused review pass and summarize findings by severity.\n"
        "If no findings exist, state that explicitly and note residual risks/testing gaps."
    )
    atomic_write(review_path, _render_ticket_markdown(review_frontmatter, review_body))

    pr_path = ticket_dir / f"TICKET-{next_index + 1:0{width}d}-open-pr.md"
    pr_frontmatter = {
        "agent": pr_agent,
        "done": False,
        "title": "Open PR",
        "goal": "Push the branch and open/update the PR with implementation notes.",
        "ticket_kind": "open_pr",
    }
    pr_body = (
        "Open or update the pull request.\n"
        "Add `pr_url` to this ticket frontmatter after the PR exists so hub summaries can surface it."
    )
    atomic_write(pr_path, _render_ticket_markdown(pr_frontmatter, pr_body))

    return [
        str(review_path.relative_to(ticket_dir.parent)),
        str(pr_path.relative_to(ticket_dir.parent)),
    ]


@hub_tickets_app.command("import")
def hub_tickets_import(
    repo_id: str = typer.Option(..., "--repo", help="Hub repo id"),
    zip_path: Path = typer.Option(..., "--zip", help="Path to ticket pack zip"),
    renumber: Optional[str] = typer.Option(
        None, "--renumber", help="Renumber tickets with start=<n>,step=<n>"
    ),
    assign_agent: Optional[str] = typer.Option(
        None, "--assign-agent", help="Override ticket frontmatter agent"
    ),
    clear_model_pin: bool = typer.Option(
        False, "--clear-model-pin", help="Clear model/reasoning overrides"
    ),
    apply_template: Optional[str] = typer.Option(
        None, "--apply-template", help="Template ref REPO:PATH[@REF]"
    ),
    strip_depends_on: bool = typer.Option(
        True,
        "--strip-depends-on/--no-strip-depends-on",
        help="Remove unsupported frontmatter.depends_on keys from imported tickets",
    ),
    reconcile_depends_on: str = typer.Option(
        "warn",
        "--reconcile-depends-on",
        help="depends_on reconciliation mode: off, warn, auto",
    ),
    lint: bool = typer.Option(
        True, "--lint/--no-lint", help="Lint destination tickets (default on)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Import a zip ticket pack into the queue."""
    config = _require_hub_config(hub)
    repo_root = _resolve_hub_repo_root(config, repo_id)

    if assign_agent:
        if assign_agent != "user":
            try:
                validate_agent_id(assign_agent)
            except ValueError as exc:
                _raise_exit(str(exc), cause=exc)

    renumber_parsed = _parse_renumber(renumber)

    if not zip_path.exists():
        _raise_exit(f"Zip path does not exist: {zip_path}")
    if zip_path.is_dir():
        _raise_exit("Zip path must be a file.")

    template_frontmatter = None
    if apply_template:
        ctx = _require_repo_config(repo_root, config.root)
        _require_templates_enabled(ctx.config)
        fetched, _scan_record, _hub_root = _fetch_template_with_scan(
            apply_template, ctx, config.root
        )
        try:
            template_frontmatter = load_template_frontmatter(fetched.content)
        except TicketPackImportError as exc:
            _raise_exit(str(exc), cause=exc)

    report = import_ticket_pack(
        repo_id=repo_id,
        repo_root=repo_root,
        ticket_dir=repo_root / ".codex-autorunner" / "tickets",
        zip_path=zip_path,
        renumber=renumber_parsed,
        assign_agent=assign_agent,
        clear_model_pin=clear_model_pin,
        template_ref=apply_template,
        template_frontmatter=template_frontmatter,
        lint=lint,
        dry_run=dry_run,
        strip_depends_on=strip_depends_on,
        reconcile_depends_on=reconcile_depends_on,
    )

    if output_json:
        typer.echo(json.dumps(report.to_dict(), indent=2))
    else:
        _print_ticket_import_report(report)

    if not report.ok():
        _raise_exit("Ticket import failed.")


@hub_tickets_app.command("bulk-set")
def hub_tickets_bulk_set(
    repo_id: str = typer.Option(..., "--repo", help="Hub repo id"),
    agent: str = typer.Option(..., "--agent", help="Agent id to set on tickets"),
    range_spec: Optional[str] = typer.Option(
        None, "--range", help="Range of ticket indices in the form A:B"
    ),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Bulk set agent for tickets in a repo queue."""
    config = _require_hub_config(hub)
    repo_root = _resolve_hub_repo_root(config, repo_id)
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"

    if agent != "user":
        try:
            validate_agent_id(agent)
        except ValueError as exc:
            _raise_exit(str(exc), cause=exc)

    try:
        result = bulk_set_agent(
            ticket_dir,
            agent,
            range_spec,
            repo_root=repo_root,
        )
    except ValueError as exc:
        _raise_exit(str(exc), cause=exc)

    lint_errors = _validate_tickets(ticket_dir)
    _print_ticket_bulk_report(
        repo_id=repo_id,
        ticket_dir=ticket_dir,
        action="bulk-set-agent",
        updated=result.updated,
        skipped=result.skipped,
        errors=result.errors,
        lint_errors=lint_errors,
    )

    if result.errors or lint_errors:
        _raise_exit("Ticket bulk update failed.")


@hub_tickets_app.command("bulk-clear-model")
def hub_tickets_bulk_clear_model(
    repo_id: str = typer.Option(..., "--repo", help="Hub repo id"),
    range_spec: Optional[str] = typer.Option(
        None, "--range", help="Range of ticket indices in the form A:B"
    ),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Bulk clear model/reasoning overrides for tickets in a repo queue."""
    config = _require_hub_config(hub)
    repo_root = _resolve_hub_repo_root(config, repo_id)
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"

    try:
        result = bulk_clear_model_pin(
            ticket_dir,
            range_spec,
            repo_root=repo_root,
        )
    except ValueError as exc:
        _raise_exit(str(exc), cause=exc)

    lint_errors = _validate_tickets(ticket_dir)
    _print_ticket_bulk_report(
        repo_id=repo_id,
        ticket_dir=ticket_dir,
        action="bulk-clear-model",
        updated=result.updated,
        skipped=result.skipped,
        errors=result.errors,
        lint_errors=lint_errors,
    )

    if result.errors or lint_errors:
        _raise_exit("Ticket bulk update failed.")


@hub_tickets_app.command("fmt")
def hub_tickets_fmt(
    repo_id: str = typer.Option(..., "--repo", help="Hub repo id"),
    check: bool = typer.Option(
        False, "--check", help="Check only (non-zero when files would change)"
    ),
    default_agent: str = typer.Option(
        "codex", "--default-agent", help="Fallback agent for missing agent key"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Normalize ticket frontmatter formatting."""
    config = _require_hub_config(hub)
    repo_root = _resolve_hub_repo_root(config, repo_id)
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"

    report = format_or_doctor_tickets(
        ticket_dir,
        write=not check,
        fill_defaults=False,
        default_agent=default_agent,
    )

    if output_json:
        payload = report.to_dict()
        payload["action"] = "fmt"
        payload["check"] = check
        typer.echo(json.dumps(payload, indent=2))
    else:
        _print_ticket_doctor_report("fmt", report, check_mode=check)

    if report.errors or (check and report.changed):
        _raise_exit("Ticket fmt failed.")


@hub_tickets_app.command("doctor")
def hub_tickets_doctor(
    repo_id: str = typer.Option(..., "--repo", help="Hub repo id"),
    fix: bool = typer.Option(
        False, "--fix", help="Apply auto-fixes for common frontmatter issues"
    ),
    default_agent: str = typer.Option(
        "codex", "--default-agent", help="Fallback agent for missing agent key"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """Validate ticket frontmatter and optionally apply auto-fixes."""
    config = _require_hub_config(hub)
    repo_root = _resolve_hub_repo_root(config, repo_id)
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"

    report = format_or_doctor_tickets(
        ticket_dir,
        write=fix,
        fill_defaults=fix,
        default_agent=default_agent,
    )

    if output_json:
        payload = report.to_dict()
        payload["action"] = "doctor"
        payload["fix"] = fix
        typer.echo(json.dumps(payload, indent=2))
    else:
        _print_ticket_doctor_report("doctor", report, check_mode=not fix)

    if report.errors or (not fix and report.changed):
        _raise_exit("Ticket doctor failed.")


@hub_tickets_app.command("setup-pack")
def hub_tickets_setup_pack(
    base_repo_id: str = typer.Option(..., "--base-repo", help="Base repo id"),
    branch: str = typer.Option(..., "--branch", help="Branch name for worktree"),
    zip_path: Path = typer.Option(..., "--zip", help="Path to ticket pack zip"),
    renumber: Optional[str] = typer.Option(
        None, "--renumber", help="Renumber tickets with start=<n>,step=<n>"
    ),
    assign_agent: Optional[str] = typer.Option(
        None, "--assign-agent", help="Override ticket frontmatter agent"
    ),
    clear_model_pin: bool = typer.Option(
        False, "--clear-model-pin", help="Clear model/reasoning overrides"
    ),
    apply_template: Optional[str] = typer.Option(
        None, "--apply-template", help="Template ref REPO:PATH[@REF]"
    ),
    reconcile_depends_on: str = typer.Option(
        "auto",
        "--reconcile-depends-on",
        help="depends_on reconciliation mode: off, warn, auto",
    ),
    final_review_agent: str = typer.Option(
        "codex", "--final-review-agent", help="Agent for final review ticket"
    ),
    pr_agent: str = typer.Option(
        "codex", "--pr-agent", help="Agent for open PR ticket"
    ),
    start_point: Optional[str] = typer.Option(
        None, "--start-point", help="Optional git ref for worktree branch"
    ),
    force: bool = typer.Option(False, "--force", help="Allow existing worktree path"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
):
    """One-command setup for ticket packs (worktree + import + assign + finalize + preflight)."""
    config = _require_hub_config(hub)
    if not zip_path.exists():
        _raise_exit(f"Zip path does not exist: {zip_path}")
    if zip_path.is_dir():
        _raise_exit("Zip path must be a file.")

    if assign_agent and assign_agent != "user":
        try:
            validate_agent_id(assign_agent)
        except ValueError as exc:
            _raise_exit(str(exc), cause=exc)
    if final_review_agent != "user":
        try:
            validate_agent_id(final_review_agent)
        except ValueError as exc:
            _raise_exit(str(exc), cause=exc)
    if pr_agent != "user":
        try:
            validate_agent_id(pr_agent)
        except ValueError as exc:
            _raise_exit(str(exc), cause=exc)

    supervisor = HubSupervisor(
        config,
        backend_factory_builder=build_agent_backend_factory,
        app_server_supervisor_factory_builder=build_app_server_supervisor_factory,
        backend_orchestrator_builder=build_backend_orchestrator,
        agent_id_validator=validate_agent_id,
    )
    try:
        snapshot = supervisor.create_worktree(
            base_repo_id=base_repo_id,
            branch=branch,
            force=force,
            start_point=start_point,
        )
    except Exception as exc:
        _raise_exit(str(exc), cause=exc)

    repo_id = snapshot.id
    repo_root = snapshot.path
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    renumber_parsed = _parse_renumber(renumber)

    template_frontmatter = None
    if apply_template:
        ctx = _require_repo_config(repo_root, config.root)
        _require_templates_enabled(ctx.config)
        fetched, _scan_record, _hub_root = _fetch_template_with_scan(
            apply_template, ctx, config.root
        )
        try:
            template_frontmatter = load_template_frontmatter(fetched.content)
        except TicketPackImportError as exc:
            _raise_exit(str(exc), cause=exc)

    import_report = import_ticket_pack(
        repo_id=repo_id,
        repo_root=repo_root,
        ticket_dir=ticket_dir,
        zip_path=zip_path,
        renumber=renumber_parsed,
        assign_agent=assign_agent,
        clear_model_pin=clear_model_pin,
        template_ref=apply_template,
        template_frontmatter=template_frontmatter,
        lint=True,
        dry_run=False,
        strip_depends_on=True,
        reconcile_depends_on=reconcile_depends_on,
    )

    final_tickets: list[str] = []
    if import_report.ok():
        final_tickets = _append_setup_pack_final_tickets(
            ticket_dir=ticket_dir,
            review_agent=final_review_agent,
            pr_agent=pr_agent,
        )

    lint_errors = _validate_tickets(ticket_dir)
    engine = _require_repo_config(repo_root, config.root)
    preflight = _ticket_flow_preflight(engine, ticket_dir)

    payload = {
        "repo_id": repo_id,
        "repo_root": str(repo_root),
        "worktree_of": base_repo_id,
        "branch": branch,
        "zip_path": str(zip_path),
        "import": import_report.to_dict(),
        "final_tickets": final_tickets,
        "lint_errors": lint_errors,
        "preflight": preflight.to_dict(),
    }

    if output_json:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(
            f"Setup pack: repo={repo_id} branch={branch} base={base_repo_id} zip={zip_path}"
        )
        _print_ticket_import_report(import_report)
        if final_tickets:
            typer.echo("Final tickets:")
            for rel in final_tickets:
                typer.echo(f"- {rel}")
        if lint_errors:
            typer.echo("Lint errors:")
            for err in lint_errors:
                typer.echo(f"- {err}")
        typer.echo("Preflight:")
        _print_preflight_report(preflight)

    if (not import_report.ok()) or lint_errors:
        _raise_exit("Ticket setup-pack failed.")


@dispatch_app.command("reply")
def hub_dispatch_reply(
    repo_id: str = typer.Option(..., "--repo-id", help="Hub repo id"),
    run_id: str = typer.Option(..., "--run-id", help="Flow run id (UUID)"),
    message: Optional[str] = typer.Option(None, "--message", help="Reply message body"),
    message_file: Optional[Path] = typer.Option(
        None, "--message-file", help="Read reply message body from file"
    ),
    resume: bool = typer.Option(
        True, "--resume/--no-resume", help="Resume run after posting reply"
    ),
    idempotency_key: Optional[str] = typer.Option(
        None, "--idempotency-key", help="Optional key to avoid duplicate replies"
    ),
    path: Optional[Path] = typer.Option(None, "--path", "--hub", help="Hub root path"),
    base_path: Optional[str] = typer.Option(
        None, "--base-path", help="Override hub server base path (e.g. /car)"
    ),
    output_json: bool = typer.Option(
        True, "--json/--no-json", help="Emit JSON output (default: true)"
    ),
    pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output"),
):
    """Reply to a paused dispatch and optionally resume the run."""
    config = _require_hub_config(path)

    if bool(message) == bool(message_file):
        _raise_exit("Provide exactly one of --message or --message-file.")

    raw_message = message
    if message_file is not None:
        try:
            raw_message = message_file.read_text(encoding="utf-8")
        except OSError as exc:
            _raise_exit(f"Failed to read message file: {exc}", cause=exc)
    body = (raw_message or "").strip()
    if not body:
        _raise_exit("Reply message cannot be empty.")

    thread_url = _build_server_url(
        config,
        f"/repos/{repo_id}/api/messages/threads/{run_id}",
        base_path_override=base_path,
    )
    reply_url = _build_server_url(
        config,
        f"/repos/{repo_id}/api/messages/{run_id}/reply",
        base_path_override=base_path,
    )
    resume_url = _build_server_url(
        config,
        f"/repos/{repo_id}/api/flows/{run_id}/resume",
        base_path_override=base_path,
    )
    inbox_url = _build_server_url(
        config, "/hub/messages?limit=200", base_path_override=base_path
    )

    marker = None
    if idempotency_key:
        marker = f"<!-- car-idempotency-key:{idempotency_key.strip()} -->"

    try:
        thread = _request_json(
            "GET", thread_url, token_env=config.server_auth_token_env
        )
    except (
        httpx.HTTPError,
        httpx.ConnectError,
        httpx.TimeoutException,
        OSError,
    ) as exc:
        _raise_exit(
            "Failed to query run thread via hub server. Ensure 'car hub serve' is running.\n"
            f"Attempted: {thread_url}\n"
            "If the hub UI is served under a base path (commonly /car), either set "
            "`server.base_path` in the hub config or pass `--base-path /car`.",
            cause=exc,
        )

    run_status = ((thread.get("run") or {}) if isinstance(thread, dict) else {}).get(
        "status"
    )
    if run_status != "paused":
        fallback_status = None
        try:
            inbox = _request_json(
                "GET", inbox_url, token_env=config.server_auth_token_env
            )
            items = inbox.get("items", []) if isinstance(inbox, dict) else []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("repo_id") or "") != repo_id:
                    continue
                if str(item.get("run_id") or "") != run_id:
                    continue
                fallback_status = item.get("status")
                break
        except Exception:
            fallback_status = None

        if run_status is None and fallback_status == "paused":
            run_status = "paused"
        else:
            hint = ""
            if fallback_status is not None and fallback_status != run_status:
                hint = f" (hub inbox sees status={fallback_status})"
            _raise_exit(
                f"Run {run_id} is not paused-awaiting-input (status={run_status or 'unknown'}).{hint}"
            )

    duplicate = False
    reply_seq = None
    if marker:
        replies = thread.get("reply_history", []) if isinstance(thread, dict) else []
        for entry in replies if isinstance(replies, list) else []:
            reply = entry.get("reply") if isinstance(entry, dict) else None
            existing_body = (reply.get("body") or "") if isinstance(reply, dict) else ""
            if marker in existing_body:
                duplicate = True
                reply_seq = entry.get("seq") if isinstance(entry, dict) else None
                break

    if not duplicate:
        post_body = body
        if marker:
            post_body = f"{body}\n\n{marker}"
        try:
            reply_resp = _request_form_json(
                "POST",
                reply_url,
                form={"body": post_body},
                token_env=config.server_auth_token_env,
                force_multipart=True,
            )
            reply_seq = reply_resp.get("seq")
        except (
            httpx.HTTPError,
            httpx.ConnectError,
            httpx.TimeoutException,
            OSError,
        ) as exc:
            _raise_exit("Failed to post dispatch reply.", cause=exc)

    resumed = False
    resume_status = None
    if resume:
        try:
            resume_resp = _request_json(
                "POST", resume_url, payload={}, token_env=config.server_auth_token_env
            )
            resumed = True
            resume_status = resume_resp.get("status")
        except (
            httpx.HTTPError,
            httpx.ConnectError,
            httpx.TimeoutException,
            OSError,
        ) as exc:
            _raise_exit("Reply posted but resume failed.", cause=exc)

    payload = {
        "repo_id": repo_id,
        "run_id": run_id,
        "reply_seq": reply_seq,
        "duplicate": duplicate,
        "resumed": resumed,
        "resume_status": resume_status,
    }

    if output_json:
        typer.echo(json.dumps(payload, indent=2 if pretty else None))
        return

    typer.echo(
        f"Reply {'reused' if duplicate else 'posted'} for run {run_id}"
        + (f" (seq={reply_seq})" if reply_seq else "")
    )
    if resume:
        typer.echo(f"Run resumed: status={resume_status or 'unknown'}")


@telegram_app.command("start")
def telegram_start(
    path: Optional[Path] = typer.Option(None, "--path", help="Repo or hub root path"),
):
    """Start the Telegram bot (polling)."""
    _require_optional_feature(
        feature="telegram",
        deps=[("httpx", "httpx")],
        extra="telegram",
    )
    try:
        config = load_hub_config(path or Path.cwd())
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    telegram_cfg = TelegramBotConfig.from_raw(
        config.raw.get("telegram_bot") if isinstance(config.raw, dict) else None,
        root=config.root,
        agent_binaries=getattr(config, "agents", None)
        and {name: agent.binary for name, agent in config.agents.items()},
    )
    if not telegram_cfg.enabled:
        _raise_exit("telegram_bot is disabled; set telegram_bot.enabled: true")
    try:
        telegram_cfg.validate()
    except TelegramBotConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    logger = setup_rotating_logger("codex-autorunner-telegram", config.log)
    env_overrides = collect_env_overrides(env=os.environ, include_telegram=True)
    if env_overrides:
        logger.info("Environment overrides active: %s", ", ".join(env_overrides))
    log_event(
        logger,
        logging.INFO,
        "telegram.bot.starting",
        root=str(config.root),
        mode="hub",
    )
    voice_raw = config.repo_defaults.get("voice") if config.repo_defaults else None
    voice_config = VoiceConfig.from_raw(voice_raw, env=os.environ)
    update_repo_url = config.update_repo_url
    update_repo_ref = config.update_repo_ref

    async def _run() -> None:
        service = TelegramBotService(
            telegram_cfg,
            logger=logger,
            hub_root=config.root,
            manifest_path=config.manifest_path,
            voice_config=voice_config,
            housekeeping_config=config.housekeeping,
            update_repo_url=update_repo_url,
            update_repo_ref=update_repo_ref,
            update_skip_checks=config.update_skip_checks,
            app_server_auto_restart=config.app_server.auto_restart,
        )
        await service.run_polling()

    try:
        asyncio.run(_run())
    except TelegramBotLockError as exc:
        _raise_exit(str(exc), cause=exc)


@telegram_app.command("health")
def telegram_health(
    path: Optional[Path] = typer.Option(None, "--path", help="Repo or hub root path"),
    timeout: float = typer.Option(5.0, "--timeout", help="Timeout (seconds)"),
):
    """Check Telegram API connectivity for the configured bot."""
    _require_optional_feature(
        feature="telegram",
        deps=[("httpx", "httpx")],
        extra="telegram",
    )
    try:
        config = load_hub_config(path or Path.cwd())
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    telegram_cfg = TelegramBotConfig.from_raw(
        config.raw.get("telegram_bot") if isinstance(config.raw, dict) else None,
        root=config.root,
        agent_binaries=getattr(config, "agents", None)
        and {name: agent.binary for name, agent in config.agents.items()},
    )
    if not telegram_cfg.enabled:
        _raise_exit("telegram_bot is disabled; set telegram_bot.enabled: true")
    bot_token = telegram_cfg.bot_token
    if not bot_token:
        _raise_exit(f"missing bot token env '{telegram_cfg.bot_token_env}'")
    timeout_seconds = max(float(timeout), 0.1)

    async def _run() -> None:
        async with TelegramBotClient(bot_token) as client:
            await asyncio.wait_for(client.get_me(), timeout=timeout_seconds)

    try:
        asyncio.run(_run())
    except TelegramAPIError as exc:
        _raise_exit(f"Telegram health check failed: {exc}", cause=exc)


@telegram_app.command("state-check")
def telegram_state_check(
    path: Optional[Path] = typer.Option(None, "--path", help="Repo or hub root path"),
):
    """Open the Telegram state DB and ensure schema migrations apply."""
    try:
        config = load_hub_config(path or Path.cwd())
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    telegram_cfg = TelegramBotConfig.from_raw(
        config.raw.get("telegram_bot") if isinstance(config.raw, dict) else None,
        root=config.root,
        agent_binaries=getattr(config, "agents", None)
        and {name: agent.binary for name, agent in config.agents.items()},
    )
    if not telegram_cfg.enabled:
        _raise_exit("telegram_bot is disabled; set telegram_bot.enabled: true")

    try:
        store = TelegramStateStore(
            telegram_cfg.state_file,
            default_approval_mode=telegram_cfg.defaults.approval_mode,
        )
        # This will open the DB and apply schema/migrations.
        store._connection_sync()  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - defensive runtime check
        _raise_exit(f"Telegram state check failed: {exc}", cause=exc)


def _normalize_flow_run_id(run_id: Optional[str]) -> Optional[str]:
    if run_id is None:
        return None
    try:
        return str(uuid.UUID(str(run_id)))
    except ValueError:
        _raise_exit("Invalid run_id format; must be a UUID")


def _ticket_flow_paths(engine: RuntimeContext) -> tuple[Path, Path, Path]:
    db_path = engine.repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = engine.repo_root / ".codex-autorunner" / "flows"
    ticket_dir = engine.repo_root / ".codex-autorunner" / "tickets"
    return db_path, artifacts_root, ticket_dir


@dataclass(frozen=True)
class PreflightCheck:
    check_id: str
    status: str  # ok | warning | error
    message: str
    fix: Optional[str] = None
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.check_id,
            "status": self.status,
            "message": self.message,
            "fix": self.fix,
            "details": list(self.details),
        }


@dataclass(frozen=True)
class PreflightReport:
    checks: list[PreflightCheck]

    def has_errors(self) -> bool:
        return any(check.status == "error" for check in self.checks)

    def to_dict(self) -> dict:
        return {
            "ok": sum(1 for check in self.checks if check.status == "ok"),
            "warnings": sum(1 for check in self.checks if check.status == "warning"),
            "errors": sum(1 for check in self.checks if check.status == "error"),
            "checks": [check.to_dict() for check in self.checks],
        }


def _print_preflight_report(report: PreflightReport) -> None:
    for check in report.checks:
        status = check.status.upper()
        typer.echo(f"- {status}: {check.message}")
        if check.details:
            for detail in check.details:
                typer.echo(f"    {detail}")
        if check.fix:
            typer.echo(f"    Fix: {check.fix}")


def _ticket_lint_details(ticket_dir: Path) -> dict[str, list[str]]:
    details = {
        "invalid_filenames": [],
        "duplicate_indices": [],
        "frontmatter": [],
    }
    if not ticket_dir.exists():
        return details

    ticket_root = ticket_dir.parent
    for path in sorted(ticket_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name == "AGENTS.md":
            continue
        if parse_ticket_index(path.name) is None:
            rel_path = safe_relpath(path, ticket_root)
            details["invalid_filenames"].append(
                f"{rel_path}: Invalid ticket filename; expected TICKET-<number>[suffix].md (e.g. TICKET-001-foo.md)"
            )

    details["duplicate_indices"].extend(lint_ticket_directory(ticket_dir))

    ticket_paths = list_ticket_paths(ticket_dir)
    for path in ticket_paths:
        _, ticket_errors = read_ticket(path)
        for err in ticket_errors:
            details["frontmatter"].append(
                f"{path.relative_to(path.parent.parent)}: {err}"
            )

    return details


def _validate_tickets(ticket_dir: Path) -> list[str]:
    """Validate all tickets in the directory and return a list of error messages."""
    errors: list[str] = []

    if not ticket_dir.exists():
        return errors

    details = _ticket_lint_details(ticket_dir)
    errors.extend(details["invalid_filenames"])
    errors.extend(details["duplicate_indices"])
    errors.extend(details["frontmatter"])

    return errors


def _ticket_flow_preflight(engine: RuntimeContext, ticket_dir: Path) -> PreflightReport:
    checks: list[PreflightCheck] = []

    state_root = engine.repo_root / ".codex-autorunner"
    if state_root.exists():
        checks.append(
            PreflightCheck(
                check_id="repo_initialized",
                status="ok",
                message="Repo initialized (.codex-autorunner present).",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="repo_initialized",
                status="error",
                message="Repo not initialized (.codex-autorunner missing).",
                fix="Run `car init` in the repo root.",
            )
        )

    if ticket_dir.exists():
        checks.append(
            PreflightCheck(
                check_id="ticket_dir",
                status="ok",
                message=f"Ticket directory found: {ticket_dir.relative_to(engine.repo_root)}.",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="ticket_dir",
                status="error",
                message="Ticket directory missing.",
                fix="Run `car flow ticket_flow bootstrap` to create the ticket dir and seed TICKET-001.",
            )
        )

    ticket_paths = list_ticket_paths(ticket_dir)
    if ticket_paths:
        checks.append(
            PreflightCheck(
                check_id="tickets_present",
                status="ok",
                message=f"Found {len(ticket_paths)} ticket(s).",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="tickets_present",
                status="error",
                message="No tickets found.",
                fix="Create tickets under .codex-autorunner/tickets or run `car flow ticket_flow bootstrap`.",
            )
        )

    lint_details = _ticket_lint_details(ticket_dir)
    if lint_details["invalid_filenames"]:
        checks.append(
            PreflightCheck(
                check_id="ticket_filenames",
                status="error",
                message="Invalid ticket filenames detected.",
                fix="Rename tickets to TICKET-<number>[suffix].md (e.g. TICKET-001-foo.md).",
                details=lint_details["invalid_filenames"],
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="ticket_filenames",
                status="ok",
                message="Ticket filenames are valid.",
            )
        )

    if lint_details["duplicate_indices"]:
        checks.append(
            PreflightCheck(
                check_id="duplicate_indices",
                status="error",
                message="Duplicate ticket indices detected.",
                fix="Rename or remove duplicates so each index is unique.",
                details=lint_details["duplicate_indices"],
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="duplicate_indices",
                status="ok",
                message="Ticket indices are unique.",
            )
        )

    if lint_details["frontmatter"]:
        checks.append(
            PreflightCheck(
                check_id="frontmatter",
                status="error",
                message="Ticket frontmatter validation failed.",
                fix="Fix the YAML frontmatter in the listed tickets.",
                details=lint_details["frontmatter"],
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="frontmatter",
                status="ok",
                message="Ticket frontmatter passes validation.",
            )
        )

    ticket_docs = []
    for path in ticket_paths:
        doc, errors = read_ticket(path)
        if doc is not None and not errors:
            ticket_docs.append(doc)

    if ticket_docs:
        agents = sorted({doc.frontmatter.agent for doc in ticket_docs})
        agent_errors: list[str] = []
        agent_warnings: list[str] = []

        if "codex" in agents:
            app_cmd = engine.config.app_server.command or []
            app_binary = app_cmd[0] if app_cmd else None
            resolved = resolve_executable(app_binary) if app_binary else None
            if not resolved:
                agent_errors.append("codex: app_server command not available in PATH.")

        if "opencode" in agents:
            opencode_cmd = engine.config.agent_serve_command("opencode")
            opencode_binary: Optional[str] = None
            if opencode_cmd:
                opencode_binary = resolve_executable(opencode_cmd[0])
            if not opencode_binary:
                try:
                    opencode_binary = resolve_executable(
                        engine.config.agent_binary("opencode")
                    )
                except ConfigError:
                    opencode_binary = None
            if not opencode_binary:
                agent_errors.append(
                    "opencode: backend unavailable (missing binary/serve command)."
                )

        for agent in agents:
            if agent in ("codex", "opencode", "user"):
                continue
            agent_warnings.append(
                f"{agent}: availability not verified; ensure its backend is configured."
            )

        if agent_errors:
            checks.append(
                PreflightCheck(
                    check_id="agents",
                    status="error",
                    message="One or more agents are unavailable.",
                    fix="Install missing agents or update agents.<id>.binary/serve_command in config.",
                    details=agent_errors,
                )
            )
        elif agent_warnings:
            checks.append(
                PreflightCheck(
                    check_id="agents",
                    status="warning",
                    message="Agents detected but availability could not be verified.",
                    details=agent_warnings,
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    check_id="agents",
                    status="ok",
                    message="All referenced agents appear available.",
                )
            )
    else:
        checks.append(
            PreflightCheck(
                check_id="agents",
                status="warning",
                message="Agent availability skipped (no valid tickets to inspect).",
            )
        )

    return PreflightReport(checks=checks)


def _open_flow_store(engine: RuntimeContext) -> FlowStore:
    db_path, _, _ = _ticket_flow_paths(engine)
    store = FlowStore(db_path, durable=engine.config.durable_writes)
    store.initialize()
    return store


def _active_or_paused_run(records: list[FlowRunRecord]) -> Optional[FlowRunRecord]:
    if not records:
        return None
    latest = records[0]
    if latest.status in (FlowRunStatus.RUNNING, FlowRunStatus.PAUSED):
        return latest
    return None


def _resumable_run(records: list[FlowRunRecord]) -> tuple[Optional[FlowRunRecord], str]:
    """Return a resumable run and the reason.

    Returns (run, reason) where run may be None.
    Reason is one of: 'active', 'completed_pending', 'force_new', 'new_run'.
    """
    if not records:
        return None, "new_run"
    latest = records[0]
    if latest.status in (FlowRunStatus.RUNNING, FlowRunStatus.PAUSED):
        return latest, "active"
    if latest.status == FlowRunStatus.COMPLETED:
        return latest, "completed_pending"
    return None, "new_run"


def _ticket_flow_status_payload(
    engine: RuntimeContext, record: FlowRunRecord, store: Optional[FlowStore]
) -> dict:
    snapshot = build_flow_status_snapshot(engine.repo_root, record, store)
    health = snapshot.get("worker_health")
    effective_ticket = snapshot.get("effective_current_ticket")
    return {
        "run_id": record.id,
        "flow_type": record.flow_type,
        "status": record.status.value,
        "current_step": record.current_step,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "last_event_seq": snapshot.get("last_event_seq"),
        "last_event_at": snapshot.get("last_event_at"),
        "current_ticket": effective_ticket,
        "ticket_progress": snapshot.get("ticket_progress"),
        "worker": (
            {
                "status": health.status,
                "pid": health.pid,
                "message": health.message,
                "exit_code": getattr(health, "exit_code", None),
                "stderr_tail": getattr(health, "stderr_tail", None),
            }
            if health
            else None
        ),
    }


def _print_ticket_flow_status(payload: dict) -> None:
    typer.echo(f"Run id: {payload.get('run_id')}")
    typer.echo(f"Status: {payload.get('status')}")
    progress = payload.get("ticket_progress") or {}
    if isinstance(progress, dict):
        done = progress.get("done")
        total = progress.get("total")
        if isinstance(done, int) and isinstance(total, int):
            typer.echo(f"Tickets: {done}/{total}")
    typer.echo(f"Current step: {payload.get('current_step')}")
    typer.echo(f"Current ticket: {payload.get('current_ticket') or 'n/a'}")
    typer.echo(f"Created at: {payload.get('created_at')}")
    typer.echo(f"Started at: {payload.get('started_at')}")
    typer.echo(f"Finished at: {payload.get('finished_at')}")
    typer.echo(
        f"Last event: {payload.get('last_event_at')} (seq={payload.get('last_event_seq')})"
    )
    worker = payload.get("worker") or {}
    status = payload.get("status") or ""
    # Only show worker details for non-terminal states
    if worker and status not in {"completed", "failed", "stopped"}:
        typer.echo(
            f"Worker: {worker.get('status')} pid={worker.get('pid')} {worker.get('message') or ''}".rstrip()
        )
    elif worker and status in {"completed", "failed", "stopped"}:
        # For terminal runs, show minimal worker info or clarify state
        worker_status = worker.get("status") or ""
        worker_pid = worker.get("pid")
        worker_msg = worker.get("message") or ""
        if worker_status == "absent" or "missing" in worker_msg.lower():
            typer.echo("Worker: exited")
        elif worker_status == "dead" or "not running" in worker_msg.lower():
            typer.echo(f"Worker: exited (pid={worker_pid})")
        else:
            typer.echo(
                f"Worker: {worker.get('status')} pid={worker.get('pid')} {worker.get('message') or ''}".rstrip()
            )
        if status == "failed":
            exit_code = worker.get("exit_code")
            stderr_tail = worker.get("stderr_tail")
            if exit_code is not None:
                typer.echo(f"Worker exit code: {exit_code}")
            if isinstance(stderr_tail, str) and stderr_tail.strip():
                typer.echo(f"Worker stderr tail: {stderr_tail.strip()}")


def _start_ticket_flow_worker(
    repo_root: Path, run_id: str, is_terminal: bool = False
) -> None:
    result = ensure_worker(repo_root, run_id, is_terminal=is_terminal)
    if result["status"] == "reused":
        return


def _stop_ticket_flow_worker(repo_root: Path, run_id: str) -> None:
    health = check_worker_health(repo_root, run_id)
    if health.status in {"dead", "mismatch", "invalid"}:
        try:
            clear_worker_metadata(health.artifact_path.parent)
        except Exception:
            pass
    if not health.pid:
        return
    try:
        subprocess.run(["kill", str(health.pid)], check=False)
    except Exception:
        pass


def _ticket_flow_controller(
    engine: RuntimeContext,
) -> tuple[FlowController, AgentPool]:
    db_path, artifacts_root, _ = _ticket_flow_paths(engine)
    agent_pool = AgentPool(engine.config)
    definition = build_ticket_flow_definition(agent_pool=agent_pool)
    definition.validate()
    controller = FlowController(
        definition=definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
        durable=engine.config.durable_writes,
    )
    controller.initialize()
    return controller, agent_pool


@flow_app.command("worker")
def flow_worker(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    run_id: Optional[str] = typer.Option(
        None, "--run-id", help="Flow run ID (required)"
    ),
):
    """Start a flow worker process for an existing run."""
    engine = _require_repo_config(repo, hub)
    normalized_run_id = _normalize_flow_run_id(run_id)
    if not normalized_run_id:
        _raise_exit("--run-id is required for worker command")

    db_path, artifacts_root, ticket_dir = _ticket_flow_paths(engine)

    typer.echo(f"Starting flow worker for run {normalized_run_id}")

    async def _run_worker():
        typer.echo(f"Flow worker started for {normalized_run_id}")
        typer.echo(f"DB path: {db_path}")
        typer.echo(f"Artifacts root: {artifacts_root}")

        store = FlowStore(db_path, durable=engine.config.durable_writes)
        store.initialize()

        record = store.get_flow_run(normalized_run_id)
        if not record:
            typer.echo(f"Flow run {normalized_run_id} not found", err=True)
            store.close()
            raise typer.Exit(code=1)

        if record.flow_type == "ticket_flow":
            report = _ticket_flow_preflight(engine, ticket_dir)
            if report.has_errors():
                typer.echo("Ticket flow preflight failed:", err=True)
                _print_preflight_report(report)
                store.close()
                raise typer.Exit(code=1)

        store.close()

        try:
            register_worker_metadata(
                engine.repo_root,
                normalized_run_id,
                artifacts_root=artifacts_root,
            )
        except Exception as exc:
            typer.echo(f"Failed to register worker metadata: {exc}", err=True)

        agent_pool: AgentPool | None = None

        def _build_definition(flow_type: str):
            nonlocal agent_pool
            if flow_type == "pr_flow":
                _raise_exit("PR flow is no longer supported. Use ticket_flow instead.")
            if flow_type == "ticket_flow":
                agent_pool = AgentPool(engine.config)
                return build_ticket_flow_definition(agent_pool=agent_pool)
            _raise_exit(f"Unknown flow type for run {normalized_run_id}: {flow_type}")
            return None

        definition = _build_definition(record.flow_type)
        definition.validate()

        controller = FlowController(
            definition=definition,
            db_path=db_path,
            artifacts_root=artifacts_root,
            durable=engine.config.durable_writes,
        )
        controller.initialize()

        record = controller.get_status(normalized_run_id)
        if not record:
            typer.echo(f"Flow run {normalized_run_id} not found", err=True)
            raise typer.Exit(code=1)

        if record.status.is_terminal() and record.status not in {
            FlowRunStatus.STOPPED,
            FlowRunStatus.FAILED,
        }:
            typer.echo(
                f"Flow run {normalized_run_id} already completed (status={record.status})"
            )
            return

        action = "Resuming" if record.status != FlowRunStatus.PENDING else "Starting"
        typer.echo(
            f"{action} flow run {normalized_run_id} from step: {record.current_step}"
        )
        try:
            final_record = await controller.run_flow(normalized_run_id)
            typer.echo(
                f"Flow run {normalized_run_id} finished with status {final_record.status}"
            )
        finally:
            if agent_pool is not None:
                try:
                    await agent_pool.close()
                except Exception:
                    typer.echo("Failed to close agent pool cleanly", err=True)

    asyncio.run(_run_worker())


@ticket_flow_app.command("bootstrap")
def ticket_flow_bootstrap(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    force_new: bool = typer.Option(
        False, "--force-new", help="Always create a new run"
    ),
):
    """Bootstrap ticket_flow (seed TICKET-001 if needed) and start a run.

    If latest run is COMPLETED and new tickets are added, a new run is created
    (use --force-new to force a new run regardless of state)."""
    engine = _require_repo_config(repo, hub)
    _guard_unregistered_hub_repo(engine.repo_root, hub)
    db_path, artifacts_root, ticket_dir = _ticket_flow_paths(engine)
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"

    store = _open_flow_store(engine)
    try:
        if not force_new:
            records = store.list_flow_runs(flow_type="ticket_flow")
            existing_run, reason = _resumable_run(records)
            if existing_run and reason == "active":
                _start_ticket_flow_worker(
                    engine.repo_root, existing_run.id, is_terminal=False
                )
                typer.echo(f"Reused active run: {existing_run.id}")
                typer.echo(
                    f"Next: car flow ticket_flow status --repo {engine.repo_root} --run-id {existing_run.id}"
                )
                return
            elif existing_run and reason == "completed_pending":
                existing_tickets = list_ticket_paths(ticket_dir)
                pending_count = len(
                    [t for t in existing_tickets if not ticket_is_done(t)]
                )
                if pending_count > 0:
                    typer.echo(
                        f"Warning: Latest run {existing_run.id} is COMPLETED with {pending_count} pending ticket(s)."
                    )
                    typer.echo(
                        "Use --force-new to start a fresh run (dispatch history will be reset)."
                    )
                    _raise_exit("Add --force-new to create a new run.")
    finally:
        store.close()

    existing_tickets = list_ticket_paths(ticket_dir)
    seeded = False
    if not existing_tickets and not ticket_path.exists():
        template = """---
agent: codex
done: false
title: Bootstrap ticket plan
goal: Capture scope and seed follow-up tickets
---

You are the first ticket in a new ticket_flow run.

- Read `.codex-autorunner/ISSUE.md`. If it is missing:
  - If GitHub is available, ask the user for the issue/PR URL or number and create `.codex-autorunner/ISSUE.md` from it.
  - If GitHub is not available, write `DISPATCH.md` with `mode: pause` asking the user to describe the work (or share a doc). After the reply, create `.codex-autorunner/ISSUE.md` with their input.
- If helpful, create or update contextspace docs under `.codex-autorunner/contextspace/`:
  - `active_context.md` for current context and links
  - `decisions.md` for decisions/rationale
  - `spec.md` for requirements and constraints
- Break the work into additional `TICKET-00X.md` files with clear owners/goals; keep this ticket open until they exist.
- Place any supporting artifacts in `.codex-autorunner/runs/<run_id>/dispatch/` if needed.
- Write `DISPATCH.md` to dispatch a message to the user:
  - Use `mode: pause` (handoff) to wait for user response. This pauses execution.
  - Use `mode: notify` (informational) to message the user but keep running.
"""
        ticket_path.write_text(template, encoding="utf-8")
        seeded = True

    controller, agent_pool = _ticket_flow_controller(engine)
    try:
        run_id = str(uuid.uuid4())
        record = asyncio.run(
            controller.start_flow(
                input_data={},
                run_id=run_id,
                metadata={"seeded_ticket": seeded},
            )
        )
        _start_ticket_flow_worker(engine.repo_root, record.id, is_terminal=False)
    finally:
        controller.shutdown()
        asyncio.run(agent_pool.close())

    typer.echo(f"Started ticket_flow run: {run_id}")
    typer.echo(
        f"Next: car flow ticket_flow status --repo {engine.repo_root} --run-id {run_id}"
    )


@ticket_flow_app.command("preflight")
def ticket_flow_preflight(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    output_json: bool = typer.Option(
        True, "--json/--no-json", help="Emit JSON output (default: true)"
    ),
):
    """Run ticket_flow preflight checks."""
    engine = _require_repo_config(repo, hub)
    _guard_unregistered_hub_repo(engine.repo_root, hub)
    _, _, ticket_dir = _ticket_flow_paths(engine)

    report = _ticket_flow_preflight(engine, ticket_dir)
    if output_json:
        typer.echo(json.dumps(report.to_dict(), indent=2))
        if report.has_errors():
            raise typer.Exit(code=1)
        return

    _print_preflight_report(report)
    if report.has_errors():
        _raise_exit("Ticket flow preflight failed.")


@ticket_flow_app.command("start")
def ticket_flow_start(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    force_new: bool = typer.Option(
        False, "--force-new", help="Always create a new run"
    ),
):
    """Start or resume the latest ticket_flow run.

    If latest run is COMPLETED and new tickets are added, a new run is created
    (use --force-new to force a new run regardless of state)."""
    engine = _require_repo_config(repo, hub)
    _guard_unregistered_hub_repo(engine.repo_root, hub)
    _, _, ticket_dir = _ticket_flow_paths(engine)
    ticket_dir.mkdir(parents=True, exist_ok=True)

    store = _open_flow_store(engine)
    try:
        if not force_new:
            records = store.list_flow_runs(flow_type="ticket_flow")
            existing_run, reason = _resumable_run(records)
            if existing_run and reason == "active":
                report = _ticket_flow_preflight(engine, ticket_dir)
                if report.has_errors():
                    typer.echo("Ticket flow preflight failed:", err=True)
                    _print_preflight_report(report)
                    _raise_exit("Fix the above errors before starting the ticket flow.")
                _start_ticket_flow_worker(
                    engine.repo_root, existing_run.id, is_terminal=False
                )
                typer.echo(f"Reused active run: {existing_run.id}")
                typer.echo(
                    f"Next: car flow ticket_flow status --repo {engine.repo_root} --run-id {existing_run.id}"
                )
                return
            elif existing_run and reason == "completed_pending":
                existing_tickets = list_ticket_paths(ticket_dir)
                pending_count = len(
                    [t for t in existing_tickets if not ticket_is_done(t)]
                )
                if pending_count > 0:
                    typer.echo(
                        f"Warning: Latest run {existing_run.id} is COMPLETED with {pending_count} pending ticket(s)."
                    )
                    typer.echo(
                        "Use --force-new to start a fresh run (dispatch history will be reset)."
                    )
                    _raise_exit("Add --force-new to create a new run.")

    finally:
        store.close()

    report = _ticket_flow_preflight(engine, ticket_dir)
    if report.has_errors():
        typer.echo("Ticket flow preflight failed:", err=True)
        _print_preflight_report(report)
        _raise_exit("Fix the above errors before starting the ticket flow.")

    controller, agent_pool = _ticket_flow_controller(engine)
    try:
        run_id = str(uuid.uuid4())
        record = asyncio.run(controller.start_flow(input_data={}, run_id=run_id))
        _start_ticket_flow_worker(engine.repo_root, record.id, is_terminal=False)
    finally:
        controller.shutdown()
        asyncio.run(agent_pool.close())

    typer.echo(f"Started ticket_flow run: {run_id}")
    typer.echo(
        f"Next: car flow ticket_flow status --repo {engine.repo_root} --run-id {run_id}"
    )


@ticket_flow_app.command("status")
def ticket_flow_status(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run ID"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
):
    """Show status for a ticket_flow run."""
    engine = _require_repo_config(repo, hub)
    normalized_run_id = _normalize_flow_run_id(run_id)

    store = _open_flow_store(engine)
    try:
        record = None
        if normalized_run_id:
            record = store.get_flow_run(normalized_run_id)
        else:
            records = store.list_flow_runs(flow_type="ticket_flow")
            record = records[0] if records else None
        if not record:
            _raise_exit("No ticket_flow runs found.")
        payload = _ticket_flow_status_payload(engine, record, store)
    finally:
        store.close()

    if output_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    _print_ticket_flow_status(payload)


@ticket_flow_app.command("resume")
def ticket_flow_resume(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run ID"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Force resume even when blocked without new reply/repo changes.",
    ),
):
    """Resume a paused ticket_flow run."""
    engine = _require_repo_config(repo, hub)
    _guard_unregistered_hub_repo(engine.repo_root, hub)
    normalized_run_id = _normalize_flow_run_id(run_id)

    store = _open_flow_store(engine)
    try:
        record = None
        if normalized_run_id:
            record = store.get_flow_run(normalized_run_id)
        else:
            records = store.list_flow_runs(flow_type="ticket_flow")
            record = records[0] if records else None
        if not record:
            _raise_exit("No ticket_flow runs found.")
        normalized_run_id = record.id
    finally:
        store.close()

    _, _, ticket_dir = _ticket_flow_paths(engine)
    report = _ticket_flow_preflight(engine, ticket_dir)
    if report.has_errors():
        typer.echo("Ticket flow preflight failed:", err=True)
        _print_preflight_report(report)
        _raise_exit("Fix the above errors before resuming the ticket flow.")

    controller, agent_pool = _ticket_flow_controller(engine)
    try:
        try:
            updated = asyncio.run(
                controller.resume_flow(normalized_run_id, force=force)
            )
        except ValueError as exc:
            _raise_exit(str(exc), cause=exc)
        _start_ticket_flow_worker(engine.repo_root, normalized_run_id)
    finally:
        controller.shutdown()
        asyncio.run(agent_pool.close())

    typer.echo(f"Resumed ticket_flow run: {updated.id}")
    typer.echo(
        f"Next: car flow ticket_flow status --repo {engine.repo_root} --run-id {updated.id}"
    )


@ticket_flow_app.command("stop")
def ticket_flow_stop(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run ID"),
):
    """Stop a ticket_flow run."""
    engine = _require_repo_config(repo, hub)
    normalized_run_id = _normalize_flow_run_id(run_id)

    store = _open_flow_store(engine)
    try:
        record = None
        if normalized_run_id:
            record = store.get_flow_run(normalized_run_id)
        else:
            records = store.list_flow_runs(flow_type="ticket_flow")
            record = records[0] if records else None
        if not record:
            _raise_exit("No ticket_flow runs found.")
        normalized_run_id = record.id
    finally:
        store.close()

    controller, agent_pool = _ticket_flow_controller(engine)
    try:
        _stop_ticket_flow_worker(engine.repo_root, normalized_run_id)
        updated = asyncio.run(controller.stop_flow(normalized_run_id))
    finally:
        controller.shutdown()
        asyncio.run(agent_pool.close())

    typer.echo(f"Stop requested for run: {updated.id} (status={updated.status.value})")


if __name__ == "__main__":
    app()
