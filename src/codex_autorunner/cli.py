import os
import subprocess
from pathlib import Path
from typing import Optional

import typer
import uvicorn

from .bootstrap import seed_repo_files
from .config import ConfigError, HubConfig, load_config
from .engine import Engine, LockError, clear_stale_lock, doctor
from .hub import HubSupervisor
from .server import create_app, create_hub_app
from .state import load_state, save_state, RunnerState, now_iso
from .utils import RepoNotFoundError, default_editor, find_repo_root
from .spec_ingest import (
    SpecIngestError,
    generate_docs_from_spec,
    write_ingested_docs,
    clear_work_docs,
)

app = typer.Typer(add_completion=False)
hub_app = typer.Typer(add_completion=False)


def _require_repo_config(repo: Optional[Path]) -> Engine:
    try:
        config = load_config(repo or Path.cwd())
    except ConfigError as exc:
        raise typer.Exit(str(exc))
    if config.mode != "repo":
        raise typer.Exit("This command must be run in repo mode (config.mode=repo).")
    return Engine(config.root)


def _require_hub_config(path: Optional[Path]) -> HubConfig:
    try:
        config = load_config(path or Path.cwd())
    except ConfigError as exc:
        raise typer.Exit(str(exc))
    if not isinstance(config, HubConfig):
        raise typer.Exit("This command requires hub mode (config.mode=hub).")
    return config


app.add_typer(hub_app, name="hub")


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
):
    """Initialize a repo for Codex autorunner."""
    start_path = (path or Path.cwd()).resolve()
    git_required = True
    try:
        repo_root = find_repo_root(start_path)
    except RepoNotFoundError:
        repo_root = start_path
        if git_init:
            subprocess.run(["git", "init"], cwd=repo_root, check=False)
        elif _has_nested_git(repo_root):
            git_required = False
        else:
            raise typer.Exit(
                "No .git directory found; rerun with --git-init to create one"
            )

    ca_dir = repo_root / ".codex-autorunner"
    ca_dir.mkdir(parents=True, exist_ok=True)

    seed_repo_files(repo_root, force=force, git_required=git_required)
    typer.echo(f"Initialized {ca_dir}")
    typer.echo("Init complete")


