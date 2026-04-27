from __future__ import annotations

import io
import sys
from pathlib import Path

from codex_autorunner.core.ticket_manager_cli import (
    _SCRIPT as MANAGER_SCRIPT,
    ensure_ticket_manager,
)
from codex_autorunner.tickets import portable_lint as _portable_lint_module

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _exec_linter_namespace():
    """Exec the shared portable linter source (same module as lint_tickets.py delegates to)."""
    ns: dict = {}
    exec(Path(_portable_lint_module.__file__).read_text(encoding="utf-8"), ns)
    return ns


def _exec_manager_namespace():
    manager_path = ensure_ticket_manager(_REPO_ROOT)
    ns: dict = {"__file__": str(manager_path)}
    exec(MANAGER_SCRIPT, ns)
    return ns


def _make_ticket_dir(tmp_path: Path) -> Path:
    ticket_dir = tmp_path / "tickets"
    ticket_dir.mkdir(parents=True)
    return ticket_dir


def _write_ticket(ticket_dir: Path, name: str, content: str) -> Path:
    path = ticket_dir / name
    path.write_text(content, encoding="utf-8")
    return path


def test_split_frontmatter_both_handle_empty_file() -> None:
    linter_ns = _exec_linter_namespace()
    manager_ns = _exec_manager_namespace()

    linter_result = linter_ns["_split_frontmatter"]("")
    manager_result = manager_ns["_split_frontmatter"]("")

    assert linter_result == (None, ["Empty file; missing YAML frontmatter."])
    assert manager_result == (None, ["Empty file; missing YAML frontmatter."])


def test_split_frontmatter_both_reject_missing_leading_dashes() -> None:
    linter_ns = _exec_linter_namespace()
    manager_ns = _exec_manager_namespace()

    text = "agent: codex\ndone: false\n"
    linter_result = linter_ns["_split_frontmatter"](text)
    manager_result = manager_ns["_split_frontmatter"](text)

    assert linter_result[0] is None
    assert manager_result[0] is None
    assert linter_result[1] == ["Missing YAML frontmatter (expected leading '---')."]
    assert manager_result[1] == ["Missing YAML frontmatter (expected leading '---')."]


def test_split_frontmatter_both_accept_valid_frontmatter() -> None:
    linter_ns = _exec_linter_namespace()
    manager_ns = _exec_manager_namespace()

    text = "---\nagent: codex\ndone: false\n---\nBody"
    linter_result = linter_ns["_split_frontmatter"](text)
    manager_result = manager_ns["_split_frontmatter"](text)

    assert linter_result[1] == []
    assert manager_result[1] == []
    assert "agent: codex" in linter_result[0]
    assert "agent: codex" in manager_result[0]


def test_ticket_paths_both_detect_duplicate_indices(tmp_path: Path) -> None:
    linter_ns = _exec_linter_namespace()
    manager_ns = _exec_manager_namespace()
    ticket_dir = _make_ticket_dir(tmp_path)

    _write_ticket(
        ticket_dir,
        "TICKET-001.md",
        "---\nagent: codex\ndone: false\nticket_id: tkt_a\n---",
    )
    _write_ticket(
        ticket_dir,
        "TICKET-001-copy.md",
        "---\nagent: codex\ndone: false\nticket_id: tkt_b\n---",
    )

    _, linter_errors = linter_ns["_ticket_paths"](ticket_dir)
    _, manager_errors = manager_ns["_ticket_paths"](ticket_dir)

    assert len(linter_errors) == 1
    assert len(manager_errors) == 1
    assert "Duplicate ticket index 001" in linter_errors[0]
    assert "Duplicate ticket index 001" in manager_errors[0]


