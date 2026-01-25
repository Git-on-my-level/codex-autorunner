from pathlib import Path

from codex_autorunner.core.about_car import (
    ABOUT_CAR_GENERATED_MARKER,
    ABOUT_CAR_REL_PATH,
)
from codex_autorunner.core.engine import Engine
from codex_autorunner.routes.shared import build_codex_terminal_cmd


def test_about_car_is_seeded(repo: Path):
    about_path = repo / ABOUT_CAR_REL_PATH
    assert about_path.exists()
    text = about_path.read_text(encoding="utf-8")
    assert ABOUT_CAR_GENERATED_MARKER in text
    assert "ABOUT_CAR" in text
    assert ".codex-autorunner/workspace/active_context.md" in text
    assert ".codex-autorunner/workspace/decisions.md" in text
    assert ".codex-autorunner/workspace/spec.md" in text


def test_terminal_new_cmd_does_not_seed_about_prompt(repo: Path):
    engine = Engine(repo)
    about_text = (repo / ABOUT_CAR_REL_PATH).read_text(encoding="utf-8")
    cmd = build_codex_terminal_cmd(engine, resume_mode=False)
    assert "--input" not in cmd
    assert about_text not in cmd


def test_terminal_resume_cmd_does_not_seed_about_prompt(repo: Path):
    engine = Engine(repo)
    about_text = (repo / ABOUT_CAR_REL_PATH).read_text(encoding="utf-8")
    cmd = build_codex_terminal_cmd(engine, resume_mode=True)
    assert "resume" in cmd
    assert about_text not in cmd
