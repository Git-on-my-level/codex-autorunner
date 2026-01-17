import asyncio
import ipaddress
import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import NoReturn, Optional

import httpx
import typer
import uvicorn

from .bootstrap import seed_hub_files, seed_repo_files
from .core.config import ConfigError, HubConfig, _normalize_base_path, load_config
from .core.engine import Engine, LockError, clear_stale_lock, doctor
from .core.git_utils import GitError, run_git
from .core.hub import HubSupervisor
from .core.logging_utils import log_event, setup_rotating_logger
from .core.optional_dependencies import require_optional_dependencies
from .core.snapshot import SnapshotError
from .core.state import RunnerState, load_state, now_iso, save_state, state_lock
from .core.usage import (
    UsageError,
    default_codex_home,
    parse_iso_datetime,
    summarize_hub_usage,
    summarize_repo_usage,
)
from .core.utils import RepoNotFoundError, default_editor, find_repo_root
from .integrations.app_server.env import build_app_server_env
from .integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from .integrations.telegram.adapter import TelegramAPIError, TelegramBotClient
from .integrations.telegram.service import (
    TelegramBotConfig,
    TelegramBotConfigError,
    TelegramBotLockError,
    TelegramBotService,
)
from .manifest import load_manifest
from .server import create_app, create_hub_app
from .spec_ingest import SpecIngestError, SpecIngestService, clear_work_docs
from .voice import VoiceConfig

app = typer.Typer(add_completion=False)
hub_app = typer.Typer(add_completion=False)
telegram_app = typer.Typer(add_completion=False)


def _raise_exit(message: str, *, cause: Optional[BaseException] = None) -> NoReturn:
    typer.echo(message, err=True)
    if cause is not None:
        raise typer.Exit(code=1) from cause
    raise typer.Exit(code=1)


def _require_repo_config(repo: Optional[Path]) -> Engine:
    try:
        config = load_config(repo or Path.cwd())
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    if config.mode != "repo":
        _raise_exit("This command must be run in repo mode (config.mode=repo).")
    return Engine(config.root)


def _require_hub_config(path: Optional[Path]) -> HubConfig:
    try:
        config = load_config(path or Path.cwd())
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    if not isinstance(config, HubConfig):
        _raise_exit("This command requires hub mode (config.mode=hub).")
    return config


def _build_server_url(config, path: str) -> str:
    base_path = config.server_base_path or ""
    if base_path.endswith("/") and path.startswith("/"):
        base_path = base_path[:-1]
    return f"http://{config.server_host}:{config.server_port}{base_path}{path}"


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
    response = httpx.request(method, url, json=payload, timeout=2.0, headers=headers)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def _require_optional_feature(
    *, feature: str, deps: list[tuple[str, str]], extra: Optional[str] = None
) -> None:
    try:
        require_optional_dependencies(feature=feature, deps=deps, extra=extra)
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)


app.add_typer(hub_app, name="hub")
app.add_typer(telegram_app, name="telegram")


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

    if selected_mode == "hub":
        seed_hub_files(target_root, force=force)
        typer.echo(f"Initialized hub at {ca_dir}")
    else:
        seed_repo_files(target_root, force=force, git_required=git_required)
        typer.echo(f"Initialized repo at {ca_dir}")
    typer.echo("Init complete")


