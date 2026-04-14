import json
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from ....agents.registry import validate_agent_id
from ....core.config import HubConfig
from ....core.runtime import RuntimeContext
from ....core.utils import atomic_write
from ....tickets.bulk import bulk_clear_model_pin, bulk_set_agent
from ....tickets.doctor import format_or_doctor_tickets
from ....tickets.hub_pack import (
    TicketPackImportError,
    TicketPackSetupError,
    import_ticket_pack,
    load_template_frontmatter,
    parse_assignment_specs,
    setup_ticket_pack,
)
from .utils import (
    collect_ticket_indices,
    next_available_ticket_index,
    parse_renumber,
    raise_exit,
    render_ticket_markdown,
    resolve_hub_repo_root,
    validate_tickets,
)


def _print_ticket_import_report(report) -> None:
    parts = [f"repo={report.repo_id}", f"dry_run={report.dry_run}"]
    if report.renumber:
        parts.append(
            f"renumber={report.renumber.get('start')}:{report.renumber.get('step')}"
        )
    if report.assign_agent:
        parts.append(f"agent={report.assign_agent}")
    typer.echo(" ".join(parts))
    for item in report.items:
        status = item.status.upper()
        target = item.target or "-"
        typer.echo(f"  {status}: {item.source} -> {target}")
        for err in item.errors:
            typer.echo(f"    {err}")
    if report.errors:
        for err in report.errors:
            typer.echo(f"error: {err}")
    if report.lint_errors:
        for err in report.lint_errors:
            typer.echo(f"lint: {err}")


def _append_setup_pack_final_tickets(
    *,
    ticket_dir: Path,
    review_agent: str,
    pr_agent: str,
) -> list[str]:
    ticket_dir.mkdir(parents=True, exist_ok=True)
    existing_indices = collect_ticket_indices(ticket_dir)
    next_index = next_available_ticket_index(existing_indices)
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
    atomic_write(
        review_path,
        render_ticket_markdown(review_frontmatter, review_body),
    )

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
        "Default to a ready-for-review PR unless the user explicitly asked for a draft.\n"
        "Add `pr_url` to this ticket frontmatter after the PR exists so hub summaries can surface it."
    )
    atomic_write(pr_path, render_ticket_markdown(pr_frontmatter, pr_body))

    return [
        str(review_path.relative_to(ticket_dir.parent)),
        str(pr_path.relative_to(ticket_dir.parent)),
    ]


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
    typer.echo(f"{action}: repo={repo_id} updated={updated} skipped={skipped}")
    if errors:
        for err in errors:
            typer.echo(f"error: {err}")
    if lint_errors:
        for err in lint_errors:
            typer.echo(f"lint: {err}")


def _print_ticket_doctor_report(action: str, report, *, check_mode: bool) -> None:
    typer.echo(f"{action}: checked={report.checked} changed={report.changed}")
    if report.warnings:
        for warning in report.warnings:
            typer.echo(f"warning: {warning}")
    if report.errors:
        for err in report.errors:
            typer.echo(f"error: {err}")


