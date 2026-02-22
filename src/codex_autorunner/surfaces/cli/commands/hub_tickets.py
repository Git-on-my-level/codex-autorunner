import json
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from ....agents.registry import validate_agent_id
from ....core.config import HubConfig
from ....core.hub import HubSupervisor
from ....core.runtime import RuntimeContext
from ....core.utils import atomic_write
from ....tickets.bulk import bulk_clear_model_pin, bulk_set_agent
from ....tickets.doctor import format_or_doctor_tickets
from ....tickets.import_pack import (
    TicketPackImportError,
    import_ticket_pack,
    load_template_frontmatter,
)
from ....tickets.pack_import import (
    TicketPackSetupError,
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
    typer.echo(f"Repo: {report.repo_id}")
    typer.echo(f"Ticket dir: {report.ticket_dir}")
    typer.echo(f"Zip: {report.zip_path}")
    typer.echo(f"Dry run: {report.dry_run}")
    if report.renumber:
        typer.echo(
            f"Renumber: start={report.renumber.get('start')}, step={report.renumber.get('step')}"
        )
    if report.assign_agent:
        typer.echo(f"Assign agent: {report.assign_agent}")
    if report.clear_model_pin:
        typer.echo("Clear model pin: true")
    if report.apply_template:
        typer.echo(f"Template: {report.apply_template}")
    if getattr(report, "strip_depends_on", False):
        typer.echo("Strip depends_on: true")
    depends_summary = getattr(report, "depends_on_summary", None)
    if isinstance(depends_summary, dict) and depends_summary.get("has_depends_on"):
        typer.echo(
            f"Depends_on: mode={depends_summary.get('reconcile_mode')} "
            f"tickets={depends_summary.get('tickets_with_depends_on')} "
            f"edges={depends_summary.get('dependency_edges')} "
            f"reconciled={bool(depends_summary.get('reconciled'))}"
        )
        for warning in depends_summary.get("ordering_conflicts", []) or []:
            typer.echo(f"  Ordering impact: {warning}")
        for warning in depends_summary.get("ambiguous_reasons", []) or []:
            typer.echo(f"  Depends_on warning: {warning}")
    if report.lint:
        typer.echo("Lint: enabled")
    if report.errors:
        typer.echo("Errors:")
        for err in report.errors:
            typer.echo(f"- {err}")
    if report.lint_errors:
        typer.echo("Lint errors:")
        for err in report.lint_errors:
            typer.echo(f"- {err}")
    typer.echo(f"Tickets ready: {report.created}")
    for item in report.items:
        status = item.status.upper()
        target = item.target or "-"
        typer.echo(f"- {status}: {item.source} -> {target}")
        for err in item.errors:
            typer.echo(f"    {err}")
        for warning in getattr(item, "warnings", []) or []:
            typer.echo(f"    Warning: {warning}")


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
    typer.echo(f"Repo: {repo_id}")
    typer.echo(f"Ticket dir: {ticket_dir}")
    typer.echo(f"Action: {action}")
    typer.echo(f"Updated: {updated}")
    typer.echo(f"Skipped: {skipped}")
    if errors:
        typer.echo("Errors:")
        for err in errors:
            typer.echo(f"- {err}")
    if lint_errors:
        typer.echo("Lint errors:")
        for err in lint_errors:
            typer.echo(f"- {err}")


def _print_ticket_doctor_report(action: str, report, *, check_mode: bool) -> None:
    typer.echo(f"Action: {action}")
    typer.echo(f"Checked: {report.checked}")
    typer.echo(f"Changed: {report.changed}")
    if report.changed_files:
        typer.echo("Changed files:")
        for rel in report.changed_files:
            typer.echo(f"- {rel}")
    if report.warnings:
        typer.echo("Warnings:")
        for warning in report.warnings:
            typer.echo(f"- {warning}")
    if report.errors:
        typer.echo("Errors:")
        for err in report.errors:
            typer.echo(f"- {err}")
    if check_mode and report.changed:
        typer.echo("Check mode detected formatting/doctor drift.")


def register_hub_tickets_commands(
    hub_tickets_app: typer.Typer,
    *,
    require_hub_config_func: Callable[[Optional[Path]], HubConfig],
    require_repo_config_func: Callable[
        [Optional[Path], Optional[Path]], RuntimeContext
    ],
    require_templates_enabled_func: Callable,
    fetch_template_with_scan_func: Callable,
    build_hub_supervisor: Callable[[HubConfig], HubSupervisor],
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
        config = require_hub_config_func(hub)
        repo_root = resolve_hub_repo_root(config, repo_id)

        if assign_agent:
            if assign_agent != "user":
                try:
                    validate_agent_id(assign_agent)
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
        range_spec: Optional[str] = typer.Option(
            None, "--range", help="Range of ticket indices in the form A:B"
        ),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    ):
        config = require_hub_config_func(hub)
        repo_root = resolve_hub_repo_root(config, repo_id)
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"

        if agent != "user":
            try:
                validate_agent_id(agent)
            except ValueError as exc:
                raise_exit(str(exc), cause=exc)

        try:
            result = bulk_set_agent(
                ticket_dir,
                agent,
                range_spec,
                repo_root=repo_root,
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
        target_path: Optional[Path] = typer.Argument(
            None, help="Existing repo/worktree path (new mode)."
        ),
        from_zip: Optional[Path] = typer.Option(
            None, "--from", help="Path to ticket pack zip (new mode)"
        ),
        assign: list[str] = typer.Option(
            [],
            "--assign",
            help="Agent assignment spec '<agent>:<ticket_numbers>' (repeatable, new mode)",
        ),
        start: bool = typer.Option(
            False,
            "--start",
            help="Start ticket_flow after setup succeeds (new mode and legacy mode)",
        ),
        base_repo_id: Optional[str] = typer.Option(
            None, "--base-repo", help="Base repo id (legacy mode)"
        ),
        branch: Optional[str] = typer.Option(
            None, "--branch", help="Branch name for worktree (legacy mode)"
        ),
        zip_path: Optional[Path] = typer.Option(
            None, "--zip", help="Path to ticket pack zip (legacy mode)"
        ),
        renumber: Optional[str] = typer.Option(
            None, "--renumber", help="Renumber tickets with start=<n>,step=<n> (legacy)"
        ),
        assign_agent: Optional[str] = typer.Option(
            None, "--assign-agent", help="Override ticket frontmatter agent (legacy)"
        ),
        clear_model_pin: bool = typer.Option(
            False, "--clear-model-pin", help="Clear model/reasoning overrides (legacy)"
        ),
        apply_template: Optional[str] = typer.Option(
            None, "--apply-template", help="Template ref REPO:PATH[@REF] (legacy)"
        ),
        reconcile_depends_on: str = typer.Option(
            "auto",
            "--reconcile-depends-on",
            help="depends_on reconciliation mode: off, warn, auto (legacy)",
        ),
        final_review_agent: str = typer.Option(
            "codex",
            "--final-review-agent",
            help="Agent for final review ticket (legacy)",
        ),
        pr_agent: str = typer.Option(
            "codex", "--pr-agent", help="Agent for open PR ticket (legacy)"
        ),
        start_point: Optional[str] = typer.Option(
            None,
            "--start-point",
            help="Optional git ref for worktree branch (legacy, default: origin/<default-branch>)",
        ),
        force: bool = typer.Option(
            False, "--force", help="Allow existing worktree path (legacy)"
        ),
        output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
        hub: Optional[Path] = typer.Option(None, "--hub", help="Hub root path"),
    ):
        """One-command ticket pack setup.

        New mode:
        - `car hub tickets setup-pack <target_path> --from <zip> [--assign ...] [--start]`

        Legacy mode:
        - `car hub tickets setup-pack --base-repo ... --branch ... --zip ...`
        """

        new_mode_requested = (
            target_path is not None or from_zip is not None or bool(assign)
        )

        if new_mode_requested:
            if target_path is None:
                raise_exit(
                    "New setup-pack mode requires <target_path>. Example: "
                    "car hub tickets setup-pack worktrees/repo--branch --from pack.zip"
                )
            if from_zip is None:
                raise_exit("New setup-pack mode requires --from <zip_path>.")
            if zip_path is not None:
                raise_exit("Do not pass --zip with new mode; use --from.")
            if (
                any(
                    value is not None
                    for value in (
                        base_repo_id,
                        branch,
                        renumber,
                        assign_agent,
                        apply_template,
                        start_point,
                    )
                )
                or clear_model_pin
                or force
            ):
                raise_exit(
                    "Cannot combine new mode flags (--from/--assign) with legacy setup-pack flags."
                )
            if reconcile_depends_on != "auto":
                raise_exit(
                    "Cannot use --reconcile-depends-on in new mode; depends_on is preserved."
                )
            if final_review_agent != "codex" or pr_agent != "codex":
                raise_exit("Cannot use --final-review-agent/--pr-agent in new mode.")

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
            lint_errors = validate_tickets(ticket_dir)
            engine = require_repo_config_func(target_repo, hub)
            preflight = ticket_flow_preflight(engine, ticket_dir)

            payload = {
                "mode": "path",
                "repo_root": str(target_repo),
                "zip_path": str(from_zip),
                "assign": list(assign),
                "setup": report.to_dict(),
                "lint_errors": lint_errors,
                "preflight": preflight.to_dict(),
                "started": False,
            }

            if output_json:
                typer.echo(json.dumps(payload, indent=2))
            else:
                typer.echo(f"Setup pack: repo_root={target_repo} zip={from_zip}")
                typer.echo(f"Extracted files: {len(report.extracted_files)}")
                typer.echo(f"Assigned files: {len(report.assigned_files)}")
                if lint_errors:
                    typer.echo("Lint errors:")
                    for err in lint_errors:
                        typer.echo(f"- {err}")
                typer.echo("Preflight:")
                print_preflight_report(preflight)

            if lint_errors or preflight.has_errors():
                raise_exit("Ticket setup-pack failed preflight.")
            if start:
                ticket_flow_start(repo=target_repo, hub=hub, force_new=False)
            return

        if not base_repo_id or not branch or zip_path is None:
            raise_exit(
                "Legacy setup-pack mode requires --base-repo, --branch, and --zip. "
                "Or use new mode: setup-pack <target_path> --from <zip>."
            )
        config = require_hub_config_func(hub)
        if not zip_path.exists():
            raise_exit(f"Zip path does not exist: {zip_path}")
        if zip_path.is_dir():
            raise_exit("Zip path must be a file.")

        if assign_agent and assign_agent != "user":
            try:
                validate_agent_id(assign_agent)
            except ValueError as exc:
                raise_exit(str(exc), cause=exc)
        if final_review_agent != "user":
            try:
                validate_agent_id(final_review_agent)
            except ValueError as exc:
                raise_exit(str(exc), cause=exc)
        if pr_agent != "user":
            try:
                validate_agent_id(pr_agent)
            except ValueError as exc:
                raise_exit(str(exc), cause=exc)

        supervisor = build_hub_supervisor(config)
        try:
            snapshot = supervisor.create_worktree(
                base_repo_id=base_repo_id,
                branch=branch,
                force=force,
                start_point=start_point,
            )
        except Exception as exc:
            raise_exit(str(exc), cause=exc)

        repo_id = snapshot.id
        repo_root = snapshot.path
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        renumber_parsed = parse_renumber(renumber)

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

        import_report = import_ticket_pack(
            repo_id=repo_id,
            repo_root=repo_root,
            ticket_dir=ticket_dir,
            zip_path=zip_path,
            renumber=renumber_parsed,
            assign_agent=assign_agent,
            clear_model_pin=clear_model_pin,
            template_ref=apply_template,
            template_frontmatter=template_frontmatter,
            lint=True,
            dry_run=False,
            strip_depends_on=False,
            reconcile_depends_on=reconcile_depends_on,
        )

        final_tickets: list[str] = []
        if import_report.ok():
            final_tickets = _append_setup_pack_final_tickets(
                ticket_dir=ticket_dir,
                review_agent=final_review_agent,
                pr_agent=pr_agent,
            )

        lint_errors = validate_tickets(ticket_dir)
        engine = require_repo_config_func(repo_root, config.root)
        preflight = ticket_flow_preflight(engine, ticket_dir)

        payload = {
            "mode": "legacy",
            "repo_id": repo_id,
            "repo_root": str(repo_root),
            "worktree_of": base_repo_id,
            "branch": branch,
            "zip_path": str(zip_path),
            "import": import_report.to_dict(),
            "final_tickets": final_tickets,
            "lint_errors": lint_errors,
            "preflight": preflight.to_dict(),
            "started": False,
        }

        if output_json:
            typer.echo(json.dumps(payload, indent=2))
        else:
            typer.echo(
                f"Setup pack: repo={repo_id} branch={branch} base={base_repo_id} zip={zip_path}"
            )
            _print_ticket_import_report(import_report)
            if final_tickets:
                typer.echo("Final tickets:")
                for rel in final_tickets:
                    typer.echo(f"- {rel}")
            if lint_errors:
                typer.echo("Lint errors:")
                for err in lint_errors:
                    typer.echo(f"- {err}")
            typer.echo("Preflight:")
            print_preflight_report(preflight)

        if (not import_report.ok()) or lint_errors or preflight.has_errors():
            raise_exit("Ticket setup-pack failed.")
        if start:
            ticket_flow_start(repo=repo_root, hub=config.root, force_new=False)
