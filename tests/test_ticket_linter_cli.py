from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

from codex_autorunner.core.ticket_linter_cli import LINTER_REL_PATH


def _run_linter(repo: Path) -> subprocess.CompletedProcess[str]:
    linter_path = repo / LINTER_REL_PATH
    return subprocess.run(
        [sys.executable, str(linter_path)],
        cwd=repo,
        text=True,
        capture_output=True,
    )


def test_linter_is_seeded_with_repo(repo: Path) -> None:
    linter_path = repo / LINTER_REL_PATH
    assert linter_path.exists()
    mode = linter_path.stat().st_mode
    assert mode & stat.S_IXUSR, "linter should be executable"


def test_linter_rejects_invalid_filename_and_extension(repo: Path) -> None:
    tickets_dir = repo / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)

    invalid_only = tickets_dir / "NOTE-001.md"
    invalid_only.write_text(
        "---\nagent: codex\ndone: false\n---\nBody\n", encoding="utf-8"
    )
    result_only_invalid = _run_linter(repo)
    assert result_only_invalid.returncode == 1
    assert "Invalid ticket filename" in result_only_invalid.stderr

    invalid_only.unlink()

    good = tickets_dir / "TICKET-001.md"
    good.write_text("---\nagent: codex\ndone: false\n---\nBody\n", encoding="utf-8")

    invalid_prefix = tickets_dir / "NOTE-001.md"
    invalid_prefix.write_text(
        "---\nagent: codex\ndone: false\n---\nBody\n", encoding="utf-8"
    )
    result_prefix = _run_linter(repo)
    assert result_prefix.returncode == 1
    assert "Invalid ticket filename" in result_prefix.stderr

    invalid_prefix.unlink()

    invalid_ext = tickets_dir / "TICKET-002-bad.txt"
    invalid_ext.write_text(
        "---\nagent: codex\ndone: false\n---\nBody\n", encoding="utf-8"
    )
    result_ext = _run_linter(repo)
    assert result_ext.returncode == 1
    assert "Invalid ticket filename" in result_ext.stderr

    invalid_ext.unlink()

    result_good = _run_linter(repo)
    assert result_good.returncode == 0
    assert "OK" in result_good.stdout


def test_linter_flags_invalid_yaml_with_suffix(repo: Path) -> None:
    tickets_dir = repo / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)

    bad = tickets_dir / "TICKET-999-sse-resume.md"
    bad.write_text(
        "---\nagent: codex\ntitle: Foo: Bar\ndone: false\n---\nBody\n",
        encoding="utf-8",
    )
    result_bad = _run_linter(repo)
    assert result_bad.returncode == 1
    assert "YAML parse error" in result_bad.stderr

    good = tickets_dir / "TICKET-000.md"
    good.write_text(
        "---\nagent: codex\ndone: false\n---\nBody\n",
        encoding="utf-8",
    )
    # Remove the bad ticket to allow a clean run.
    bad.unlink()

    result_good = _run_linter(repo)
    assert result_good.returncode == 0
    assert "OK" in result_good.stdout
