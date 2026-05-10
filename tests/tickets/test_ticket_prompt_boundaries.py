from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codex_autorunner.tickets.files import read_ticket
from codex_autorunner.tickets.models import TicketRunConfig
from codex_autorunner.tickets.runner_prompt import (
    CAR_HUD_MAX_CHARS,
    CAR_HUD_MAX_LINES,
    build_prompt,
)
from codex_autorunner.tickets.runner_prompt_support import REQUIRED_PROMPT_MARKERS


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
    assert "<CAR_TICKET>" in prompt
    assert "</CAR_TICKET>" in prompt

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


@pytest.mark.parametrize(
    ("agent_line", "expected_agent"),
    [
        ("agent: codex", "agent: codex"),
        ('agent: "codex"', 'agent: "codex"'),
        ("agent: opencode", "agent: opencode"),
    ],
)
def test_ticket_flow_prompt_shape_is_not_agent_specific(
    tmp_path: Path, agent_line: str, expected_agent: str
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        f"---\n{agent_line}\ndone: false\n---\nGoal: Test agent prompt shape\n",
        encoding="utf-8",
    )

    ticket_doc, errors = read_ticket(ticket_path)
    assert errors == []
    prompt = build_prompt(
        ticket_path=ticket_path,
        workspace_root=workspace_root,
        ticket_doc=ticket_doc,
        last_agent_output=None,
        outbox_paths=_make_outbox(workspace_root),
        lint_errors=None,
        requested_context="(requested context placeholder)",
    )

    assert "<CAR_HUD>" in prompt
    assert "<CAR_REQUESTED_CONTEXT>" in prompt
    assert expected_agent in prompt
    for marker in REQUIRED_PROMPT_MARKERS:
        assert marker in prompt
    assert "Ticket-first fallback prompt" not in prompt


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


def test_prompt_budget_trims_previous_output_before_compacting(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        "---\nagent: codex\ndone: false\n---\nGoal: Preserve replies\n",
        encoding="utf-8",
    )
    ticket_doc, errors = read_ticket(ticket_path)
    assert errors == []
    base_prompt = build_prompt(
        ticket_path=ticket_path,
        workspace_root=workspace_root,
        ticket_doc=ticket_doc,
        last_agent_output=None,
        outbox_paths=_make_outbox(workspace_root),
        lint_errors=None,
        reply_context="KEEP_REPLY",
        requested_context="KEEP_REQUESTED_CONTEXT",
    )
    budget = len(base_prompt.encode("utf-8")) + 100

    prompt = build_prompt(
        ticket_path=ticket_path,
        workspace_root=workspace_root,
        ticket_doc=ticket_doc,
        last_agent_output="z" * 500_000,
        outbox_paths=_make_outbox(workspace_root),
        lint_errors=None,
        reply_context="KEEP_REPLY",
        requested_context="KEEP_REQUESTED_CONTEXT",
        prompt_max_bytes=budget,
    )

    assert len(prompt.encode("utf-8")) <= budget
    assert "KEEP_REPLY" in prompt
    assert "KEEP_REQUESTED_CONTEXT" in prompt
    assert "<CAR_HUMAN_REPLIES>" in prompt
    assert "[... TRUNCATED ...]" in prompt


def test_prompt_budget_can_drop_static_warnings_without_crashing(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        "---\nagent: codex\ndone: false\n---\nGoal: Tight static warnings\n",
        encoding="utf-8",
    )
    ticket_doc, errors = read_ticket(ticket_path)
    assert errors == []

    prompt = build_prompt(
        ticket_path=ticket_path,
        workspace_root=workspace_root,
        ticket_doc=ticket_doc,
        last_agent_output="z" * 500_000,
        last_checkpoint_error="checkpoint failed: " + ("x" * 20_000),
        commit_required=True,
        commit_attempt=1,
        commit_max_attempts=2,
        outbox_paths=_make_outbox(workspace_root),
        lint_errors=["bad frontmatter: " + ("y" * 20_000)],
        prior_no_change_turns=3,
        prompt_max_bytes=1200,
    )

    assert len(prompt.encode("utf-8")) <= 1200
    for marker in REQUIRED_PROMPT_MARKERS:
        assert marker in prompt
