from pathlib import Path

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.engine import Engine
from codex_autorunner.core.prompt import build_final_summary_prompt, build_prompt_text


def test_prompt_calls_out_work_doc_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    seed_hub_files(repo, force=True)
    seed_repo_files(repo, git_required=False)

    engine = Engine(repo)
    prompt = build_final_summary_prompt(
        engine.config, engine.docs, prev_run_output=None
    )

    assert ".codex-autorunner/TODO.md" in prompt
    assert ".codex-autorunner/PROGRESS.md" in prompt
    assert ".codex-autorunner/OPINIONS.md" in prompt
    assert ".codex-autorunner/SPEC.md" in prompt
    assert ".codex-autorunner/SUMMARY.md" in prompt
    assert "FINAL user-facing report" in prompt


def test_build_prompt_text_includes_prev_run_block() -> None:
    template = "TODO={{TODO}}\nPATH={{TODO_PATH}}\n{{PREV_RUN_OUTPUT}}"
    rendered = build_prompt_text(
        template=template,
        docs={"todo": "Do the thing"},
        doc_paths={"todo": "TODO.md"},
        prev_run_output="finished",
    )

    assert "TODO=Do the thing" in rendered
    assert "PATH=TODO.md" in rendered
    assert "<PREV_RUN_OUTPUT>" in rendered
    assert "finished" in rendered


def test_build_prompt_text_single_pass_replacement() -> None:
    template = "TODO={{TODO}}\nSPEC={{SPEC}}"
    rendered = build_prompt_text(
        template=template,
        docs={"todo": "Check {{SPEC}} details", "spec": "Spec contents"},
        doc_paths={},
        prev_run_output=None,
    )

    assert "Check {{SPEC}} details" in rendered
    assert "SPEC=Spec contents" in rendered