def test_ticket_paths_no_duplicates(tmp_path: Path) -> None:
    linter_ns = _exec_linter_namespace()
    manager_ns = _exec_manager_namespace()
    ticket_dir = _make_ticket_dir(tmp_path)

    _write_ticket(
        ticket_dir,
        "TICKET-001.md",
        "---\nagent: codex\ndone: false\nticket_id: tkt_a\n---",
    )
    _write_ticket(
        ticket_dir,
        "TICKET-002.md",
        "---\nagent: codex\ndone: false\nticket_id: tkt_b\n---",
    )

    _, linter_errors = linter_ns["_ticket_paths"](ticket_dir)
    _, manager_errors = manager_ns["_ticket_paths"](ticket_dir)

    assert linter_errors == []
    assert manager_errors == []


def test_cmd_lint_detects_duplicate_ticket_ids_across_all_files(tmp_path: Path) -> None:
    manager_ns = _exec_manager_namespace()
    ticket_dir = _make_ticket_dir(tmp_path)

    _write_ticket(
        ticket_dir,
        "TICKET-001.md",
        "---\nagent: codex\ndone: false\nticket_id: tkt_shared123\n---",
    )
    _write_ticket(
        ticket_dir,
        "TICKET-002.md",
        "---\nagent: opencode\ndone: true\nticket_id: tkt_shared123\n---",
    )

    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        rc = manager_ns["cmd_lint"](ticket_dir, fix_ticket_ids=False)
    finally:
        captured = sys.stderr.getvalue()
        sys.stderr = old_stderr

    assert rc != 0
    assert "Duplicate ticket_id" in captured
    assert "tkt_shared123" in captured


def test_cmd_lint_clean_tickets(tmp_path: Path) -> None:
    manager_ns = _exec_manager_namespace()
    ticket_dir = _make_ticket_dir(tmp_path)

    _write_ticket(
        ticket_dir,
        "TICKET-001.md",
        "---\nagent: codex\ndone: false\nticket_id: tkt_one123456\n---\n## Goal\n",
    )
    _write_ticket(
        ticket_dir,
        "TICKET-002.md",
        "---\nagent: opencode\ndone: true\nticket_id: tkt_two123456\n---\n## Goal\n",
    )

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = manager_ns["cmd_lint"](ticket_dir, fix_ticket_ids=False)
    finally:
        captured = sys.stdout.getvalue()
        sys.stdout = old_stdout

    assert rc == 0
    assert "2 ticket(s) linted" in captured


def test_read_ticket_id_both_agree(tmp_path: Path) -> None:
    linter_ns = _exec_linter_namespace()
    manager_ns = _exec_manager_namespace()

    path = tmp_path / "TICKET-001.md"
    path.write_text(
        "---\nagent: codex\ndone: false\nticket_id: tkt_abc12345\n---", encoding="utf-8"
    )

    linter_id = linter_ns["_read_ticket_id"](path)
    manager_id = manager_ns["_read_ticket_id"](path)

    assert linter_id == "tkt_abc12345"
    assert manager_id == "tkt_abc12345"


def test_read_ticket_id_returns_none_on_parse_error(tmp_path: Path) -> None:
    linter_ns = _exec_linter_namespace()
    manager_ns = _exec_manager_namespace()

    path = tmp_path / "TICKET-001.md"
    path.write_text("not frontmatter at all", encoding="utf-8")

    assert linter_ns["_read_ticket_id"](path) is None
    assert manager_ns["_read_ticket_id"](path) is None


def test_cmd_lint_detects_duplicate_ticket_ids_even_with_other_errors(
    tmp_path: Path,
) -> None:
    manager_ns = _exec_manager_namespace()
    ticket_dir = _make_ticket_dir(tmp_path)

    _write_ticket(
        ticket_dir,
        "TICKET-001.md",
        "---\nagent: codex\ndone: false\nticket_id: tkt_dup_shared\n---",
    )
    _write_ticket(
        ticket_dir,
        "TICKET-002.md",
        "---\nagent: invalid_agent\ndone: false\nticket_id: tkt_dup_shared\n---",
    )

    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        rc = manager_ns["cmd_lint"](ticket_dir, fix_ticket_ids=False)
    finally:
        captured = sys.stderr.getvalue()
        sys.stderr = old_stderr

    assert rc != 0
    assert "Duplicate ticket_id" in captured
    assert "tkt_dup_shared" in captured
