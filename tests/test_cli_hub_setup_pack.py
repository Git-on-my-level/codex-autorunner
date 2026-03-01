from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.cli import app
from codex_autorunner.surfaces.cli import cli as cli_module
from codex_autorunner.tickets.frontmatter import parse_markdown_frontmatter

runner = CliRunner()


def _make_zip(path: Path, entries: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)


def _ok_preflight() -> cli_module.PreflightReport:
    return cli_module.PreflightReport(
        checks=[
            cli_module.PreflightCheck(check_id="frontmatter", status="ok", message="ok")
        ]
    )


def _error_preflight() -> cli_module.PreflightReport:
    return cli_module.PreflightReport(
        checks=[
            cli_module.PreflightCheck(
                check_id="frontmatter",
                status="error",
                message="frontmatter failed",
                details=["TICKET-001.md: invalid frontmatter"],
            )
        ]
    )


def test_hub_tickets_setup_pack_new_mode_assigns_and_preserves_depends_on(
    hub_env, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.cli._ticket_flow_preflight",
        lambda *_args, **_kwargs: _ok_preflight(),
    )
    zip_path = tmp_path / "pack.zip"
    _make_zip(
        zip_path,
        {
            "tickets/TICKET-001.md": (
                "---\n"
                "agent: codex\n"
                "done: false\n"
                "depends_on:\n"
                "  - TICKET-002\n"
                "---\n\n"
                "Body 1\n"
            ),
            "tickets/TICKET-002.md": (
                "---\nagent: codex\ndone: false\n---\n\nBody 2\n"
            ),
        },
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
            "--assign",
            "opencode:001",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    fm1, _ = parse_markdown_frontmatter(
        (ticket_dir / "TICKET-001.md").read_text(encoding="utf-8")
    )
    fm2, _ = parse_markdown_frontmatter(
        (ticket_dir / "TICKET-002.md").read_text(encoding="utf-8")
    )
    assert fm1.get("agent") == "opencode"
    assert fm2.get("agent") == "codex"
    assert fm1.get("depends_on") == ["TICKET-002"]

    receipt_path = ticket_dir / "ingest_state.json"
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == 1
    assert receipt["ingested"] is True
    assert receipt["source"] == "setup_pack_new"
    assert receipt["ingested_at"]


def test_hub_tickets_setup_pack_new_mode_start_invokes_ticket_flow_start(
    hub_env, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.cli._ticket_flow_preflight",
        lambda *_args, **_kwargs: _ok_preflight(),
    )
    started: list[Path] = []

    def _fake_start(*, repo, hub, force_new):  # type: ignore[no-untyped-def]
        assert hub is None
        assert force_new is False
        started.append(Path(repo))

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.cli.ticket_flow_start", _fake_start
    )

    zip_path = tmp_path / "pack-start.zip"
    _make_zip(
        zip_path,
        {"TICKET-001.md": "---\nagent: codex\ndone: false\n---\n\nBody\n"},
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
            "--start",
        ],
    )
    assert result.exit_code == 0, result.output
    assert started == [hub_env.repo_root.resolve()]


def test_hub_tickets_setup_pack_new_mode_fails_fast_on_preflight_errors(
    hub_env, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.cli._ticket_flow_preflight",
        lambda *_args, **_kwargs: _error_preflight(),
    )
    zip_path = tmp_path / "pack-preflight.zip"
    _make_zip(
        zip_path,
        {"TICKET-001.md": "---\nagent: codex\ndone: false\n---\n\nBody\n"},
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
        ],
    )
    assert result.exit_code != 0
    assert "failed preflight" in result.output.lower()


def test_hub_tickets_setup_pack_new_mode_ignores_non_ticket_markdown(
    hub_env, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.cli._ticket_flow_preflight",
        lambda *_args, **_kwargs: _ok_preflight(),
    )
    zip_path = tmp_path / "pack-with-readme.zip"
    _make_zip(
        zip_path,
        {
            "README.md": "# note\n",
            "tickets/TICKET-001.md": "---\nagent: codex\ndone: false\n---\n\nBody\n",
        },
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
        ],
    )
    assert result.exit_code == 0, result.output
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    assert (ticket_dir / "TICKET-001.md").exists()
    assert not (ticket_dir / "README.md").exists()


def test_hub_tickets_setup_pack_legacy_mode_allows_start_flag(
    hub_env, tmp_path: Path, monkeypatch
) -> None:
    zip_path = tmp_path / "legacy-pack.zip"
    _make_zip(
        zip_path,
        {"TICKET-001.md": "---\nagent: codex\ndone: false\n---\n\nBody\n"},
    )

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
    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.cli._ticket_flow_preflight",
        lambda *_args, **_kwargs: _ok_preflight(),
    )
    started: list[Path] = []

    def _fake_start(*, repo, hub, force_new):  # type: ignore[no-untyped-def]
        assert force_new is False
        started.append(Path(repo))

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.cli.ticket_flow_start", _fake_start
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
            "feature/legacy-start",
            "--zip",
            str(zip_path),
            "--start",
        ],
    )
    assert result.exit_code == 0, result.output
    assert started
