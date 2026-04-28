from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Callable, Optional

import typer
import yaml

from ....core.apps import (
    AppApplyError,
    AppInstallConflictError,
    AppInstallError,
    AppLock,
    AppSourceInfo,
    AppToolRunnerError,
    AppToolRunResult,
    apply_app_entrypoint,
    get_app_by_ref,
    get_installed_app,
    index_apps,
    install_app,
    is_probably_installed_app_id,
    list_installed_app_tools,
    list_installed_apps,
    run_installed_app_tool,
)
from ....core.config import ConfigError, load_hub_config
from ..hub_path_option import hub_root_path_option


def register_apps_commands(
    app: typer.Typer,
    *,
    require_repo_config: Callable[[Optional[Path], Optional[Path]], Any],
    require_apps_enabled: Callable[[Any], None],
    raise_exit: Callable[..., None],
    resolve_hub_config_path_for_cli: Callable[[Path, Optional[Path]], Optional[Path]],
) -> None:
    @app.command("list")
    def apps_list(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """List available app bundles from configured source repos."""
        ctx = require_repo_config(repo, hub)
        require_apps_enabled(ctx.config)
        hub_config, hub_root = _load_hub_context(
            ctx.repo_root,
            hub,
            raise_exit,
            resolve_hub_config_path_for_cli,
        )
        apps = index_apps(hub_config, hub_root)

        if output_json:
            payload = {
                "apps": [_app_payload(item) for item in apps],
                "count": len(apps),
            }
            typer.echo(json.dumps(payload, indent=2))
            return

        if not apps:
            typer.echo("No apps.")
            return

        typer.echo(f"Apps ({len(apps)}):")
        for item in apps:
            description = (
                item.description[:60] + "..."
                if len(item.description) > 60
                else item.description
            )
            typer.echo(
                f"  {item.repo_id}:{item.path}@{item.ref} | {item.app_id} | "
                f"{item.app_name} {item.app_version} | {description}"
            )

    @app.command("install")
    def apps_install(
        app_ref: str = typer.Argument(..., help="Source app ref (REPO_ID:PATH[@REF])"),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        force: bool = typer.Option(
            False, "--force", help="Replace an existing install"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Install an app bundle into repo-local CAR app state."""
        ctx = require_repo_config(repo, hub)
        require_apps_enabled(ctx.config)
        hub_config, hub_root = _load_hub_context(
            ctx.repo_root,
            hub,
            raise_exit,
            resolve_hub_config_path_for_cli,
        )
        try:
            result = install_app(
                hub_config,
                hub_root,
                ctx.repo_root,
                app_ref,
                force=force,
            )
        except (AppInstallConflictError, AppInstallError) as exc:
            raise_exit(str(exc), cause=exc)

        if output_json:
            typer.echo(
                json.dumps(
                    {
                        "changed": result.changed,
                        "app": _installed_app_payload(
                            result.app,
                            include_manifest=True,
                            include_manifest_text=True,
                        ),
                    },
                    indent=2,
                )
            )
            return

        typer.echo(
            "Installed app."
            if result.changed
            else "App already installed; lock and bundle match."
        )
        _echo_installed_app(result.app)

    @app.command("installed")
    def apps_installed(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """List repo-local installed apps."""
        ctx = require_repo_config(repo, hub)
        require_apps_enabled(ctx.config)
        try:
            installed_apps = list_installed_apps(ctx.repo_root)
        except AppInstallError as exc:
            raise_exit(str(exc), cause=exc)

        if output_json:
            typer.echo(
                json.dumps(
                    {
                        "apps": [
                            _installed_app_payload(item) for item in installed_apps
                        ],
                        "count": len(installed_apps),
                    },
                    indent=2,
                )
            )
            return

        if not installed_apps:
            typer.echo("No installed apps.")
            return

        typer.echo(f"Installed apps ({len(installed_apps)}):")
        for item in installed_apps:
            typer.echo(
                f"  {item.app_id} | {item.app_version} | "
                f"{_source_ref_from_lock(item.lock)} | {item.lock.installed_at}"
            )

    @app.command("apply")
    def apps_apply(
        app_ref_or_id: str = typer.Argument(
            ..., help="Source app ref (REPO_ID:PATH[@REF]) or installed app id"
        ),
        at: Optional[int] = typer.Option(None, "--at", help="Explicit ticket index"),
        next_index: bool = typer.Option(
            True, "--next/--no-next", help="Use next available index (default)"
        ),
        suffix: Optional[str] = typer.Option(
            None, "--suffix", help="Optional filename suffix (e.g. -foo)"
        ),
        set_values: Optional[list[str]] = typer.Option(
            None,
            "--set",
            help="Set an app input as key=value. May be provided multiple times.",
        ),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Apply an app entrypoint template into the ticket queue."""
        ctx = require_repo_config(repo, hub)
        require_apps_enabled(ctx.config)
        parsed_inputs = _parse_set_values(set_values, raise_exit)

        hub_config = None
        hub_root = None
        if not is_probably_installed_app_id(app_ref_or_id):
            hub_config, hub_root = _load_hub_context(
                ctx.repo_root,
                hub,
                raise_exit,
                resolve_hub_config_path_for_cli,
            )

        try:
            result = apply_app_entrypoint(
                ctx.repo_root,
                app_ref_or_id,
                hub_config=hub_config,
                hub_root=hub_root,
                at=at,
                next_index=next_index,
                suffix=suffix,
                app_inputs=parsed_inputs,
            )
        except (AppApplyError, AppInstallConflictError, AppInstallError) as exc:
            raise_exit(str(exc), cause=exc)

        if output_json:
            typer.echo(json.dumps(_app_apply_payload(result, ctx.repo_root), indent=2))
            return

        typer.echo(
            "Created ticket "
            f"{result.ticket_path} (index={result.ticket_index}, app={result.app.app_id})"
        )
        typer.echo(json.dumps(_app_apply_payload(result, ctx.repo_root), indent=2))

    @app.command("show")
    def apps_show(
        app_ref_or_id: str = typer.Argument(
            ..., help="Source app ref (REPO_ID:PATH[@REF]) or installed app id"
        ),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Show manifest metadata and provenance for a discovered app bundle."""
        ctx = require_repo_config(repo, hub)
        require_apps_enabled(ctx.config)

        if is_probably_installed_app_id(app_ref_or_id):
            try:
                installed_app = get_installed_app(ctx.repo_root, app_ref_or_id)
            except AppInstallError as exc:
                raise_exit(str(exc), cause=exc)
            if installed_app is None:
                raise_exit(f"Installed app not found: {app_ref_or_id}")

            if output_json:
                payload = _installed_app_payload(
                    installed_app,
                    include_manifest=True,
                    include_manifest_text=True,
                )
                typer.echo(json.dumps(payload, indent=2))
                return

            _echo_installed_app(installed_app)
            return

        hub_config, hub_root = _load_hub_context(
            ctx.repo_root,
            hub,
            raise_exit,
            resolve_hub_config_path_for_cli,
        )
        app_info = get_app_by_ref(hub_config, hub_root, app_ref_or_id)
        if app_info is None:
            raise_exit(f"App not found: {app_ref_or_id}")
        assert app_info is not None

        if output_json:
            payload = _app_payload(
                app_info,
                include_manifest=True,
                include_manifest_text=True,
            )
            typer.echo(json.dumps(payload, indent=2))
            return

        typer.echo(f"{app_info.app_name} ({app_info.app_id})")
        typer.echo(f"Version: {app_info.app_version}")
        typer.echo(f"Source: {app_info.repo_id}:{app_info.path}@{app_info.ref}")
        typer.echo(f"Trusted: {'yes' if app_info.trusted else 'no'}")
        typer.echo(f"Commit: {app_info.commit_sha}")
        typer.echo(f"Manifest SHA: {app_info.manifest_sha}")
        if app_info.description:
            typer.echo(f"Description: {app_info.description}")
        manifest = app_info.manifest
        if manifest.entrypoint is not None:
            typer.echo(f"Entrypoint: {manifest.entrypoint.path}")
        if manifest.templates:
            typer.echo("Templates: " + ", ".join(sorted(manifest.templates.keys())))
        if manifest.tools:
            typer.echo("Tools: " + ", ".join(sorted(manifest.tools.keys())))
        if manifest.hooks:
            typer.echo("Hooks: " + ", ".join(hook.point for hook in manifest.hooks))

    @app.command("tools")
    def apps_tools(
        app_id: str = typer.Argument(..., help="Installed app id"),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """List declared tools for an installed app."""
        ctx = require_repo_config(repo, hub)
        require_apps_enabled(ctx.config)
        try:
            tools = list_installed_app_tools(ctx.repo_root, app_id)
        except (AppInstallError, AppToolRunnerError) as exc:
            raise_exit(str(exc), cause=exc)

        if output_json:
            typer.echo(
                json.dumps(
                    {
                        "app_id": app_id,
                        "tools": [_tool_payload(tool) for tool in tools],
                        "count": len(tools),
                    },
                    indent=2,
                )
            )
            return

        if not tools:
            typer.echo(f"No tools declared for installed app {app_id}.")
            return

        typer.echo(f"Tools for {app_id} ({len(tools)}):")
        for tool in tools:
            suffix = "" if tool.bundle_verified else " [bundle dirty]"
            typer.echo(f"  {tool.tool_id} | timeout={tool.timeout_seconds}s{suffix}")
            if tool.description:
                typer.echo(f"    {tool.description}")

    @app.command(
        "run",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )
    def apps_run(
        ctx: typer.Context,
        app_id: str = typer.Argument(..., help="Installed app id"),
        tool_id: str = typer.Argument(..., help="Declared tool id"),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = hub_root_path_option(),
        timeout: Optional[float] = typer.Option(
            None, "--timeout", help="Override tool timeout in seconds"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ) -> None:
        """Run a declared tool from an installed app."""
        repo_ctx = require_repo_config(repo, hub)
        require_apps_enabled(repo_ctx.config)
        extra_args = list(ctx.args)
        if extra_args[:1] == ["--"]:
            extra_args = extra_args[1:]

        try:
            result = run_installed_app_tool(
                repo_ctx.repo_root,
                app_id,
                tool_id,
                extra_argv=extra_args,
                timeout_seconds=timeout,
            )
        except (AppInstallError, AppToolRunnerError) as exc:
            raise_exit(str(exc), cause=exc)

        if output_json:
            typer.echo(json.dumps(_tool_run_payload(result), indent=2))
        else:
            _echo_tool_run(result)

        if result.exit_code != 0:
            raise typer.Exit(code=result.exit_code)


def _load_hub_context(
    repo_root: Path,
    hub: Optional[Path],
    raise_exit: Callable[..., None],
    resolve_hub_config_path_for_cli: Callable[[Path, Optional[Path]], Optional[Path]],
):
    hub_config_path = resolve_hub_config_path_for_cli(repo_root, hub)
    try:
        hub_config = load_hub_config(hub or repo_root)
    except ConfigError as exc:
        raise_exit(str(exc), cause=exc)
    if hub_config_path is None:
        return hub_config, hub_config.root
    return hub_config, hub_config_path.parent.parent.resolve()


def _app_payload(
    info: AppSourceInfo,
    *,
    include_manifest: bool = False,
    include_manifest_text: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "repo_id": info.repo_id,
        "path": info.path,
        "ref": info.ref,
        "source_ref": f"{info.repo_id}:{info.path}@{info.ref}",
        "app_id": info.app_id,
        "app_version": info.app_version,
        "app_name": info.app_name,
        "description": info.description,
        "commit_sha": info.commit_sha,
        "manifest_sha": info.manifest_sha,
        "trusted": info.trusted,
    }
    if include_manifest:
        payload["manifest"] = dataclasses.asdict(info.manifest)
    if include_manifest_text:
        payload["manifest_text"] = info.manifest_text
    return payload


def _installed_app_payload(
    info,
    *,
    include_manifest: bool = False,
    include_manifest_text: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "app_id": info.app_id,
        "app_version": info.app_version,
        "app_name": info.manifest.name,
        "description": info.manifest.description,
        "source_repo_id": info.lock.source_repo_id,
        "source_url": info.lock.source_url,
        "source_path": info.lock.source_path,
        "source_ref": info.lock.source_ref,
        "source_ref_string": _source_ref_from_lock(info.lock),
        "commit_sha": info.lock.commit_sha,
        "manifest_sha": info.lock.manifest_sha,
        "bundle_sha": info.lock.bundle_sha,
        "trusted": info.lock.trusted,
        "installed_at": info.lock.installed_at,
        "bundle_verified": info.bundle_verified,
    }
    if include_manifest:
        payload["manifest"] = dataclasses.asdict(info.manifest)
    if include_manifest_text:
        payload["manifest_text"] = info.manifest_text
    return payload


def _source_ref_from_lock(lock: AppLock) -> str:
    return f"{lock.source_repo_id}:{lock.source_path}@{lock.source_ref}"


def _echo_installed_app(info) -> None:
    typer.echo(f"{info.manifest.name} ({info.app_id})")
    typer.echo(f"Version: {info.app_version}")
    typer.echo(f"Source: {_source_ref_from_lock(info.lock)}")
    typer.echo(f"Source URL: {info.lock.source_url}")
    typer.echo(f"Trusted: {'yes' if info.lock.trusted else 'no'}")
    typer.echo(f"Commit: {info.lock.commit_sha}")
    typer.echo(f"Manifest SHA: {info.lock.manifest_sha}")
    typer.echo(f"Bundle SHA: {info.lock.bundle_sha}")
    typer.echo(f"Bundle Verified: {'yes' if info.bundle_verified else 'no'}")
    typer.echo(f"Installed At: {info.lock.installed_at}")
    if info.manifest.description:
        typer.echo(f"Description: {info.manifest.description}")
    if info.manifest.entrypoint is not None:
        typer.echo(f"Entrypoint: {info.manifest.entrypoint.path}")
    if info.manifest.templates:
        typer.echo("Templates: " + ", ".join(sorted(info.manifest.templates.keys())))
    if info.manifest.tools:
        typer.echo("Tools: " + ", ".join(sorted(info.manifest.tools.keys())))
    if info.manifest.hooks:
        typer.echo("Hooks: " + ", ".join(hook.point for hook in info.manifest.hooks))


def _tool_payload(tool) -> dict[str, object]:
    return {
        "app_id": tool.app_id,
        "tool_id": tool.tool_id,
        "description": tool.description,
        "argv": list(tool.argv),
        "timeout_seconds": tool.timeout_seconds,
        "bundle_verified": tool.bundle_verified,
        "outputs": [dataclasses.asdict(output) for output in tool.outputs],
    }


def _tool_run_payload(result: AppToolRunResult) -> dict[str, object]:
    return {
        "app_id": result.app_id,
        "tool_id": result.tool_id,
        "argv": list(result.argv),
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "stdout_excerpt": result.stdout_excerpt,
        "stderr_excerpt": result.stderr_excerpt,
        "stdout_log_path": str(result.stdout_log_path),
        "stderr_log_path": str(result.stderr_log_path),
        "outputs": [
            {
                "kind": output.kind,
                "label": output.label,
                "relative_path": output.relative_path,
                "absolute_path": str(output.absolute_path),
            }
            for output in result.outputs
        ],
    }


def _app_apply_payload(result, repo_root: Path) -> dict[str, object]:
    return {
        "app_id": result.app.app_id,
        "app_version": result.app.app_version,
        "ticket_path": str(result.ticket_path),
        "ticket_path_repo_relative": str(result.ticket_path.relative_to(repo_root)),
        "ticket_index": result.ticket_index,
        "source_ref": result.source_ref,
        "app_inputs": result.app_inputs,
        "apply_inputs_path": str(result.apply_inputs_path),
        "install_changed": result.install_changed,
    }


def _parse_set_values(
    raw_values: Optional[list[str]],
    raise_exit: Callable[..., None],
) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for item in raw_values or []:
        if "=" not in item:
            raise_exit(f"Invalid --set value {item!r}; expected key=value.")
        key, value = item.split("=", 1)
        cleaned_key = key.strip()
        if not cleaned_key:
            raise_exit(f"Invalid --set value {item!r}; key must not be empty.")
        if cleaned_key in parsed:
            raise_exit(f"Duplicate --set key: {cleaned_key}")
        try:
            parsed_value = yaml.safe_load(value)
        except yaml.YAMLError as exc:
            raise_exit(f"Invalid YAML value for --set {cleaned_key}: {exc}", cause=exc)
        parsed[cleaned_key] = parsed_value
    return parsed


def _echo_tool_run(result: AppToolRunResult) -> None:
    typer.echo(f"App: {result.app_id}")
    typer.echo(f"Tool: {result.tool_id}")
    typer.echo(f"Exit Code: {result.exit_code}")
    typer.echo(f"Duration: {result.duration_seconds:.3f}s")
    typer.echo(f"Stdout Log: {result.stdout_log_path}")
    typer.echo(f"Stderr Log: {result.stderr_log_path}")
    if result.outputs:
        typer.echo("Outputs:")
        for output in result.outputs:
            label = f" | {output.label}" if output.label else ""
            typer.echo(f"  {output.kind} | {output.relative_path}{label}")
    if result.stdout_excerpt:
        typer.echo("stdout:")
        typer.echo(result.stdout_excerpt)
    if result.stderr_excerpt:
        typer.echo("stderr:")
        typer.echo(result.stderr_excerpt)
