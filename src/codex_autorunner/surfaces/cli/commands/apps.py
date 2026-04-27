from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from ....core.apps import (
    AppSourceInfo,
    get_app_by_ref,
    index_apps,
    is_probably_installed_app_id,
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
            raise_exit(
                "Installed app lookup is not implemented yet. "
                "Use a source ref like REPO_ID:PATH[@REF]."
            )

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
