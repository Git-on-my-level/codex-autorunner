from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from codex_autorunner.tickets.files import read_ticket
from codex_autorunner.tickets.models import TicketRunConfig
from codex_autorunner.tickets.runner import (
    CAR_HUD_MAX_CHARS,
    CAR_HUD_MAX_LINES,
)
from codex_autorunner.tickets.runner_prompt import build_prompt


def _make_outbox(workspace_root: Path) -> MagicMock:
    outbox_paths = MagicMock()
    outbox_paths.dispatch_dir = (
        workspace_root / ".codex-autorunner" / "runs" / "run-1" / "dispatch"
    )
    outbox_paths.dispatch_path = (
        workspace_root / ".codex-autorunner" / "runs" / "run-1" / "DISPATCH.md"
    )
    return outbox_paths


def _write_installed_app(repo_root: Path, app_id: str, version: str = "0.1.0") -> None:
    apps_root = repo_root / ".codex-autorunner" / "apps"
    app_root = apps_root / app_id
    bundle_root = app_root / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "car-app.yaml").write_text(
        f"schema_version: 1\nid: {app_id}\nname: {app_id}\n"
        f"version: {version}\ndescription: test\n"
        f"tools:\n  run:\n    argv: ['python3', 'scripts/run.py']\n",
        encoding="utf-8",
    )
    (app_root / "state").mkdir(exist_ok=True)
    (app_root / "artifacts").mkdir(exist_ok=True)
    lock = {
        "id": app_id,
        "version": version,
        "source_repo_id": "test",
        "source_url": "https://example.com",
        "source_path": f"apps/{app_id}",
        "source_ref": "main",
        "commit_sha": "a" * 40,
        "manifest_sha": "b" * 64,
        "bundle_sha": "c" * 64,
        "trusted": True,
        "installed_at": "2026-01-01T00:00:00Z",
    }
    (app_root / "app.lock.json").write_text(json.dumps(lock) + "\n", encoding="utf-8")


def test_ticket_flow_prompt_boundaries(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        "---\nagent: codex\ndone: false\n---\nGoal: Test boundaries\n",
        encoding="utf-8",
    )

    outbox_paths = _make_outbox(workspace_root)

    ticket_doc, _ = read_ticket(ticket_path)
    prompt = build_prompt(
        ticket_path=ticket_path,
        workspace_root=workspace_root,
        ticket_doc=ticket_doc,
        last_agent_output=None,
        outbox_paths=outbox_paths,
        lint_errors=None,
        prompt_max_bytes=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ).prompt_max_bytes,
    )

    assert "<CAR_TICKET_FLOW_PROMPT>" in prompt
    assert "</CAR_TICKET_FLOW_PROMPT>" in prompt
    assert "<CAR_HUD>" in prompt
    assert "</CAR_HUD>" in prompt
    assert "<CAR_CURRENT_TICKET_FILE>" in prompt
    assert "</CAR_CURRENT_TICKET_FILE>" in prompt
    assert "<TICKET_MARKDOWN>" in prompt
    assert "</TICKET_MARKDOWN>" in prompt

    hud_start = prompt.index("<CAR_HUD>") + len("<CAR_HUD>\n")
    hud_end = prompt.index("</CAR_HUD>")
    hud = prompt[hud_start:hud_end].rstrip("\n")
    assert len(hud) <= CAR_HUD_MAX_CHARS
    assert len(hud.splitlines()) <= CAR_HUD_MAX_LINES
    assert "car describe --json" in hud

    start = prompt.index("<CAR_CURRENT_TICKET_FILE>")
    end = prompt.index("</CAR_CURRENT_TICKET_FILE>")
    section = prompt[start:end]
    path_marker = "PATH: .codex-autorunner/tickets/TICKET-001.md"
    assert path_marker in section


def test_prompt_no_apps_installed_omits_apps_section(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        "---\nagent: codex\ndone: false\n---\nGoal: No apps\n",
        encoding="utf-8",
    )

    ticket_doc, _ = read_ticket(ticket_path)
    prompt = build_prompt(
        ticket_path=ticket_path,
        workspace_root=workspace_root,
        ticket_doc=ticket_doc,
        last_agent_output=None,
        outbox_paths=_make_outbox(workspace_root),
        lint_errors=None,
    )

    assert "<CAR_INSTALLED_APPS>" not in prompt


def test_prompt_with_installed_apps_includes_hint(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        "---\nagent: codex\ndone: false\n---\nGoal: With apps\n",
        encoding="utf-8",
    )
    _write_installed_app(workspace_root, "test.app", version="1.2.3")

    ticket_doc, _ = read_ticket(ticket_path)
    prompt = build_prompt(
        ticket_path=ticket_path,
        workspace_root=workspace_root,
        ticket_doc=ticket_doc,
        last_agent_output=None,
        outbox_paths=_make_outbox(workspace_root),
        lint_errors=None,
    )

    assert "<CAR_INSTALLED_APPS>" in prompt
    assert "</CAR_INSTALLED_APPS>" in prompt
    assert "test.app v1.2.3" in prompt
    assert "car apps run test.app run -- ..." in prompt