@app.command()
def status(repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path")):
    """Show autorunner status."""
    engine = _require_repo_config(repo)
    state = load_state(engine.state_path)
    outstanding, _ = engine.docs.todos()
    repo_key = str(engine.repo_root)
    session_id = state.repo_to_session.get(repo_key) or state.repo_to_session.get(
        f"{repo_key}:codex"
    )
    opencode_session_id = state.repo_to_session.get(f"{repo_key}:opencode")
    session_record = state.sessions.get(session_id) if session_id else None
    opencode_record = (
        state.sessions.get(opencode_session_id) if opencode_session_id else None
    )
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
    typer.echo(f"Outstanding TODO items: {len(outstanding)}")


@app.command()
def sessions(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
):
    """List active terminal sessions."""
    engine = _require_repo_config(repo)
    config = engine.config
    url = _build_server_url(config, "/api/sessions")
    auth_token = _resolve_auth_token(config.server_auth_token_env)
    if auth_token:
        url = f"{url}?include_abs_paths=1"
    payload = None
    source = "server"
    try:
        payload = _request_json("GET", url, token_env=config.server_auth_token_env)
    except Exception:
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
    session_id: Optional[str] = typer.Option(
        None, "--session", help="Session id to stop"
    ),
):
    """Stop a terminal session by id or repo path."""
    engine = _require_repo_config(repo)
    config = engine.config
    payload: dict[str, str] = {}
    if session_id:
        payload["session_id"] = session_id
    else:
        payload["repo_path"] = str(engine.repo_root)

    url = _build_server_url(config, "/api/sessions/stop")
    try:
        response = _request_json(
            "POST", url, payload, token_env=config.server_auth_token_env
        )
        stopped_id = response.get("session_id", payload.get("session_id", ""))
        typer.echo(f"Stopped session {stopped_id}")
        return
    except Exception:
        pass

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
        config = load_config(repo or Path.cwd())
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)

    try:
        since_dt = parse_iso_datetime(since)
        until_dt = parse_iso_datetime(until)
    except UsageError as exc:
        _raise_exit(str(exc), cause=exc)

    codex_root = (codex_home or default_codex_home()).expanduser()

    if isinstance(config, HubConfig):
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

    engine = _require_repo_config(repo)
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
def run(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    force: bool = typer.Option(False, "--force", help="Ignore existing lock"),
):
    """Run the autorunner loop."""
    engine: Optional[Engine] = None
    try:
        engine = _require_repo_config(repo)
        engine.clear_stop_request()
        engine.acquire_lock(force=force)
        engine.run_loop()
    except (ConfigError, LockError) as exc:
        _raise_exit(str(exc), cause=exc)
    finally:
        if engine:
            try:
                engine.release_lock()
            except Exception:
                pass


@app.command()
def once(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    force: bool = typer.Option(False, "--force", help="Ignore existing lock"),
):
    """Execute a single Codex run."""
    engine: Optional[Engine] = None
    try:
        engine = _require_repo_config(repo)
        engine.clear_stop_request()
        engine.acquire_lock(force=force)
        engine.run_once()
    except (ConfigError, LockError) as exc:
        _raise_exit(str(exc), cause=exc)
    finally:
        if engine:
            try:
                engine.release_lock()
            except Exception:
                pass


@app.command()
def kill(repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path")):
    """Force-kill a running autorunner and clear stale lock/state."""
    engine = _require_repo_config(repo)
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
    once: bool = typer.Option(False, "--once", help="Resume with a single run"),
    force: bool = typer.Option(False, "--force", help="Override active lock"),
):
    """Resume a stopped/errored autorunner, clearing stale locks if needed."""
    engine: Optional[Engine] = None
    try:
        engine = _require_repo_config(repo)
        engine.clear_stop_request()
        clear_stale_lock(engine.lock_path)
        engine.acquire_lock(force=force)
        engine.run_loop(stop_after_runs=1 if once else None)
    except (ConfigError, LockError) as exc:
        _raise_exit(str(exc), cause=exc)
    finally:
        if engine:
            try:
                engine.release_lock()
            except Exception:
                pass


@app.command()
def log(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    run_id: Optional[int] = typer.Option(None, "--run", help="Show a specific run"),
    tail: Optional[int] = typer.Option(None, "--tail", help="Tail last N lines"),
):
    """Show autorunner log output."""
    engine = _require_repo_config(repo)
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
    target: str = typer.Argument(..., help="todo|progress|opinions|spec"),
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
):
    """Open one of the docs in $EDITOR."""
    engine = _require_repo_config(repo)
    config = engine.config
    key = target.lower()
    if key not in ("todo", "progress", "opinions", "spec"):
        _raise_exit("Invalid target; choose todo, progress, opinions, or spec")
    path = config.doc_path(key)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or default_editor()
    editor_parts = shlex.split(editor)
    if not editor_parts:
        editor_parts = [editor]
    typer.echo(f"Opening {path} with {' '.join(editor_parts)}")
    subprocess.run([*editor_parts, str(path)])


