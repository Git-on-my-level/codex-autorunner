import os
import subprocess
from pathlib import Path
from typing import Optional

import typer
import uvicorn

from .config import ConfigError, DEFAULT_CONFIG, load_config
from .engine import Engine, LockError, clear_stale_lock, doctor
from .server import create_app, doctor_server
from .state import load_state, save_state, RunnerState, now_iso
from .utils import default_editor, find_repo_root, RepoNotFoundError, atomic_write

app = typer.Typer(add_completion=False)


def resolve_repo(repo: Optional[Path]) -> Path:
    if repo:
        return find_repo_root(repo)
    return find_repo_root(Path.cwd())


@app.command()
def init(
    path: Optional[Path] = typer.Argument(None, help="Repo path; defaults to CWD"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files"),
    git_init: bool = typer.Option(False, "--git-init", help="Run git init if missing"),
):
    """Initialize a repo for Codex autorunner."""
    repo_root = path or Path.cwd()
    repo_root = repo_root.resolve()
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        if git_init:
            subprocess.run(["git", "init"], cwd=repo_root, check=False)
        else:
            raise typer.Exit(
                "No .git directory found; rerun with --git-init to create one"
            )

    ca_dir = repo_root / ".codex-autorunner"
    ca_dir.mkdir(parents=True, exist_ok=True)

    config_path = ca_dir / "config.yml"
    if config_path.exists() and not force:
        typer.echo(f"Config already exists at {config_path}; use --force to overwrite")
    else:
        import yaml

        with config_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(DEFAULT_CONFIG, f, sort_keys=False)
        typer.echo(f"Wrote {config_path}")

    state_path = ca_dir / "state.json"
    if not state_path.exists() or force:
        atomic_write(
            state_path,
            '{\n  "last_run_id": null,\n  "status": "idle",\n  "last_exit_code": null,\n  "last_run_started_at": null,\n  "last_run_finished_at": null,\n  "runner_pid": null\n}\n',
        )
        typer.echo(f"Initialized {state_path}")

    log_path = ca_dir / "codex-autorunner.log"
    if not log_path.exists() or force:
        log_path.write_text("", encoding="utf-8")
        typer.echo(f"Created {log_path}")

    _seed_doc(repo_root / ".codex-autorunner" / "TODO.md", force, sample_todo())
    _seed_doc(repo_root / ".codex-autorunner" / "PROGRESS.md", force, "# Progress\n\n")
    _seed_doc(repo_root / ".codex-autorunner" / "OPINIONS.md", force, sample_opinions())

    typer.echo("Init complete")


def _seed_doc(path: Path, force: bool, content: str) -> None:
    if path.exists() and not force:
        return
    path.write_text(content, encoding="utf-8")
    typer.echo(f"Wrote {path}")


def sample_todo() -> str:
    return """# TODO\n\n- [ ] Replace this item with your first task\n- [ ] Add another task\n- [x] Example completed item\n"""


def sample_opinions() -> str:
    return """# Opinions\n\n- Prefer small, well-tested changes.\n- Keep docs in sync with code.\n- Avoid unnecessary dependencies.\n"""


@app.command()
def status(repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path")):
    """Show autorunner status."""
    try:
        root = resolve_repo(repo)
    except RepoNotFoundError as exc:
        raise typer.Exit(str(exc))
    engine = Engine(root)
    state = load_state(engine.state_path)
    outstanding, _ = engine.docs.todos()
    typer.echo(f"Repo: {root}")
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
        root = resolve_repo(repo)
        engine = Engine(root)
        engine.acquire_lock(force=force)
        engine.run_loop()
    except (RepoNotFoundError, ConfigError, LockError) as exc:
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
        root = resolve_repo(repo)
        engine = Engine(root)
        engine.acquire_lock(force=force)
        engine.run_once()
    except (RepoNotFoundError, ConfigError, LockError) as exc:
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
    try:
        root = resolve_repo(repo)
    except RepoNotFoundError as exc:
        raise typer.Exit(str(exc))
    engine = Engine(root)
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
        root = resolve_repo(repo)
        engine = Engine(root)
        clear_stale_lock(engine.lock_path)
        engine.acquire_lock(force=force)
        engine.run_loop(stop_after_runs=1 if once else None)
    except (RepoNotFoundError, ConfigError, LockError) as exc:
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
    try:
        root = resolve_repo(repo)
    except RepoNotFoundError as exc:
        raise typer.Exit(str(exc))
    engine = Engine(root)
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
    target: str = typer.Argument(..., help="todo|progress|opinions"),
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
):
    """Open one of the docs in $EDITOR."""
    try:
        root = resolve_repo(repo)
    except RepoNotFoundError as exc:
        raise typer.Exit(str(exc))
    config = load_config(root)
    key = target.lower()
    if key not in ("todo", "progress", "opinions"):
        raise typer.Exit("Invalid target; choose todo, progress, or opinions")
    path = config.doc_path(key)
    editor = default_editor()
    typer.echo(f"Opening {path} with {editor}")
    subprocess.run([editor, str(path)])


@app.command("doctor")
def doctor_cmd(repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path")):
    """Validate repo setup."""
    try:
        root = resolve_repo(repo)
        doctor(root)
    except (RepoNotFoundError, ConfigError) as exc:
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
        root = resolve_repo(repo)
        config = load_config(root)
    except (RepoNotFoundError, ConfigError) as exc:
        raise typer.Exit(str(exc))
    app = create_app(root)
    bind_host = host or config.server_host
    bind_port = port or config.server_port
    typer.echo(f"Serving on http://{bind_host}:{bind_port}")
    uvicorn.run(app, host=bind_host, port=bind_port)


if __name__ == "__main__":
    app()
