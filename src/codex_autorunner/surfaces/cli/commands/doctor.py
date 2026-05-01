import json
import logging
import site
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import typer

from ....core.config import ConfigError, RepoConfig, derive_repo_config, load_hub_config
from ....core.diagnostics.process_snapshot import (
    collect_processes,
    enrich_with_ownership,
)
from ....core.flows.worker_reaper import (
    inspect_flow_workers,
    reap_stale_flow_workers,
)
from ....core.git_utils import GitError, run_git
from ....core.managed_processes import list_process_records
from ....core.runtime import (
    DoctorReport,
    doctor,
    hermes_doctor_checks,
    hub_destination_doctor_checks,
    hub_worktree_doctor_checks,
    pma_doctor_checks,
    summarize_opencode_lifecycle,
    zeroclaw_doctor_checks,
)
from ....core.utils import (
    RepoNotFoundError,
    find_repo_root,
    is_within,
    resolve_executable,
)
from ....integrations.chat.doctor import (
    chat_doctor_checks,
    chat_surface_lab_doctor_checks,
    chat_ux_timing_diagnostic_checks,
)
from ....integrations.discord.doctor import discord_doctor_checks
from ....integrations.telegram.doctor import telegram_doctor_checks
from .utils import get_car_version, raise_exit

logger = logging.getLogger(__name__)