@app.command("ingest-spec")
def ingest_spec_cmd(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    spec: Optional[Path] = typer.Option(
        None, "--spec", help="Path to SPEC (defaults to configured docs.spec)"
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite TODO/PROGRESS/OPINIONS"
    ),
):
    """Generate TODO/PROGRESS/OPINIONS from SPEC using Codex."""
    try:
        engine = _require_repo_config(repo)
        config = engine.config
        if not config.app_server.command:
            raise SpecIngestError("app_server.command must be configured")

        async def _run_ingest() -> dict:
            logger = logging.getLogger("codex_autorunner.cli.app_server")

            def _env_builder(
                workspace_root: Path, _workspace_id: str, state_dir: Path
            ) -> dict[str, str]:
                state_dir.mkdir(parents=True, exist_ok=True)
                return build_app_server_env(
                    config.app_server.command,
                    workspace_root,
                    state_dir,
                    logger=logger,
                    event_prefix="cli",
                )

            supervisor = WorkspaceAppServerSupervisor(
                config.app_server.command,
                state_root=config.app_server.state_root,
                env_builder=_env_builder,
                logger=logger,
                max_handles=config.app_server.max_handles,
                idle_ttl_seconds=config.app_server.idle_ttl_seconds,
                request_timeout=config.app_server.request_timeout,
            )
            service = SpecIngestService(engine, app_server_supervisor=supervisor)
            try:
                await service.execute(force=force, spec_path=spec, message=None)
                return service.apply_patch()
            finally:
                await supervisor.close_all()

        docs = asyncio.run(_run_ingest())
    except (ConfigError, SpecIngestError) as exc:
        _raise_exit(str(exc), cause=exc)

    typer.echo("Ingested SPEC into TODO/PROGRESS/OPINIONS.")
    for key, content in docs.items():
        lines = len(content.splitlines())
        typer.echo(f"- {key.upper()}: {lines} lines")


