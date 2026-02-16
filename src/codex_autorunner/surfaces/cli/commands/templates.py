import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Optional

import typer
import yaml

from ....agents.registry import validate_agent_id
from ....core.config import ConfigError, load_hub_config
from ....core.git_utils import GitError
from ....core.templates import (
    NetworkUnavailableError,
    RefNotFoundError,
    RepoNotConfiguredError,
    TemplateNotFoundError,
    fetch_template,
    get_scan_record,
    get_template_by_ref,
    index_templates,
    inject_provenance,
    parse_template_ref,
    scan_lock,
    search_templates,
)
from ....integrations.templates.scan_agent import (
    TemplateScanError,
    TemplateScanRejectedError,
    format_template_scan_rejection,
    run_template_scan,
)
from ....tickets.frontmatter import split_markdown_frontmatter
from ....tickets.lint import parse_ticket_index
from .utils import find_template_repo, resolve_hub_config_path_for_cli


def register_templates_commands(
    app: typer.Typer,
    *,
    require_repo_config: Callable[[Optional[Path], Optional[Path]], Any],
    require_templates_enabled: Callable[[Any], None],
    raise_exit: Callable[..., None],
    resolve_hub_config_path_for_cli: Callable[[Path, Optional[Path]], Optional[Path]],
) -> None:
    @app.command("fetch")
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
        ctx = require_repo_config(repo, hub)
        require_templates_enabled(ctx.config)
        fetched, scan_record, _hub_root = _fetch_template_with_scan(
            template, ctx, hub, ctx.config, raise_exit, resolve_hub_config_path_for_cli
        )

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

    @app.command("apply")
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
        ctx = require_repo_config(repo, hub)
        require_templates_enabled(ctx.config)

        fetched, scan_record, _hub_root = _fetch_template_with_scan(
            template, ctx, hub, ctx.config, raise_exit, resolve_hub_config_path_for_cli
        )

        resolved_dir = _resolve_ticket_dir(ctx.repo_root, ticket_dir)
        if resolved_dir.exists() and not resolved_dir.is_dir():
            raise_exit(f"Ticket dir is not a directory: {resolved_dir}")
        try:
            resolved_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise_exit(f"Unable to create ticket dir: {exc}")

        if at is None and not next_index:
            raise_exit("Specify --at or leave --next enabled to pick an index.")
        if at is not None and at < 1:
            raise_exit("Ticket index must be >= 1.")

        existing_indices = _collect_ticket_indices(resolved_dir)
        if at is None:
            index = _next_available_ticket_index(existing_indices)
        else:
            index = at
            if index in existing_indices:
                raise_exit(
                    f"Ticket index {index} already exists. Choose another index or open a gap."
                )

        normalized_suffix = _normalize_ticket_suffix(suffix, raise_exit)
        width = max(3, max([len(str(i)) for i in existing_indices + [index]]))
        filename = _ticket_filename(index, suffix=normalized_suffix, width=width)
        path = resolved_dir / filename
        if path.exists():
            raise_exit(f"Ticket already exists: {path}")

        content = fetched.content
        if set_agent:
            if set_agent != "user":
                try:
                    validate_agent_id(set_agent)
                except ValueError as exc:
                    raise_exit(str(exc), cause=exc)
            content = _apply_agent_override(content, set_agent, raise_exit)

        if provenance:
            content = inject_provenance(content, fetched, scan_record)

        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise_exit(f"Failed to write ticket: {exc}")

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


def _fetch_template_with_scan(
    template: str,
    ctx,
    hub: Optional[Path],
    config,
    raise_exit: Callable[..., None],
    resolve_hub_config_path_for_cli: Callable[[Path, Optional[Path]], Optional[Path]],
):
    try:
        parsed = parse_template_ref(template)
    except ValueError as exc:
        raise_exit(str(exc), cause=exc)

    repo_cfg = find_template_repo(config, parsed.repo_id)
    if repo_cfg is None:
        raise_exit(f"Template repo not configured: {parsed.repo_id}")

    hub_config_path = resolve_hub_config_path_for_cli(ctx.repo_root, hub)
    if hub_config_path is None:
        try:
            hub_config = load_hub_config(ctx.repo_root)
            hub_root = hub_config.root
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
    else:
        hub_root = hub_config_path.parent.parent.resolve()

    try:
        fetched = fetch_template(
            repo=repo_cfg, hub_root=hub_root, template_ref=template
        )
    except NetworkUnavailableError as exc:
        raise_exit(
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
        raise_exit(str(exc), cause=exc)

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
                    raise_exit(str(exc), cause=exc)
                except TemplateScanError as exc:
                    raise_exit(str(exc), cause=exc)
            elif scan_record.decision != "approve":
                raise_exit(format_template_scan_rejection(scan_record))

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


def _normalize_ticket_suffix(
    suffix: Optional[str], raise_exit: Callable[..., None]
) -> str:
    if not suffix:
        return ""
    cleaned = suffix.strip()
    if not cleaned:
        return ""
    if "/" in cleaned or "\\" in cleaned:
        raise_exit("Ticket suffix may not include path separators.")
    if not cleaned.startswith("-"):
        return f"-{cleaned}"
    return cleaned


def _apply_agent_override(
    content: str, agent: str, raise_exit: Callable[..., None]
) -> str:
    fm_yaml, body = split_markdown_frontmatter(content)
    if fm_yaml is None:
        raise_exit("Template is missing YAML frontmatter; cannot set agent.")
    try:
        data = yaml.safe_load(fm_yaml)
    except yaml.YAMLError as exc:
        raise_exit(f"Template frontmatter is invalid YAML: {exc}")
    if not isinstance(data, dict):
        raise_exit("Template frontmatter must be a YAML mapping to set agent.")
    data["agent"] = agent
    rendered = yaml.safe_dump(data, sort_keys=False).rstrip()
    return f"---\n{rendered}\n---{body}"


def _get_hub_root(
    ctx,
    hub: Optional[Path],
    raise_exit: Callable[..., None],
) -> Path:
    """Get the hub root path."""
    hub_config_path = resolve_hub_config_path_for_cli(ctx.repo_root, hub)
    if hub_config_path is None:
        try:
            hub_config = load_hub_config(ctx.repo_root)
            return hub_config.root
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)
    return hub_config_path.parent.parent.resolve()


