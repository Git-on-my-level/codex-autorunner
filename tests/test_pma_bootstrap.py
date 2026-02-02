from pathlib import Path

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import load_hub_config


def test_pma_files_created_on_hub_init(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)

    pma_dir = tmp_path / ".codex-autorunner" / "pma"
    assert pma_dir.exists()
    assert pma_dir.is_dir()

    prompt_path = pma_dir / "prompt.md"
    assert prompt_path.exists()
    prompt_content = prompt_path.read_text(encoding="utf-8")
    assert "Project Management Agent" in prompt_content
    assert "You are the hub-level" in prompt_content

    about_path = pma_dir / "ABOUT_CAR.md"
    assert about_path.exists()
    about_content = about_path.read_text(encoding="utf-8")
    assert "PMA Operations Guide" in about_content
    assert "Ticket flow" in about_content


def test_pma_config_defaults(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)

    config = load_hub_config(tmp_path)
    assert "pma" in config.raw
    pma_config = config.raw["pma"]
    assert isinstance(pma_config, dict)
    assert pma_config.get("enabled") is True
    assert pma_config.get("default_agent") == "codex"
    assert pma_config.get("model") is None
    assert pma_config.get("reasoning") is None
    assert pma_config.get("max_repos") == 25
    assert pma_config.get("max_messages") == 10
    assert pma_config.get("max_text_chars") == 800


def test_pma_files_not_overridden_without_force(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)

    pma_dir = tmp_path / ".codex-autorunner" / "pma"
    prompt_path = pma_dir / "prompt.md"
    about_path = pma_dir / "ABOUT_CAR.md"

    prompt_path.write_text("custom prompt", encoding="utf-8")
    about_path.write_text("custom about", encoding="utf-8")

    seed_hub_files(tmp_path, force=False)

    assert prompt_path.read_text(encoding="utf-8") == "custom prompt"
    assert about_path.read_text(encoding="utf-8") == "custom about"
