from __future__ import annotations

import zipfile
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.surfaces.cli.cli import FLOW_COMMANDS
from codex_autorunner.tickets.frontmatter import parse_markdown_frontmatter

runner = CliRunner()


def test_hub_tickets_doctor_fix_preserves_depends_on(hub_env) -> None:
    ticket = hub_env.repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text(
        '---\nagent: codex\ndone: "false"\ndepends_on:\n  - TICKET-000\n---\n\nBody\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "hub",
            "tickets",
            "doctor",
            "--path",
            str(hub_env.hub_root),
            "--repo",
            hub_env.repo_id,
            "--fix",
        ],
    )
    assert result.exit_code == 0, result.output

    raw = ticket.read_text(encoding="utf-8")
    fm, _ = parse_markdown_frontmatter(raw)
    assert fm.get("depends_on") == ["TICKET-000"]
    assert fm.get("done") is False
    assert "CAR ticket note: depends_on=" not in raw


def test_hub_tickets_fmt_check_fails_on_drift(hub_env) -> None:
    ticket = hub_env.repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text(
        "---\nagent: codex\ndone: false\ndepends_on:\n  - TICKET-000\n---\n\nBody\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "hub",
            "tickets",
            "fmt",
            "--path",
            str(hub_env.hub_root),
            "--repo",
            hub_env.repo_id,
            "--check",
        ],
    )
    assert result.exit_code != 0
    assert "changed=1" in result.output


def test_hub_tickets_bulk_set_profile_updates_frontmatter(hub_env) -> None:
    ticket = hub_env.repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text(
        "---\nagent: hermes\ndone: false\nticket_id: tkt_bulksetprofile001\n---\n\nBody\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "hub",
            "tickets",
            "bulk-set",
            "--path",
            str(hub_env.hub_root),
            "--repo",
            hub_env.repo_id,
            "--agent",
            "hermes",
            "--profile",
            "m4-pma",
        ],
    )
    assert result.exit_code == 0, result.output

    frontmatter, _ = parse_markdown_frontmatter(ticket.read_text(encoding="utf-8"))
    assert frontmatter["agent"] == "hermes"
    assert frontmatter["profile"] == "m4-pma"


def test_hub_tickets_setup_pack_creates_final_tickets(
    hub_env, tmp_path: Path, monkeypatch
) -> None:
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("TICKET-001.md", "---\nagent: codex\ndone: false\n---\n\nBody\n")

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.cli._ticket_flow_preflight",
        lambda *_args, **_kwargs: FLOW_COMMANDS.PreflightReport(
            checks=[
                FLOW_COMMANDS.PreflightCheck(
                    check_id="frontmatter",
                    status="ok",
                    message="ok",
                )
            ]
        ),
    )

    result = runner.invoke(
        app,
        [
            "hub",
            "tickets",
            "setup-pack",
            str(hub_env.repo_root),
            "--from",
            str(zip_path),
            "--append-final-tickets",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    payload = result.stdout
    assert "final-review" in payload
    assert "open-pr" in payload
    assert "preflight" in payload

    pr_ticket = (
        hub_env.repo_root / ".codex-autorunner" / "tickets" / "TICKET-003-open-pr.md"
    )
    _frontmatter, body = parse_markdown_frontmatter(
        pr_ticket.read_text(encoding="utf-8")
    )
    assert "ready-for-review PR unless the user explicitly asked for a draft" in body