def register_hub_tickets_commands(
    hub_tickets_app: typer.Typer,
    *,
    require_hub_config_func: Callable[[Optional[Path]], HubConfig],
    require_repo_config_func: Callable[
        [Optional[Path], Optional[Path]], RuntimeContext
    ],
    require_templates_enabled_func: Callable,
    fetch_template_with_scan_func: Callable,
    ticket_flow_preflight: Callable[[RuntimeContext, Path], Any],
    print_preflight_report: Callable[[Any], None],
    ticket_flow_start: Callable[..., None],
) -> None:
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
            False,
            "--strip-depends-on/--no-strip-depends-on",
            help="Strip frontmatter.depends_on keys from imported tickets",
        ),
        reconcile_depends_on: str = typer.Option(
            "warn",
            "--reconcile-depends-on",
            help="depends_on reconciliation mode: off, warn, auto",
        ),
        lint: bool = typer.Option(
            True, "--lint/--no-lint", help="Lint destination tickets (default on)"
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Preview without writing"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    ):
        """Import a zipped ticket pack into a hub repo ticket directory."""
        config = require_hub_config_func(hub)
        repo_root = resolve_hub_repo_root(config, repo_id)

        if assign_agent:
            if assign_agent != "user":
                try:
                    validate_agent_id(assign_agent, repo_root)
                except ValueError as exc:
                    raise_exit(str(exc), cause=exc)

        renumber_parsed = parse_renumber(renumber)

        if not zip_path.exists():
            raise_exit(f"Zip path does not exist: {zip_path}")
        if zip_path.is_dir():
            raise_exit("Zip path must be a file.")

        template_frontmatter = None
        if apply_template:
            ctx = require_repo_config_func(repo_root, config.root)
            require_templates_enabled_func(ctx.config)
            fetched, _scan_record, _hub_root = fetch_template_with_scan_func(
                apply_template, ctx, config.root
            )
            try:
                template_frontmatter = load_template_frontmatter(fetched.content)
            except TicketPackImportError as exc:
                raise_exit(str(exc), cause=exc)

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
            raise_exit("Ticket import failed.")

    @hub_tickets_app.command("bulk-set")
    def hub_tickets_bulk_set(
        repo_id: str = typer.Option(..., "--repo", help="Hub repo id"),
        agent: str = typer.Option(..., "--agent", help="Agent id to set on tickets"),
        profile: Optional[str] = typer.Option(
            None, "--profile", help="Agent profile to set on tickets"
        ),
        range_spec: Optional[str] = typer.Option(
            None, "--range", help="Range of ticket indices in the form A:B"
        ),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    ):
        """Set `frontmatter.agent` (and optionally `profile`) for a ticket range."""
        config = require_hub_config_func(hub)
        repo_root = resolve_hub_repo_root(config, repo_id)
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"

        if agent != "user":
            try:
                validate_agent_id(agent, repo_root)
            except ValueError as exc:
                raise_exit(str(exc), cause=exc)

        try:
            result = bulk_set_agent(
                ticket_dir,
                agent,
                range_spec,
                repo_root=repo_root,
                profile=profile,
                profile_explicit=profile is not None,
            )
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)

        lint_errors = validate_tickets(ticket_dir)
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
            raise_exit("Ticket bulk update failed.")

    @hub_tickets_app.command("bulk-clear-model")
    def hub_tickets_bulk_clear_model(
        repo_id: str = typer.Option(..., "--repo", help="Hub repo id"),
        range_spec: Optional[str] = typer.Option(
            None, "--range", help="Range of ticket indices in the form A:B"
        ),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    ):
        """Clear model/reasoning pins from ticket frontmatter for a range."""
        config = require_hub_config_func(hub)
        repo_root = resolve_hub_repo_root(config, repo_id)
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"

        try:
            result = bulk_clear_model_pin(
                ticket_dir,
                range_spec,
                repo_root=repo_root,
            )
        except ValueError as exc:
            raise_exit(str(exc), cause=exc)

        lint_errors = validate_tickets(ticket_dir)
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
            raise_exit("Ticket bulk update failed.")

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
        """Format ticket frontmatter and report lint drift."""
        config = require_hub_config_func(hub)
        repo_root = resolve_hub_repo_root(config, repo_id)
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
            raise_exit("Ticket fmt failed.")

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
        """Check or auto-fix ticket frontmatter consistency issues."""
        config = require_hub_config_func(hub)
        repo_root = resolve_hub_repo_root(config, repo_id)
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
            raise_exit("Ticket doctor failed.")

    @hub_tickets_app.command("setup-pack")
    def hub_tickets_setup_pack(
        target_path: Path = typer.Argument(
            ...,
            help="Existing repo/worktree path that will receive unpacked tickets.",
        ),
        from_zip: Path = typer.Option(
            ...,
            "--from",
            help="Path to ticket pack zip",
        ),
        assign: list[str] = typer.Option(
            [],
            "--assign",
            help="Agent assignment spec '<agent>:<ticket_numbers>' (repeatable)",
        ),
        start: bool = typer.Option(
            False,
            "--start",
            help="Start ticket_flow after setup succeeds",
        ),
        append_final_tickets: bool = typer.Option(
            False,
            "--append-final-tickets",
            help="Add final-review and open-pr tickets after import",
        ),
        final_review_agent: str = typer.Option(
            "codex",
            "--final-review-agent",
            help="Agent for final-review ticket (with --append-final-tickets)",
        ),
        pr_agent: str = typer.Option(
            "codex",
            "--pr-agent",
            help="Agent for open-pr ticket (with --append-final-tickets)",
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    ):
        """Unpack a ticket pack zip into a repo and run ticket lint + flow preflight."""

        target_repo = target_path.resolve()
        try:
            parse_assignment_specs(assign)
            report = setup_ticket_pack(
                target_path=target_repo,
                zip_path=from_zip,
                assignment_specs=assign,
            )
        except TicketPackSetupError as exc:
            raise_exit(str(exc), cause=exc)

        ticket_dir = target_repo / ".codex-autorunner" / "tickets"
        final_tickets: list[str] = []
        if append_final_tickets:
            if final_review_agent != "user":
                try:
                    validate_agent_id(final_review_agent, target_repo)
                except ValueError as exc:
                    raise_exit(str(exc), cause=exc)
            if pr_agent != "user":
                try:
                    validate_agent_id(pr_agent, target_repo)
                except ValueError as exc:
                    raise_exit(str(exc), cause=exc)
            final_tickets = _append_setup_pack_final_tickets(
                ticket_dir=ticket_dir,
                review_agent=final_review_agent,
                pr_agent=pr_agent,
            )

        lint_errors = validate_tickets(ticket_dir)
        engine = require_repo_config_func(target_repo, hub)
        preflight = ticket_flow_preflight(engine, ticket_dir)

        payload = {
            "mode": "path",
            "repo_root": str(target_repo),
            "zip_path": str(from_zip),
            "assign": list(assign),
            "setup": report.to_dict(),
            "final_tickets": final_tickets,
            "lint_errors": lint_errors,
            "preflight": preflight.to_dict(),
            "started": False,
        }

        if output_json:
            typer.echo(json.dumps(payload, indent=2))
        else:
            typer.echo(
                f"setup-pack: repo={target_repo} extracted={len(report.extracted_files)} assigned={len(report.assigned_files)}"
            )
            if final_tickets:
                typer.echo(f"final: {', '.join(final_tickets)}")
            if lint_errors:
                for err in lint_errors:
                    typer.echo(f"lint: {err}")
            typer.echo("preflight:")
            print_preflight_report(preflight)

        if lint_errors or preflight.has_errors():
            raise_exit("Ticket setup-pack failed preflight.")
        if start:
            ticket_flow_start(repo=target_repo, hub=hub, force_new=False)