def _build_process_registry_payload(
    repo_root: Optional[Path],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    if repo_root is None:
        return {}, []

    records: list[dict[str, Any]] = []
    try:
        process_records = list_process_records(repo_root)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.debug("Failed to list process records: %s", e)
        process_records = []

    for record in process_records:
        record_key = "unknown"
        try:
            record_key = record.record_key()
        except (
            Exception
        ) as e:  # intentional: record implementation may raise arbitrary errors
            logger.debug("Failed to get record key: %s", e)
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
    except (OSError, subprocess.SubprocessError):
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
    except GitError:
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
    except (ImportError, OSError):
        package_path = None

    site_packages: list[str] = []
    try:
        for item in site.getsitepackages():
            if isinstance(item, str) and item not in site_packages:
                site_packages.append(item)
    except (OSError, ValueError) as e:
        logger.debug("Failed to get site packages: %s", e)
    try:
        user_site = site.getusersitepackages()
        if isinstance(user_site, str) and user_site not in site_packages:
            site_packages.append(user_site)
    except (OSError, ValueError) as e:
        logger.debug("Failed to get user site packages: %s", e)

    hub_server = None
    if hub_config is not None:
        hub_server = _find_hub_server_process(hub_config.server_port)

    checkout = _repo_checkout_info(repo_root)

    source_matches_checkout = None
    if package_path and repo_root:
        try:
            source_matches_checkout = is_within(
                root=repo_root, target=Path(package_path)
            )
        except (OSError, ValueError):
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
        dev: bool = typer.Option(
            False,
            "--dev",
            help="Include developer-focused parity checks",
        ),
    ):
        """Run default doctor checks (or subcommands when provided)."""
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
            discord_checks = discord_doctor_checks(hub_config)
            pma_checks = pma_doctor_checks(hub_config, repo_root=repo_root)
            hub_worktree_checks = hub_worktree_doctor_checks(hub_config)
            hub_destination_checks = hub_destination_doctor_checks(hub_config)
            chat_checks = chat_doctor_checks(repo_root=repo_root) if dev else []
            chat_lab_checks = (
                chat_surface_lab_doctor_checks(repo_root=repo_root) if dev else []
            )
            timing_checks = chat_ux_timing_diagnostic_checks() if dev else []

            report = DoctorReport(
                checks=report.checks
                + telegram_checks
                + discord_checks
                + pma_checks
                + hub_worktree_checks
                + hub_destination_checks
                + zeroclaw_doctor_checks(hub_config)
                + hermes_doctor_checks(hub_config)
                + chat_checks
                + chat_lab_checks
                + timing_checks
            )
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
        if json_output:
            typer.echo(json.dumps(report.to_dict(), indent=2))
            if report.has_errors():
                raise typer.Exit(code=1)
            return
        for check in report.checks:
            line = f"{check.status.upper()}: {check.message}"
            if check.fix:
                line = f"{line} fix: {check.fix}"
            typer.echo(line)
        if report.has_errors():
            raise_exit("Doctor check failed")
        typer.echo("ok")

    @doctor_app.command("versions")
    def doctor_versions(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo or hub path"),
        json_output: bool = typer.Option(False, "--json", help="Output JSON"),
    ):
        """Print build/runtime version diagnostics for CLI, Python, and hub server."""
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

        typer.echo(
            f"cli version={cli.get('version')} argv0={cli.get('argv0')} car={cli.get('car_executable') or 'n/a'}"
        )
        typer.echo(
            f"python {python_info.get('executable')} prefix={python_info.get('prefix')}"
        )
        site_packages = python_info.get("site_packages")
        if isinstance(site_packages, list) and site_packages:
            typer.echo(f"  site-packages: {', '.join(site_packages)}")
        typer.echo(
            f"package codex-autorunner {package.get('version')} {package.get('path') or 'n/a'}"
        )
        typer.echo(
            f"hub root={hub.get('root') or 'n/a'} host={hub.get('server_host') or 'n/a'} "
            f"port={hub.get('server_port') or 'n/a'} base_path={hub.get('server_base_path') or '/'} "
            f"running={bool(process.get('running'))}"
        )
        if process.get("running"):
            typer.echo(
                f"  pid={process.get('pid')} cmd={process.get('startup_command')}"
            )
        elif hub.get("config_error"):
            typer.echo(f"  config_error={hub.get('config_error')}")
        if isinstance(checkout, dict) and checkout:
            typer.echo(
                f"checkout root={checkout.get('root')} branch={checkout.get('branch') or 'n/a'} "
                f"head={checkout.get('head') or 'n/a'} describe={checkout.get('describe') or 'n/a'} "
                f"dirty={bool(checkout.get('dirty'))}"
            )
        else:
            typer.echo("checkout: n/a")
        typer.echo(
            f"mismatch: source_matches={mismatch.get('source_matches_checkout')} detected={mismatch.get('detected')}"
        )

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
        Capture a snapshot of CAR-related processes.

        Useful for diagnosing process leaks. Repro steps:
        1. Start a ticket_flow worker
        2. Stop it (SIGTERM)
        3. Run this command before and after to compare process counts

        This command shows counts and top N cmdlines for:
        - CAR service processes
        - OpenCode runtime processes
        - Codex app-server processes
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
        if repo_root:
            snapshot = enrich_with_ownership(snapshot, repo_root)
        registry_counts, registry_records = _build_process_registry_payload(repo_root)
        opencode_lifecycle = {}
        if repo_root is not None:
            try:
                opencode_lifecycle = summarize_opencode_lifecycle(repo_root)
            except (ConfigError, OSError, ValueError, TypeError) as exc:
                logger.debug(
                    "Skipping OpenCode lifecycle summary for %s: %s",
                    repo_root,
                    exc,
                )
                opencode_lifecycle = {}

        if json_output:
            payload = {
                "snapshot": snapshot.to_dict(),
                "registry": {
                    "counts_by_kind": registry_counts,
                    "records": registry_records,
                },
                "opencode_lifecycle": opencode_lifecycle,
            }
            typer.echo(json.dumps(payload, indent=2))
            return

        typer.echo(f"snapshot collected={snapshot.collected_at}")
        typer.echo(f"car_services({snapshot.car_service_count}):")
        for proc in snapshot.car_service_processes[:top_n]:
            mem = f" rss={proc.rss_kb}KB" if proc.rss_kb else ""
            own = f" [{proc.ownership.value}]" if proc.ownership else ""
            typer.echo(
                f"  pid={proc.pid} ppid={proc.ppid} pgid={proc.pgid}{mem}{own}: {proc.command[:80]}"
            )
        typer.echo(f"opencode({snapshot.opencode_count}):")
        for proc in snapshot.opencode_processes[:top_n]:
            mem = f" rss={proc.rss_kb}KB" if proc.rss_kb else ""
            own = f" [{proc.ownership.value}]" if proc.ownership else ""
            typer.echo(
                f"  pid={proc.pid} ppid={proc.ppid} pgid={proc.pgid}{mem}{own}: {proc.command[:80]}"
            )
        typer.echo(f"codex_app_server({snapshot.codex_app_server_count}):")
        for proc in snapshot.app_server_processes[:top_n]:
            mem = f" rss={proc.rss_kb}KB" if proc.rss_kb else ""
            own = f" [{proc.ownership.value}]" if proc.ownership else ""
            typer.echo(
                f"  pid={proc.pid} ppid={proc.ppid} pgid={proc.pgid}{mem}{own}: {proc.command[:80]}"
            )
        typer.echo(
            "summary: "
            f"managed_runtimes={snapshot.managed_runtime_count} total={snapshot.total_count}"
        )
        if not registry_counts:
            typer.echo("registry: none")
        else:
            typer.echo("registry:")
            for kind in sorted(registry_counts):
                typer.echo(f"  {kind}: {registry_counts[kind]}")
            for record in registry_records:
                typer.echo(
                    "  {kind} {key} pid={pid} owner={owner_pid} url={base_url}".format(
                        kind=record.get("kind"),
                        key=record.get("record_key"),
                        pid=record.get("pid"),
                        owner_pid=record.get("owner_pid"),
                        base_url=record.get("base_url") or "n/a",
                    )
                )
        if not opencode_lifecycle:
            typer.echo("lifecycle: unavailable")
        else:
            counts = opencode_lifecycle.get("counts") or {}
            typer.echo(
                "lifecycle: scope={scope} active={active} stale={stale}".format(
                    scope=opencode_lifecycle.get("server_scope") or "workspace",
                    active=counts.get("active", 0),
                    stale=counts.get("stale", 0),
                )
            )
            for record in (opencode_lifecycle.get("managed_servers") or [])[:top_n]:
                typer.echo(
                    "  ws={workspace} pid={pid} status={status} origin={origin}".format(
                        workspace=record.get("workspace_id") or "pid-only",
                        pid=record.get("pid") or "n/a",
                        status=record.get("status") or "unknown",
                        origin=record.get("process_origin") or "unknown",
                    )
                )
            live_handles = opencode_lifecycle.get("live_handles") or []
            if live_handles:
                for handle in live_handles[:top_n]:
                    typer.echo(
                        "  live: ws={workspace} mode={mode} turns={turns} pid={pid}".format(
                            workspace=handle.get("workspace_id") or "unknown",
                            mode=handle.get("mode") or "unknown",
                            turns=handle.get("active_turns") or 0,
                            pid=handle.get("process_pid") or "n/a",
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
                "opencode_lifecycle": opencode_lifecycle,
            }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w") as f:
                json.dump(payload, f, indent=2)
            typer.echo(f"saved: {output_path}")

    @doctor_app.command("flow-workers")
    def doctor_flow_workers(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        json_output: bool = typer.Option(False, "--json", help="Output JSON"),
        prune: bool = typer.Option(
            False,
            "--prune",
            help="Kill confirmed stale/orphaned flow workers",
        ),
    ):
        """List and optionally prune stale flow worker processes."""
        try:
            repo_root = find_repo_root(repo or Path.cwd())
        except RepoNotFoundError as exc:
            raise_exit("No repo found for flow worker diagnostics", cause=exc)

        if prune:
            summary = reap_stale_flow_workers(repo_root, logger=logger)
            workers = summary.workers
            payload = summary.to_dict()
        else:
            workers = inspect_flow_workers(repo_root)
            stale = [w for w in workers if w.classification == "stale"]
            zombies = [w for w in workers if w.classification == "zombie"]
            active = [w for w in workers if w.classification == "active"]
            payload = {
                "scanned_count": len(workers),
                "active_count": len(active),
                "stale_count": len(stale),
                "zombie_count": len(zombies),
                "pruned_count": 0,
                "memory_waste_kb": sum(int(w.rss_kb or 0) for w in stale + zombies),
                "workers": [w.to_dict() for w in workers],
                "errors": [],
            }

        if json_output:
            typer.echo(json.dumps(payload, indent=2))
            return

        typer.echo(f"repo: {repo_root}")
        typer.echo(
            "summary: active={active_count} stale={stale_count} zombie={zombie_count} "
            "memory_waste={memory_waste_kb}KB pruned={pruned_count}".format(**payload)
        )
        if not workers:
            typer.echo("workers: none")
            return
        for classification in ("active", "stale", "zombie", "dead"):
            group = [w for w in workers if w.classification == classification]
            if not group:
                continue
            typer.echo(f"{classification}:")
            for worker in group:
                age = (
                    f"{int(worker.metadata_age_seconds)}s"
                    if worker.metadata_age_seconds is not None
                    else "n/a"
                )
                rss = f" rss={worker.rss_kb}KB" if worker.rss_kb is not None else ""
                reason = f" reason={worker.reason}" if worker.reason else ""
                typer.echo(
                    f"  run={worker.run_id} pid={worker.pid} "
                    f"status={worker.status or 'missing'} age={age}{rss}{reason}"
                )
        for error in payload.get("errors") or []:
            typer.echo(f"error: {error}", err=True)

    @doctor_app.command("execution-history")
    def doctor_execution_history(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo or hub path"),
        json_output: bool = typer.Option(False, "--json", help="Output JSON"),
        top_n: int = typer.Option(10, "--top", help="Top N heavy executions to show"),
    ):
        """Report execution-history growth, recovery costs, and trace spill behavior."""
        from ....core.orchestration.execution_history_diagnostics import (
            ExecutionHistoryThresholds,
            run_execution_history_diagnostics,
        )

        try:
            start_path = repo or Path.cwd()
            hub_config = load_hub_config(start_path)
        except ConfigError:
            raise_exit("No hub config found")

        thresholds = ExecutionHistoryThresholds(top_n_heavy_executions=top_n)
        report = run_execution_history_diagnostics(
            hub_config.root,
            thresholds=thresholds,
        )

        if json_output:
            typer.echo(json.dumps(report.to_dict(), indent=2))
            return

        m = report.metrics
        typer.echo(
            f"executions: total={m.total_executions} terminal={m.terminal_executions}"
        )
        typer.echo(f"hot: timeline_rows={m.timeline_rows} checkpoints={m.checkpoints}")
        typer.echo(
            f"cold: finalized_manifests={m.finalized_manifests} "
            f"archived_manifests={m.archived_manifests} "
            f"total_trace_bytes={m.total_trace_bytes}"
        )
        if m.oversized_execution_ids:
            typer.echo(f"oversized_executions: {len(m.oversized_execution_ids)}")
        for fam, cnt in sorted(m.hot_row_count_by_family.items()):
            typer.echo(f"  hot family={fam} rows={cnt}")

        if report.top_n.top_heavy_executions:
            typer.echo(f"top {top_n} heavy executions (hot rows):")
            for entry in report.top_n.top_heavy_executions:
                typer.echo(f"  {entry['execution_id']}: {entry['hot_rows']} hot rows")

        if report.top_n.top_cold_trace_by_bytes:
            typer.echo(f"top {top_n} cold traces (bytes):")
            for entry in report.top_n.top_cold_trace_by_bytes:
                typer.echo(
                    f"  {entry['execution_id']}: {entry['cold_trace_bytes']} bytes"
                )

        if report.threshold_breaches:
            typer.echo(f"threshold breaches: {len(report.threshold_breaches)}")
            for breach in report.threshold_breaches:
                typer.echo(f"  {breach.level.upper()}: {breach.message}")
        else:
            typer.echo("threshold breaches: none")

        if report.startup_recovery_duration_seconds is not None:
            typer.echo(
                f"scan_duration: {report.startup_recovery_duration_seconds:.3f}s"
            )
