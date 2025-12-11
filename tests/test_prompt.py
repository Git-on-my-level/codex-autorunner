from pathlib import Path

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.engine import Engine
from codex_autorunner.prompt import build_prompt


def test_prompt_calls_out_work_doc_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    seed_repo_files(repo, git_required=False)

    engine = Engine(repo)
    prompt = build_prompt(engine.config, engine.docs, prev_run_output=None)

    assert ".codex-autorunner/TODO.md" in prompt
    assert ".codex-autorunner/PROGRESS.md" in prompt
    assert ".codex-autorunner/OPINIONS.md" in prompt
    assert ".codex-autorunner/SPEC.md" in prompt
    assert "Edit these files directly; do not create new copies elsewhere" in prompt
