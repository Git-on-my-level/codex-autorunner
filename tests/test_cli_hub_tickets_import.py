from __future__ import annotations

import zipfile
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.tickets.frontmatter import parse_markdown_frontmatter

runner = CliRunner()


def _make_zip(path: Path, entries: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)


def test_hub_tickets_import_success(hub_env, tmp_path: Path) -> None:
    zip_path = tmp_path / "tickets.zip"
    _make_zip(
        zip_path,
        {
            "tickets/TICKET-001.md": """---
agent: codex
done: false
title: First
model: gpt-5
reasoning: low
---

Body one
""",
            "TICKET-002.md": """---
agent: codex
done: false
title: Second
model: gpt-5
reasoning: high
---

Body two
""",
        },
    )

    result = runner.invoke(
        app,
        [
            "hub",
            "tickets",
            "import",
            "--hub",
            str(hub_env.hub_root),
            "--repo",
            hub_env.repo_id,
            "--zip",
            str(zip_path),
            "--renumber",
            "start=5,step=5",
            "--assign-agent",
            "codex",
            "--clear-model-pin",
        ],
    )
    assert result.exit_code == 0, result.output

    tickets_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    t5 = tickets_dir / "TICKET-005.md"
    t10 = tickets_dir / "TICKET-010.md"
    assert t5.exists()
    assert t10.exists()

    fm1, _ = parse_markdown_frontmatter(t5.read_text(encoding="utf-8"))
    fm2, _ = parse_markdown_frontmatter(t10.read_text(encoding="utf-8"))
    assert fm1.get("agent") == "codex"
    assert fm2.get("agent") == "codex"
    assert "model" not in fm1
    assert "reasoning" not in fm1


def test_hub_tickets_import_invalid_frontmatter_fails(hub_env, tmp_path: Path) -> None:
    zip_path = tmp_path / "bad.zip"
    _make_zip(
        zip_path,
        {
            "TICKET-001.md": "not frontmatter",
            "TICKET-002.md": """---
agent: codex
done: false
title: Ok
---

Body
""",
        },
    )

    result = runner.invoke(
        app,
        [
            "hub",
            "tickets",
            "import",
            "--hub",
            str(hub_env.hub_root),
            "--repo",
            hub_env.repo_id,
            "--zip",
            str(zip_path),
        ],
    )
    assert result.exit_code != 0

    tickets_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    assert not any(p.name.startswith("TICKET-") for p in tickets_dir.iterdir())


def test_hub_tickets_import_dry_run_no_write(hub_env, tmp_path: Path) -> None:
    zip_path = tmp_path / "dry.zip"
    _make_zip(
        zip_path,
        {
            "TICKET-001.md": """---
agent: codex
done: false
title: First
---

Body one
""",
        },
    )

    result = runner.invoke(
        app,
        [
            "hub",
            "tickets",
            "import",
            "--hub",
            str(hub_env.hub_root),
            "--repo",
            hub_env.repo_id,
            "--zip",
            str(zip_path),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0

    tickets_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    assert not any(p.name.startswith("TICKET-") for p in tickets_dir.iterdir())
