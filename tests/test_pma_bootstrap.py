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

    notes_path = pma_dir / "notes.md"
    assert notes_path.exists()
    assert notes_path.read_text(encoding="utf-8") == ""


def test_pma_config_defaults(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)

    config = load_hub_config(tmp_path)
    assert "pma" in config.raw
    pma_config = config.raw["pma"]
    assert isinstance(pma_config, dict)
    assert pma_config.get("enabled") is False
    assert pma_config.get("default_agent") == "codex"
    assert pma_config.get("model") is None
    assert pma_config.get("reasoning") is None


def test_pma_files_not_overridden_without_force(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)

    pma_dir = tmp_path / ".codex-autorunner" / "pma"
    prompt_path = pma_dir / "prompt.md"
    notes_path = pma_dir / "notes.md"

    prompt_path.write_text("custom prompt", encoding="utf-8")
    notes_path.write_text("custom notes", encoding="utf-8")

    seed_hub_files(tmp_path, force=False)

    assert prompt_path.read_text(encoding="utf-8") == "custom prompt"
    assert notes_path.read_text(encoding="utf-8") == "custom notes"