def register_template_index_commands(
    app: typer.Typer,
    require_repo_config: Callable[[Optional[Path], Optional[Path]], Any],
    require_hub_config: Callable[[Optional[Path]], Any],
    raise_exit: Callable[..., None],
    resolve_hub_config_path_for_cli: Callable[[Path, Optional[Path]], Optional[Path]],
) -> None:
    """Register template list/show/search commands."""

    @app.command("list")
    def templates_list(
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """List available templates with id, name, and summary."""
        ctx = require_repo_config(repo, hub)

        try:
            hub_config = load_hub_config(hub or ctx.repo_root)
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)

        hub_root = _get_hub_root(ctx, hub, raise_exit)

        templates = index_templates(hub_config, hub_root)

        if output_json:
            payload = {
                "templates": [
                    {
                        "repo_id": t.repo_id,
                        "path": t.path,
                        "name": t.name,
                        "summary": t.summary,
                        "ref": t.ref,
                    }
                    for t in templates
                ],
                "count": len(templates),
            }
            typer.echo(json.dumps(payload, indent=2))
            return

        if not templates:
            typer.echo("No templates found.")
            return

        typer.echo(f"Available templates ({len(templates)}):")
        for t in templates:
            summary = t.summary[:50] + "..." if len(t.summary) > 50 else t.summary
            typer.echo(f"  {t.repo_id}:{t.path}@{t.ref}")
            typer.echo(f"    name: {t.name}")
            typer.echo(f"    summary: {summary}")
            typer.echo()

    @app.command("show")
    def templates_show(
        template_ref: str = typer.Argument(
            ..., help="Template ref (REPO_ID:PATH[@REF])"
        ),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Show details for a specific template."""
        ctx = require_repo_config(repo, hub)

        try:
            hub_config = load_hub_config(hub or ctx.repo_root)
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)

        hub_root = _get_hub_root(ctx, hub, raise_exit)

        template = get_template_by_ref(hub_config, hub_root, template_ref)

        if template is None:
            raise_exit(f"Template not found: {template_ref}")

        if output_json:
            payload = {
                "repo_id": template.repo_id,
                "path": template.path,
                "name": template.name,
                "summary": template.summary,
                "ref": template.ref,
            }
            typer.echo(json.dumps(payload, indent=2))
            return

        typer.echo(f"Template: {template.repo_id}:{template.path}@{template.ref}")
        typer.echo(f"Name: {template.name}")
        typer.echo(f"Summary: {template.summary}")
        typer.echo(f"Ref: {template.ref}")

    @app.command("search")
    def templates_search(
        query: str = typer.Argument(..., help="Search query"),
        repo: Optional[Path] = typer.Option(None, "--repo", help="Repo path"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    ):
        """Search templates by name, path, or summary."""
        ctx = require_repo_config(repo, hub)

        try:
            hub_config = load_hub_config(hub or ctx.repo_root)
        except ConfigError as exc:
            raise_exit(str(exc), cause=exc)

        hub_root = _get_hub_root(ctx, hub, raise_exit)

        templates = search_templates(hub_config, hub_root, query)

        if output_json:
            payload = {
                "query": query,
                "templates": [
                    {
                        "repo_id": t.repo_id,
                        "path": t.path,
                        "name": t.name,
                        "summary": t.summary,
                        "ref": t.ref,
                    }
                    for t in templates
                ],
                "count": len(templates),
            }
            typer.echo(json.dumps(payload, indent=2))
            return

        if not templates:
            typer.echo(f"No templates found matching: {query}")
            return

        typer.echo(f"Search results for '{query}' ({len(templates)}):")
        for t in templates:
            summary = t.summary[:50] + "..." if len(t.summary) > 50 else t.summary
            typer.echo(f"  {t.repo_id}:{t.path}@{t.ref}")
            typer.echo(f"    name: {t.name}")
            typer.echo(f"    summary: {summary}")
            typer.echo()
