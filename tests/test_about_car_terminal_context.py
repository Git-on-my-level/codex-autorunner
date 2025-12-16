from pathlib import Path

from codex_autorunner.about_car import ABOUT_CAR_GENERATED_MARKER, ABOUT_CAR_REL_PATH
from codex_autorunner.api_routes import build_codex_terminal_cmd
from codex_autorunner.engine import Engine


def test_about_car_is_seeded(repo: Path):
    about_path = repo / ABOUT_CAR_REL_PATH
    assert about_path.exists()
    text = about_path.read_text(encoding="utf-8")
    assert ABOUT_CAR_GENERATED_MARKER in text
    assert "ABOUT_CAR" in text
    assert ".codex-autorunner/TODO.md" in text
    assert "add this to the TODOs" in text


def test_terminal_new_cmd_uses_input_when_supported(repo: Path, monkeypatch):
    engine = Engine(repo)
    about_path = repo / ABOUT_CAR_REL_PATH
    about_text = about_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "codex_autorunner.api_routes._codex_supports_input_flag", lambda _bin: True
    )
    cmd = build_codex_terminal_cmd(engine, resume_mode=False)
    assert "--input" in cmd
    idx = cmd.index("--input")
    assert cmd[idx + 1] == str(about_path)
    assert about_text not in cmd


def test_terminal_new_cmd_falls_back_to_prompt_text(repo: Path, monkeypatch):
    engine = Engine(repo)
    about_path = repo / ABOUT_CAR_REL_PATH
    about_text = about_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "codex_autorunner.api_routes._codex_supports_input_flag", lambda _bin: False
    )
    cmd = build_codex_terminal_cmd(engine, resume_mode=False)
    assert cmd[-1] == about_text


def test_terminal_resume_cmd_does_not_seed_about_prompt(repo: Path, monkeypatch):
    engine = Engine(repo)
    about_text = (repo / ABOUT_CAR_REL_PATH).read_text(encoding="utf-8")
    monkeypatch.setattr(
        "codex_autorunner.api_routes._codex_supports_input_flag", lambda _bin: True
    )
    cmd = build_codex_terminal_cmd(engine, resume_mode=True)
    assert "resume" in cmd
    assert about_text not in cmd


