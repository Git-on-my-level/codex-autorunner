from pathlib import Path

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.engine import Engine
from codex_autorunner.core.snapshot import (
    collect_seed_context,
    redact_text,
)


def test_redaction_scrubs_common_tokens() -> None:
    text = "sk-1234567890abcdefghijkl ghp_1234567890abcdefghijkl AKIA1234567890ABCDEF eyJhbGciOiJIUzI1NiJ9.eyJmb28iOiJiYXIifQ.abcDEF123_-"
    out = redact_text(text)
    assert "sk-1234567890" not in out
    assert "ghp_1234567890" not in out
    assert "AKIA1234567890ABCDEF" not in out
    assert "eyJhbGciOiJIUzI1NiJ9." not in out


def test_collect_seed_context_avoids_secret_paths_and_bounds_excerpts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    seed_hub_files(repo, force=True)
    seed_repo_files(repo, git_required=False)

    # Secret-ish files: should not appear in tree outline.
    (repo / ".env").write_text(
        "OPENAI_API_KEY=sk-1234567890abcdefghijkl\n", encoding="utf-8"
    )
    (repo / "secret.pem").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")

    # Normal source file: should be visible.
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

    # Make TODO doc huge so excerpt is skipped (>200KB default per-file cap).
    todo_path = repo / ".codex-autorunner" / "TODO.md"
    todo_path.write_text("# TODO\n\n" + ("x" * 210_000), encoding="utf-8")

    engine = Engine(repo)
    seed = collect_seed_context(engine)

    assert ".env" not in seed.text
    assert "secret.pem" not in seed.text
    assert "- src" in seed.text
    assert "  - app.py" in seed.text
    assert "Skipped excerpt" in seed.text
    # Ensure redaction ran on excerpts we did include.
    assert "sk-1234567890" not in seed.text
