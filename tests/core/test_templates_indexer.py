from pathlib import Path

import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import CONFIG_FILENAME, load_hub_config
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.templates import get_template_by_ref


def test_get_template_by_ref_defaults_to_repo_default_ref(
    monkeypatch, tmp_path: Path
) -> None:
    """Verify no-ref template references use repository default_ref."""
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)

    repo = tmp_path / "template_repo"
    repo.mkdir()
    run_git(["init"], repo, check=True)
    run_git(["config", "user.email", "test@example.com"], repo, check=True)
    run_git(["config", "user.name", "Test User"], repo, check=True)
    run_git(["checkout", "-b", "main"], repo, check=True)
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    run_git(["add", "seed.txt"], repo, check=True)
    run_git(["commit", "-m", "seed"], repo, check=True)
    run_git(["checkout", "-b", "templates"], repo, check=True)
    (repo / "template.md").write_text("# Template\n", encoding="utf-8")
    run_git(["add", "template.md"], repo, check=True)
    run_git(["commit", "-m", "add template"], repo, check=True)
    run_git(["checkout", "main"], repo, check=True)

    config_path = hub_root / CONFIG_FILENAME
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    templates = raw.get("templates")
    if not isinstance(templates, dict):
        templates = {}
    templates["enabled"] = True
    templates["repos"] = [
        {
            "id": "local",
            "url": str(repo),
            "trusted": True,
            "default_ref": "templates",
        }
    ]
    raw["templates"] = templates
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    hub_config = load_hub_config(hub_root)

    # Avoid cloning by using the template source repo directly.
    monkeypatch.setattr(
        "codex_autorunner.core.templates.indexer.ensure_git_mirror",
        lambda _repo, _hub_root: repo,
    )

    template = get_template_by_ref(hub_config, hub_root, "local:template.md")
    assert template is not None
    assert template.repo_id == "local"
    assert template.path == "template.md"
    assert template.ref == "templates"
