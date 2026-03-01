from pathlib import Path

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import load_hub_config


def test_pma_files_created_on_hub_init(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)

    pma_dir = tmp_path / ".codex-autorunner" / "pma"
    docs_dir = pma_dir / "docs"
    assert pma_dir.exists()
    assert pma_dir.is_dir()
    assert docs_dir.exists()

    prompt_path = docs_dir / "prompt.md"
    assert prompt_path.exists()
    prompt_content = prompt_path.read_text(encoding="utf-8")
    assert "CAR:PMA_DOCS_GENERATED" in prompt_content
    assert "Project Management Agent" in prompt_content
    assert "You are the hub-level" in prompt_content
    assert "Ticket planning constraints" in prompt_content
    assert "ascending numeric order" in prompt_content
    assert "Destinations (execution runtime)" in prompt_content
    assert "car hub destination show" in prompt_content
    assert "car hub destination set <repo_id> docker --image <image>" in prompt_content
    assert "active_context.md" in prompt_content
    assert "decisions.md" in prompt_content
    assert "spec.md" in prompt_content
    assert "car pma thread" in prompt_content

    about_path = docs_dir / "ABOUT_CAR.md"
    assert about_path.exists()
    about_content = about_path.read_text(encoding="utf-8")
    assert "CAR:PMA_DOCS_GENERATED" in about_content
    assert "PMA Operations Guide" in about_content
    assert "Ticket flow" in about_content
    assert "Ticket flow mechanics (planning constraints)" in about_content
    assert "Ticket turn prompt context" in about_content
    assert "Destinations (local/docker runtime)" in about_content
    assert "car hub destination show" in about_content
    assert ".codex-autorunner/filebox/inbox/" in about_content


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


def test_pma_generated_files_refreshed_without_force(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)

    docs_dir = tmp_path / ".codex-autorunner" / "pma" / "docs"
    prompt_path = docs_dir / "prompt.md"
    about_path = docs_dir / "ABOUT_CAR.md"

    prompt_path.write_text("custom prompt", encoding="utf-8")
    about_path.write_text("custom about", encoding="utf-8")

    seed_hub_files(tmp_path, force=False)

    refreshed_prompt = prompt_path.read_text(encoding="utf-8")
    refreshed_about = about_path.read_text(encoding="utf-8")

    assert refreshed_prompt != "custom prompt"
    assert refreshed_about != "custom about"
    assert "CAR:PMA_DOCS_GENERATED" in refreshed_prompt
    assert "CAR:PMA_DOCS_GENERATED" in refreshed_about


def test_pma_user_docs_not_overridden_without_force(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)

    docs_dir = tmp_path / ".codex-autorunner" / "pma" / "docs"
    agents_path = docs_dir / "AGENTS.md"
    active_path = docs_dir / "active_context.md"
    log_path = docs_dir / "context_log.md"

    agents_path.write_text("custom agents", encoding="utf-8")
    active_path.write_text("custom active", encoding="utf-8")
    log_path.write_text("custom log", encoding="utf-8")

    seed_hub_files(tmp_path, force=False)

    assert agents_path.read_text(encoding="utf-8") == "custom agents"
    assert active_path.read_text(encoding="utf-8") == "custom active"
    assert log_path.read_text(encoding="utf-8") == "custom log"


def test_pma_legacy_docs_migrated_to_docs_dir(tmp_path: Path) -> None:
    pma_dir = tmp_path / ".codex-autorunner" / "pma"
    pma_dir.mkdir(parents=True, exist_ok=True)
    legacy_agents = pma_dir / "AGENTS.md"
    legacy_agents.write_text("legacy agents", encoding="utf-8")

    seed_hub_files(tmp_path, force=False)

    docs_agents = pma_dir / "docs" / "AGENTS.md"
    assert docs_agents.exists()
    assert docs_agents.read_text(encoding="utf-8") == "legacy agents"
    assert not legacy_agents.exists()


def test_pma_legacy_user_docs_preserved_on_force_init(tmp_path: Path) -> None:
    pma_dir = tmp_path / ".codex-autorunner" / "pma"
    pma_dir.mkdir(parents=True, exist_ok=True)
    legacy_agents = pma_dir / "AGENTS.md"
    legacy_agents.write_text("legacy agents force", encoding="utf-8")

    seed_hub_files(tmp_path, force=True)

    docs_agents = pma_dir / "docs" / "AGENTS.md"
    assert docs_agents.exists()
    assert docs_agents.read_text(encoding="utf-8") == "legacy agents force"
    assert not legacy_agents.exists()
