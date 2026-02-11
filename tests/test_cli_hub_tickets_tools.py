from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.cli import app
from codex_autorunner.tickets.frontmatter import parse_markdown_frontmatter

runner = CliRunner()


def test_hub_tickets_doctor_fix_removes_depends_on(hub_env) -> None:
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
            "--hub",
            str(hub_env.hub_root),
            "--repo",
            hub_env.repo_id,
            "--fix",
        ],
    )
    assert result.exit_code == 0, result.output

    raw = ticket.read_text(encoding="utf-8")
    fm, _ = parse_markdown_frontmatter(raw)
    assert "depends_on" not in fm
    assert fm.get("done") is False
    assert "CAR ticket note: depends_on=" in raw


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
            "--hub",
            str(hub_env.hub_root),
            "--repo",
            hub_env.repo_id,
            "--check",
        ],
    )
    assert result.exit_code != 0
    assert "drift" in result.output.lower()


def test_hub_tickets_setup_pack_creates_final_tickets(
    hub_env, tmp_path: Path, monkeypatch
) -> None:
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("TICKET-001.md", "---\nagent: codex\ndone: false\n---\n\nBody\n")

    def _fake_create_worktree(self, base_repo_id: str, branch: str, **_kwargs):
        repo_id = f"{base_repo_id}--{branch}"
        repo_root = self.hub_config.root / "worktrees" / repo_id
        repo_root.mkdir(parents=True, exist_ok=True)
        (repo_root / ".git").mkdir(exist_ok=True)
        seed_repo_files(repo_root, git_required=False)
        return SimpleNamespace(id=repo_id, path=repo_root, branch=branch)

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.cli.HubSupervisor.create_worktree",
        _fake_create_worktree,
    )

    result = runner.invoke(
        app,
        [
            "hub",
            "tickets",
            "setup-pack",
            "--hub",
            str(hub_env.hub_root),
            "--base-repo",
            hub_env.repo_id,
            "--branch",
            "feature/setup-pack",
            "--zip",
            str(zip_path),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    payload = result.stdout
    assert "final-review" in payload
    assert "open-pr" in payload
    assert "preflight" in payload
