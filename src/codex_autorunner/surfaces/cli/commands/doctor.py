import json
import site
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import typer

from ....core.config import ConfigError, RepoConfig, derive_repo_config, load_hub_config
from ....core.diagnostics.process_snapshot import (
    collect_processes,
)
from ....core.git_utils import run_git
from ....core.managed_processes import list_process_records
from ....core.runtime import (
    DoctorReport,
    doctor,
    hub_worktree_doctor_checks,
    pma_doctor_checks,
)
from ....core.utils import (
    RepoNotFoundError,
    find_repo_root,
    is_within,
    resolve_executable,
)
from ....integrations.chat.doctor import chat_doctor_checks
from ....integrations.discord.doctor import discord_doctor_checks
from ....integrations.telegram.doctor import telegram_doctor_checks
from .utils import get_car_version, raise_exit


def _build_process_registry_payload(
    repo_root: Optional[Path],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    if repo_root is None:
        return {}, []

    records: list[dict[str, Any]] = []
    try:
        process_records = list_process_records(repo_root)
    except Exception:
        process_records = []

    for record in process_records:
        record_key = "unknown"
        try:
            record_key = record.record_key()
        except Exception:
            pass
        records.append(
            {
                "kind": record.kind,
                "workspace_id": record.workspace_id,
                "record_key": record_key,
                "pid": record.pid,
                "pgid": record.pgid,
                "owner_pid": record.owner_pid,
                "base_url": record.base_url,
                "path": str(
                    repo_root
                    / ".codex-autorunner"
                    / "processes"
                    / record.kind
                    / f"{record_key}.json"
                ),
            }
        )

    counts: dict[str, int] = {}
    for record in records:  # type: ignore[assignment]
        kind = record.get("kind")  # type: ignore[attr-defined]
        if isinstance(kind, str):
            counts[kind] = counts.get(kind, 0) + 1
    return counts, records


def _find_hub_server_process(port: Optional[int]) -> Optional[dict[str, Any]]:
    import subprocess

    try:
        proc = subprocess.run(
            ["ps", "-ax", "-o", "pid=", "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    lines = (proc.stdout or "").splitlines()
    candidates: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        command = parts[1]
        command_lc = command.lower()

        is_hub_serve = any(
            marker in command_lc
            for marker in (
                "car hub serve",
                "car serve",
                "codex_autorunner.cli hub serve",
                "codex_autorunner.cli serve",
            )
        )
        if not is_hub_serve:
            continue
        candidates.append({"pid": pid, "command": command})

    if not candidates:
        return None
    if port is None:
        return candidates[0]

    for candidate in candidates:
        command = str(candidate.get("command") or "")
        if (
            f"--port {port}" in command
            or f"--port={port}" in command
            or f":{port}" in command
        ):
            return candidate

    if len(candidates) == 1:
        return candidates[0]
    return None


def _repo_checkout_info(repo_root: Optional[Path]) -> Optional[dict[str, Any]]:
    if repo_root is None:
        return None
    if not (repo_root / ".git").exists():
        return None
    try:
        head = run_git(["rev-parse", "HEAD"], repo_root, check=False)
        short_head = run_git(["rev-parse", "--short", "HEAD"], repo_root, check=False)
        branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root, check=False)
        describe = run_git(
            ["describe", "--tags", "--always", "--dirty"],
            repo_root,
            check=False,
        )
        dirty = run_git(["status", "--porcelain"], repo_root, check=False)
    except Exception:
        return None
    return {
        "root": str(repo_root),
        "branch": (branch.stdout or "").strip() or None,
        "head": (head.stdout or "").strip() or None,
        "short_head": (short_head.stdout or "").strip() or None,
        "describe": (describe.stdout or "").strip() or None,
        "dirty": bool((dirty.stdout or "").strip()),
    }


def _doctor_versions_payload(start_path: Path) -> dict[str, Any]:
    start_path = start_path.resolve()
    hub_config = None
    hub_error = None
    try:
        hub_config = load_hub_config(start_path)
    except ConfigError as exc:
        hub_error = str(exc)

    repo_root = None
    try:
        repo_root = find_repo_root(start_path)
    except RepoNotFoundError:
        repo_root = None

    package_version = get_car_version()
    package_path = None
    try:
        import codex_autorunner

        package_path = str(Path(codex_autorunner.__file__).resolve())
    except Exception:
        package_path = None

    site_packages: list[str] = []
    try:
        for item in site.getsitepackages():
            if isinstance(item, str) and item not in site_packages:
                site_packages.append(item)
    except Exception:
        pass
    try:
        user_site = site.getusersitepackages()
        if isinstance(user_site, str) and user_site not in site_packages:
            site_packages.append(user_site)
    except Exception:
        pass

    hub_server = None
    if hub_config is not None:
        hub_server = _find_hub_server_process(hub_config.server_port)

    checkout = _repo_checkout_info(repo_root)

    source_matches_checkout = None
    if package_path and repo_root:
        try:
            source_matches_checkout = is_within(Path(package_path), repo_root)
        except Exception:
            source_matches_checkout = None

    mismatch_detected = None
    if source_matches_checkout is not None:
        mismatch_detected = not source_matches_checkout

    return {
        "cli": {
            "version": package_version,
            "argv0": sys.argv[0],
            "resolved_argv0": (
                str(Path(sys.argv[0]).resolve()) if Path(sys.argv[0]).exists() else None
            ),
            "car_executable": resolve_executable("car"),
        },
        "python": {
            "executable": sys.executable,
            "prefix": sys.prefix,
            "base_prefix": getattr(sys, "base_prefix", None),
            "site_packages": site_packages,
        },
        "package": {
            "name": "codex-autorunner",
            "version": package_version,
            "path": package_path,
        },
        "hub": {
            "root": str(hub_config.root) if hub_config else None,
            "config_error": hub_error,
            "server_host": getattr(hub_config, "server_host", None),
            "server_port": getattr(hub_config, "server_port", None),
            "server_base_path": getattr(hub_config, "server_base_path", None),
            "server_process": {
                "running": bool(hub_server),
                "pid": hub_server.get("pid") if hub_server else None,
                "startup_command": hub_server.get("command") if hub_server else None,
                "version": package_version if hub_server else None,
            },
        },
        "checkout": checkout,
        "mismatch": {
            "source_matches_checkout": source_matches_checkout,
            "detected": mismatch_detected,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def register_doctor_commands(
    doctor_app: typer.Typer,
) -> None:
    @doctor_app.callback(invoke_without_command=True)
    def doctor_cmd(
        ctx: typer.Context,
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo or hub path"),
        json_output: bool = typer.Option(
            False, "--json", help="Output JSON for scripting"
        ),
    ):
        if ctx.invoked_subcommand:
            return
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
            chat_checks = chat_doctor_checks(repo_root=repo_root)
            discord_checks = discord_doctor_checks(hub_config)
            pma_checks = pma_doctor_checks(hub_config, repo_root=repo_root)
            hub_worktree_checks = hub_worktree_doctor_checks(hub_config)

            report = DoctorReport(
                checks=report.checks
                + telegram_checks
                + chat_checks
                + discord_checks
                + pma_checks
                + hub_worktree_checks
            )
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
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
            raise_exit("Doctor check failed")
        typer.echo("Doctor check passed")

    @doctor_app.command("versions")
    def doctor_versions(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo or hub path"),
        json_output: bool = typer.Option(False, "--json", help="Output JSON"),
    ):
        payload = _doctor_versions_payload(repo or Path.cwd())
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
            return

        cli = payload.get("cli", {})
        python_info = payload.get("python", {})
        package = payload.get("package", {})
        hub = payload.get("hub", {})
        checkout = payload.get("checkout", {})
        mismatch = payload.get("mismatch", {})
        process = hub.get("server_process", {}) if isinstance(hub, dict) else {}

        typer.echo("CLI")
        typer.echo(f"- version: {cli.get('version')}")
        typer.echo(f"- argv0: {cli.get('argv0')}")
        typer.echo(f"- resolved argv0: {cli.get('resolved_argv0') or 'n/a'}")
        typer.echo(f"- car executable: {cli.get('car_executable') or 'n/a'}")

        typer.echo("Python")
        typer.echo(f"- executable: {python_info.get('executable')}")
        typer.echo(f"- prefix: {python_info.get('prefix')}")
        typer.echo(f"- base_prefix: {python_info.get('base_prefix')}")
        site_packages = python_info.get("site_packages")
        if isinstance(site_packages, list) and site_packages:
            typer.echo("- site-packages:")
            for entry in site_packages:
                typer.echo(f"  - {entry}")
        else:
            typer.echo("- site-packages: n/a")

        typer.echo("Package")
        typer.echo(f"- name: {package.get('name')}")
        typer.echo(f"- version: {package.get('version')}")
        typer.echo(f"- path: {package.get('path') or 'n/a'}")

        typer.echo("Hub Server")
        typer.echo(f"- hub root: {hub.get('root') or 'n/a'}")
        typer.echo(f"- host: {hub.get('server_host') or 'n/a'}")
        typer.echo(f"- port: {hub.get('server_port') or 'n/a'}")
        typer.echo(f"- base path: {hub.get('server_base_path') or '/'}")
        typer.echo(f"- running: {bool(process.get('running'))}")
        if process.get("running"):
            typer.echo(f"- pid: {process.get('pid')}")
            typer.echo(f"- startup command: {process.get('startup_command')}")
            typer.echo(f"- process version: {process.get('version')}")
        elif hub.get("config_error"):
            typer.echo(f"- config error: {hub.get('config_error')}")

        typer.echo("Checkout")
        if isinstance(checkout, dict) and checkout:
            typer.echo(f"- root: {checkout.get('root')}")
            typer.echo(f"- branch: {checkout.get('branch') or 'n/a'}")
            typer.echo(f"- head: {checkout.get('head') or 'n/a'}")
            typer.echo(f"- describe: {checkout.get('describe') or 'n/a'}")
            typer.echo(f"- dirty: {bool(checkout.get('dirty'))}")
        else:
            typer.echo("- repo checkout: n/a")

        typer.echo("Mismatch Detection")
        typer.echo(
            f"- source matches checkout: {mismatch.get('source_matches_checkout')}"
        )
        typer.echo(f"- mismatch detected: {mismatch.get('detected')}")

    @doctor_app.command("processes")
    def doctor_processes(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo or hub path"),
        json_output: bool = typer.Option(False, "--json", help="Output JSON"),
        save: bool = typer.Option(
            False,
            "--save",
            help="Save snapshot to .codex-autorunner/diagnostics/process-snapshot.json",
        ),
        top_n: int = typer.Option(
            5, "--top", help="Number of processes to show per category"
        ),
    ):
        """
        Capture a snapshot of CAR-related processes (opencode, codex app-server).

        Useful for diagnosing process leaks. Repro steps:
        1. Start a ticket_flow worker
        2. Stop it (SIGTERM)
        3. Run this command before and after to compare process counts

        This command shows counts and top N cmdlines for:
        - opencode processes
        - codex app-server processes
        """
        try:
            start_path = repo or Path.cwd()
            hub_config = load_hub_config(start_path)
        except ConfigError:
            hub_config = None

        snapshot = collect_processes()
        repo_root: Optional[Path] = None
        try:
            repo_root = find_repo_root(start_path)
        except RepoNotFoundError:
            pass
        registry_counts, registry_records = _build_process_registry_payload(repo_root)

        if json_output:
            payload = {
                "snapshot": snapshot.to_dict(),
                "registry": {
                    "counts_by_kind": registry_counts,
                    "records": registry_records,
                },
            }
            typer.echo(json.dumps(payload, indent=2))
            return

        typer.echo("Process Snapshot")
        typer.echo(f"- collected at: {snapshot.collected_at}")

        typer.echo(f"\nopencode processes: {snapshot.opencode_count}")
        for proc in snapshot.opencode_processes[:top_n]:
            typer.echo(
                f"  - pid={proc.pid} ppid={proc.ppid} pgid={proc.pgid}: "
                f"{proc.command[:100]}"
            )

        typer.echo(f"\ncodex app-server processes: {snapshot.app_server_count}")
        for proc in snapshot.app_server_processes[:top_n]:
            typer.echo(
                f"  - pid={proc.pid} ppid={proc.ppid} pgid={proc.pgid}: "
                f"{proc.command[:100]}"
            )

        typer.echo("\nProcess Registry")
        if not registry_counts:
            typer.echo("  - by kind: none")
            typer.echo("  - records: none")
        else:
            typer.echo("  - by kind:")
            for kind in sorted(registry_counts):
                typer.echo(f"    - {kind}: {registry_counts[kind]}")
            typer.echo("  - records: owner_pid identifies CAR owner")
            for record in registry_records:
                typer.echo(
                    "    - {kind} {key} pid={pid} owner_pid={owner_pid} "
                    "base_url={base_url} path={path}".format(
                        kind=record.get("kind"),
                        key=record.get("record_key"),
                        pid=record.get("pid"),
                        owner_pid=record.get("owner_pid"),
                        base_url=record.get("base_url") or "n/a",
                        path=record.get("path"),
                    )
                )

        if save and hub_config:
            output_path = (
                hub_config.root
                / ".codex-autorunner"
                / "diagnostics"
                / "process-snapshot.json"
            )
            payload = {
                "snapshot": snapshot.to_dict(),
                "registry": {
                    "counts_by_kind": registry_counts,
                    "records": registry_records,
                },
            }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w") as f:
                json.dump(payload, f, indent=2)
            typer.echo(f"\nSnapshot saved to: {output_path}")
