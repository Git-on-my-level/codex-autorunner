"""PMA binding CLI commands (non-thread binding queries)."""

import asyncio
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx
import typer

from ...adapters.chat.surface_resolver import (
    SurfaceInfo,
    build_surface_resolvers,
    close_surface_resolvers,
    resolve_surface_bindings,
    surface_info_display,
)
from ...core.config import load_hub_config
from .hub_control_plane_client import (
    build_hub_control_plane_url,
    format_resource_owner_label,
    normalize_resource_owner_options,
    request_json,
    resolve_hub_path,
)
from .hub_path_option import hub_root_path_option
from .output import echo_json, exit_with_error


def register_binding_commands(app: typer.Typer) -> None:
    app.command("list")(pma_binding_list)
    app.command("active")(pma_binding_active)
    app.command("work")(pma_binding_work)


def pma_binding_list(
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent"),
    repo_id: Optional[str] = typer.Option(None, "--repo", help="Filter by repo id"),
    resource_kind: Optional[str] = typer.Option(
        None, "--resource-kind", help="Filter by managed resource kind"
    ),
    resource_id: Optional[str] = typer.Option(
        None, "--resource-id", help="Filter by managed resource id"
    ),
    surface_kind: Optional[str] = typer.Option(
        None, "--surface", help="Filter by surface kind (discord, telegram, etc.)"
    ),
    include_disabled: bool = typer.Option(
        False, "--include-disabled", help="Include disabled bindings"
    ),
    limit: int = typer.Option(200, "--limit", min=1, help="Maximum rows to return"),
    resolve: bool = typer.Option(
        False,
        "--resolve",
        help="Resolve surface keys to human-readable channel/chat names",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """List orchestration bindings for threads."""
    hub_root = resolve_hub_path(path)
    (
        normalized_resource_kind,
        normalized_resource_id,
        _normalized_workspace_root,
    ) = normalize_resource_owner_options(
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    params = {
        key: value
        for key, value in {
            "agent": agent,
            "resource_kind": normalized_resource_kind,
            "resource_id": normalized_resource_id,
            "surface_kind": surface_kind,
            "include_disabled": include_disabled,
            "limit": limit,
        }.items()
        if value is not None
    }
    try:
        config = load_hub_config(hub_root)
        data = request_json(
            "GET",
            build_hub_control_plane_url(config, "/bindings"),
            token_env=config.server_auth_token_env,
            params=params,
        )
    except httpx.HTTPError as exc:
        exit_with_error(f"HTTP error: {exc}", cause=None)
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        exit_with_error(f"Error: {exc}", cause=None)

    bindings = data.get("bindings", []) if isinstance(data, dict) else []
    resolved: dict[tuple[str, str], Optional[SurfaceInfo]] = {}
    if resolve and isinstance(bindings, list):
        resolved = asyncio.run(
            _resolve_bindings_for_cli(
                bindings=bindings,
                raw_config=config.raw if isinstance(config.raw, dict) else {},
            )
        )
        if output_json and isinstance(data, dict):
            data = dict(data)
            data["bindings"] = [
                (
                    _binding_with_resolved_surface(binding, resolved)
                    if isinstance(binding, dict)
                    else binding
                )
                for binding in bindings
            ]

    if output_json:
        echo_json(data)
        return

    if not isinstance(bindings, list) or not bindings:
        typer.echo("No bindings found")
        return
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        disabled = " (disabled)" if binding.get("disabled_at") else ""
        typer.echo(
            " ".join(
                [
                    str(binding.get("binding_id") or "")[:12],
                    f"surface={binding.get('surface_kind') or ''}",
                    f"key={binding.get('surface_key') or ''}",
                    _resolved_binding_label(binding, resolved) if resolve else "",
                    f"thread={binding.get('thread_target_id') or ''}"[:20],
                    f"agent={binding.get('agent_id') or ''}",
                    format_resource_owner_label(binding),
                ]
            ).strip()
            + disabled
        )


async def _resolve_bindings_for_cli(
    *,
    bindings: list[Any],
    raw_config: Mapping[str, Any],
) -> dict[tuple[str, str], Optional[SurfaceInfo]]:
    mapping_bindings = [binding for binding in bindings if isinstance(binding, Mapping)]
    resolvers = build_surface_resolvers(raw_config)
    try:
        return await resolve_surface_bindings(mapping_bindings, resolvers)
    finally:
        await close_surface_resolvers(resolvers)


def _binding_surface_lookup_key(
    binding: Mapping[str, Any],
) -> Optional[tuple[str, str]]:
    surface_kind = binding.get("surface_kind")
    surface_key = binding.get("surface_key")
    if not isinstance(surface_kind, str) or not surface_kind.strip():
        return None
    if not isinstance(surface_key, str) or not surface_key.strip():
        return None
    return (surface_kind.strip(), surface_key.strip())


def _resolved_binding_label(
    binding: Mapping[str, Any],
    resolved: Mapping[tuple[str, str], Optional[SurfaceInfo]],
) -> str:
    lookup_key = _binding_surface_lookup_key(binding)
    info = resolved.get(lookup_key) if lookup_key is not None else None
    return f"name={surface_info_display(info)}"


def _binding_with_resolved_surface(
    binding: dict[str, Any],
    resolved: Mapping[tuple[str, str], Optional[SurfaceInfo]],
) -> dict[str, Any]:
    enriched = dict(binding)
    lookup_key = _binding_surface_lookup_key(binding)
    info = resolved.get(lookup_key) if lookup_key is not None else None
    enriched["surface_resolved"] = info.to_dict() if info is not None else None
    return enriched


def pma_binding_active(
    surface_kind: str = typer.Option(
        ..., "--surface", help="Surface kind (discord, telegram, etc.)"
    ),
    surface_key: str = typer.Option(
        ..., "--key", help="Surface-specific key (channel id, chat id, etc.)"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Get the active thread bound to a surface key."""
    hub_root = resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = request_json(
            "GET",
            build_hub_control_plane_url(
                config,
                f"/bindings/active?surface_kind={surface_kind}&surface_key={surface_key}",
            ),
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        exit_with_error(f"HTTP error: {exc}", cause=None)
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        exit_with_error(f"Error: {exc}", cause=None)

    if output_json:
        echo_json(data)
        return

    thread_target_id = data.get("thread_target_id")
    if thread_target_id:
        typer.echo(f"Active thread: {thread_target_id}")
    else:
        typer.echo("No active thread for this surface key")


def pma_binding_work(
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent"),
    repo_id: Optional[str] = typer.Option(None, "--repo", help="Filter by repo id"),
    resource_kind: Optional[str] = typer.Option(
        None, "--resource-kind", help="Filter by managed resource kind"
    ),
    resource_id: Optional[str] = typer.Option(
        None, "--resource-id", help="Filter by managed resource id"
    ),
    limit: int = typer.Option(200, "--limit", min=1, help="Maximum rows to return"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """List busy-work summaries (threads with running or queued work)."""
    hub_root = resolve_hub_path(path)
    (
        normalized_resource_kind,
        normalized_resource_id,
        _normalized_workspace_root,
    ) = normalize_resource_owner_options(
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    params = {
        key: value
        for key, value in {
            "agent": agent,
            "resource_kind": normalized_resource_kind,
            "resource_id": normalized_resource_id,
            "limit": limit,
        }.items()
        if value is not None
    }
    try:
        config = load_hub_config(hub_root)
        data = request_json(
            "GET",
            build_hub_control_plane_url(config, "/bindings/work"),
            token_env=config.server_auth_token_env,
            params=params,
        )
    except httpx.HTTPError as exc:
        exit_with_error(f"HTTP error: {exc}", cause=None)
    except (ValueError, OSError) as exc:  # intentional: top-level error handler
        exit_with_error(f"Error: {exc}", cause=None)

    if output_json:
        echo_json(data)
        return

    summaries = data.get("summaries", []) if isinstance(data, dict) else []
    if not isinstance(summaries, list) or not summaries:
        typer.echo("No busy work found")
        return
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        thread_id = summary.get("thread_target_id", "")
        agent_id = summary.get("agent_id", "")
        owner = format_resource_owner_label(summary)
        lifecycle = summary.get("lifecycle_status", "-")
        runtime = summary.get("runtime_status", "-")
        exec_status = summary.get("execution_status", "-")
        bindings = summary.get("binding_count", 0)
        surfaces = ",".join(summary.get("surface_kinds", []))
        preview = summary.get("message_preview", "")
        if preview:
            preview = preview[:50] + "..." if len(preview) > 50 else preview
        typer.echo(
            f"{thread_id[:12]} agent={agent_id} {owner} "
            f"lifecycle={lifecycle} runtime={runtime} exec={exec_status} "
            f"bindings={bindings} surfaces={surfaces}"
        )
        if preview:
            typer.echo(f"  preview: {preview}")