@app.command()
def status(repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path")):
    """Show autorunner status."""
    engine = _require_repo_config(repo)
    state = load_state(engine.state_path)
    outstanding, _ = engine.docs.todos()
    typer.echo(f"Repo: {engine.repo_root}")
    typer.echo(f"Status: {state.status}")
    typer.echo(f"Last run id: {state.last_run_id}")
    typer.echo(f"Last exit code: {state.last_exit_code}")
    typer.echo(f"Last start: {state.last_run_started_at}")
    typer.echo(f"Last finish: {state.last_run_finished_at}")
    typer.echo(f"Runner pid: {state.runner_pid}")
    typer.echo(f"Outstanding TODO items: {len(outstanding)}")


@app.command()
def run(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    force: bool = typer.Option(False, "--force", help="Ignore existing lock"),
):
    """Run the autorunner loop."""
    engine: Optional[Engine] = None
    try:
        engine = _require_repo_config(repo)
        engine.acquire_lock(force=force)
        engine.run_loop()
    except (ConfigError, LockError) as exc:
        raise typer.Exit(str(exc))
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
        engine.acquire_lock(force=force)
        engine.run_once()
    except (ConfigError, LockError) as exc:
        raise typer.Exit(str(exc))
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
    state = load_state(engine.state_path)
    new_state = RunnerState(
        last_run_id=state.last_run_id,
        status="error",
        last_exit_code=137,
        last_run_started_at=state.last_run_started_at,
        last_run_finished_at=now_iso(),
        runner_pid=None,
    )
    save_state(engine.state_path, new_state)
    engine.release_lock()
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
        clear_stale_lock(engine.lock_path)
        engine.acquire_lock(force=force)
        engine.run_loop(stop_after_runs=1 if once else None)
    except (ConfigError, LockError) as exc:
        raise typer.Exit(str(exc))
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
        raise typer.Exit("Log file not found; run init")

    if run_id is not None:
        _print_run_block(engine.log_path, run_id)
        return

    lines = engine.log_path.read_text(encoding="utf-8").splitlines()
    if tail is not None:
        for line in lines[-tail:]:
            typer.echo(line)
    else:
        state = load_state(engine.state_path)
        last_id = state.last_run_id
        if last_id is None:
            typer.echo("No runs recorded yet")
            return
        _print_run_block(engine.log_path, last_id)


def _print_run_block(log_path: Path, run_id: int) -> None:
    start = f"=== run {run_id} start ==="
    end = f"=== run {run_id} end"
    printing = False
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip() == start:
            printing = True
            typer.echo(line)
            continue
        if printing and line.startswith(end):
            typer.echo(line)
            break
        if printing:
            typer.echo(line)


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
        raise typer.Exit("Invalid target; choose todo, progress, opinions, or spec")
    path = config.doc_path(key)
    editor = default_editor()
    typer.echo(f"Opening {path} with {editor}")
    subprocess.run([editor, str(path)])


@app.command("ingest-spec")
def ingest_spec_cmd(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    spec: Optional[Path] = typer.Option(None, "--spec", help="Path to SPEC (defaults to configured docs.spec)"),
    force: bool = typer.Option(False, "--force", help="Overwrite TODO/PROGRESS/OPINIONS"),
):
    """Generate TODO/PROGRESS/OPINIONS from SPEC using Codex."""
    try:
        engine = _require_repo_config(repo)
        docs = generate_docs_from_spec(engine, spec_path=spec)
        write_ingested_docs(engine, docs, force=force)
    except (ConfigError, SpecIngestError) as exc:
        raise typer.Exit(str(exc))

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
            raise typer.Exit("Aborted.")
    engine = _require_repo_config(repo)
    try:
        clear_work_docs(engine)
    except ConfigError as exc:
        raise typer.Exit(str(exc))
    typer.echo("Cleared TODO/PROGRESS/OPINIONS.")


@app.command("doctor")
def doctor_cmd(repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path")):
    """Validate repo setup."""
    engine = _require_repo_config(repo)
    try:
        doctor(engine.repo_root)
    except ConfigError as exc:
        raise typer.Exit(str(exc))
    typer.echo("Doctor check passed")


@app.command()
def serve(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
    host: Optional[str] = typer.Option(None, "--host", help="Host to bind"),
    port: Optional[int] = typer.Option(None, "--port", help="Port to bind"),
):
    """Start the web server and UI API."""
    try:
        config = load_config(repo or Path.cwd())
    except ConfigError as exc:
        raise typer.Exit(str(exc))
    if isinstance(config, HubConfig):
        bind_host = host or config.server_host
        bind_port = port or config.server_port
        typer.echo(f"Serving hub on http://{bind_host}:{bind_port}")
        uvicorn.run(create_hub_app(config.root), host=bind_host, port=bind_port)
        return
    engine = _require_repo_config(repo)
    app_instance = create_app(engine.repo_root)
    bind_host = host or engine.config.server_host
    bind_port = port or engine.config.server_port
    typer.echo(f"Serving repo on http://{bind_host}:{bind_port}")
    uvicorn.run(app_instance, host=bind_host, port=bind_port)


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
        raise typer.Exit(str(exc))
    typer.echo(f"Created repo {snapshot.id} at {snapshot.path}")


@hub_app.command("serve")
def hub_serve(
    path: Optional[Path] = typer.Option(None, "--path", help="Hub root path"),
    host: Optional[str] = typer.Option(None, "--host", help="Host to bind"),
    port: Optional[int] = typer.Option(None, "--port", help="Port to bind"),
):
    """Start the hub supervisor server."""
    config = _require_hub_config(path)
    bind_host = host or config.server_host
    bind_port = port or config.server_port
    typer.echo(f"Serving hub on http://{bind_host}:{bind_port}")
    uvicorn.run(create_hub_app(config.root), host=bind_host, port=bind_port)


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


if __name__ == "__main__":
    app()