@app.command("clear-docs")
def clear_docs_cmd(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Clear TODO/PROGRESS/OPINIONS to empty templates."""
    if not yes:
        confirm = input("Clear TODO/PROGRESS/OPINIONS? Type CLEAR to confirm: ").strip()
        if confirm.upper() != "CLEAR":
            _raise_exit("Aborted.")
    engine = _require_repo_config(repo)
    try:
        clear_work_docs(engine)
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    typer.echo("Cleared TODO/PROGRESS/OPINIONS.")


@app.command("doctor")
def doctor_cmd(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo or hub path"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON for scripting"),
):
    """Validate repo or hub setup."""
    try:
        report = doctor(repo or Path.cwd())
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
def snapshot(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
):
    """Generate or update `.codex-autorunner/SNAPSHOT.md`."""
    try:
        engine = _require_repo_config(repo)
        config = engine.config
        if not config.app_server.command:
            raise SnapshotError("app_server.command must be configured")

        async def _run_snapshot() -> None:
            logger = logging.getLogger("codex_autorunner.cli.app_server")

            def _env_builder(
                workspace_root: Path, _workspace_id: str, state_dir: Path
            ) -> dict[str, str]:
                state_dir.mkdir(parents=True, exist_ok=True)
                return build_app_server_env(
                    config.app_server.command,
                    workspace_root,
                    state_dir,
                    logger=logger,
                    event_prefix="cli",
                )

            supervisor = WorkspaceAppServerSupervisor(
                config.app_server.command,
                state_root=config.app_server.state_root,
                env_builder=_env_builder,
                logger=logger,
                max_handles=config.app_server.max_handles,
                idle_ttl_seconds=config.app_server.idle_ttl_seconds,
                request_timeout=config.app_server.request_timeout,
            )
            from .core.snapshot import SnapshotService

            service = SnapshotService(engine, app_server_supervisor=supervisor)
            try:
                await service.generate_snapshot()
            finally:
                await supervisor.close_all()

        asyncio.run(_run_snapshot())
    except (ConfigError, SnapshotError) as exc:
        _raise_exit(str(exc), cause=exc)
    typer.echo("Snapshot written to .codex-autorunner/SNAPSHOT.md")


@app.command()
def serve(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    host: Optional[str] = typer.Option(None, "--host", help="Host to bind"),
    port: Optional[int] = typer.Option(None, "--port", help="Port to bind"),
    base_path: Optional[str] = typer.Option(
        None, "--base-path", help="Base path for the server"
    ),
):
    """Start the web server and UI API."""
    try:
        config = load_config(repo or Path.cwd())
    except ConfigError as exc:
        _raise_exit(str(exc), cause=exc)
    if isinstance(config, HubConfig):
        bind_host = host or config.server_host
        bind_port = port or config.server_port
        normalized_base = (
            _normalize_base_path(base_path)
            if base_path is not None
            else config.server_base_path
        )
        _enforce_bind_auth(bind_host, config.server_auth_token_env)
        typer.echo(
            f"Serving hub on http://{bind_host}:{bind_port}{normalized_base or ''}"
        )
        uvicorn.run(
            create_hub_app(config.root, base_path=normalized_base),
            host=bind_host,
            port=bind_port,
            root_path="",
            access_log=config.server_access_log,
        )
        return
    engine = _require_repo_config(repo)
    normalized_base = (
        _normalize_base_path(base_path)
        if base_path is not None
        else engine.config.server_base_path
    )
    app_instance = create_app(engine.repo_root, base_path=normalized_base)
    bind_host = host or engine.config.server_host
    bind_port = port or engine.config.server_port
    _enforce_bind_auth(bind_host, engine.config.server_auth_token_env)
    typer.echo(f"Serving repo on http://{bind_host}:{bind_port}{normalized_base or ''}")
    uvicorn.run(
        app_instance,
        host=bind_host,
        port=bind_port,
        root_path="",
        access_log=engine.config.server_access_log,
    )


@hub_app.command("create")
def hub_create(
    repo_id: str = typer.Argument(..., help="Repo id to create and initialize"),
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
    """Create a new git repo under the hub and initialize codex-autorunner files."""
    config = _require_hub_config(path)
    supervisor = HubSupervisor(config)
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
    supervisor = HubSupervisor(config)
    try:
        snapshot = supervisor.clone_repo(
            git_url=git_url, repo_id=repo_id, repo_path=repo_path, force=force
        )
    except Exception as exc:
        _raise_exit(str(exc), cause=exc)
    typer.echo(
        f"Cloned repo {snapshot.id} at {snapshot.path} (status={snapshot.status.value})"
    )


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
    supervisor = HubSupervisor(config)
    snapshots = supervisor.scan()
    typer.echo(f"Scanned hub at {config.root} (repos_root={config.repos_root})")
    for snap in snapshots:
        typer.echo(
            f"- {snap.id}: {snap.status.value}, initialized={snap.initialized}, exists={snap.exists_on_disk}"
        )


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
        config = load_config(path or Path.cwd())
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
    log_event(
        logger,
        logging.INFO,
        "telegram.bot.starting",
        root=str(config.root),
        mode=("hub" if isinstance(config, HubConfig) else "repo"),
    )
    voice_raw = config.raw.get("voice") if isinstance(config.raw, dict) else None
    voice_config = VoiceConfig.from_raw(voice_raw, env=os.environ)
    update_repo_url = config.update_repo_url if isinstance(config, HubConfig) else None
    update_repo_ref = config.update_repo_ref if isinstance(config, HubConfig) else None

    async def _run() -> None:
        service = TelegramBotService(
            telegram_cfg,
            logger=logger,
            hub_root=config.root if isinstance(config, HubConfig) else None,
            manifest_path=(
                config.manifest_path if isinstance(config, HubConfig) else None
            ),
            voice_config=voice_config,
            housekeeping_config=config.housekeeping,
            update_repo_url=update_repo_url,
            update_repo_ref=update_repo_ref,
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
        config = load_config(path or Path.cwd())
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
    except Exception as exc:
        _raise_exit(f"Telegram health check failed: {exc}", cause=exc)


if __name__ == "__main__":
    app()
