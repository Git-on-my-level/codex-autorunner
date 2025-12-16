from pathlib import Path
from typing import Optional

import yaml

from .about_car import ensure_about_car_file_for_repo
from .config import CONFIG_FILENAME, DEFAULT_REPO_CONFIG, DEFAULT_HUB_CONFIG
from .manifest import load_manifest
from .utils import atomic_write

GITIGNORE_CONTENT = "*"


def sample_todo() -> str:
    return """# TODO\n\n- [ ] Replace this item with your first task\n- [ ] Add another task\n- [x] Example completed item\n"""


def sample_opinions() -> str:
    return """# Opinions\n\n- Prefer small, well-tested changes.\n- Keep docs in sync with code.\n- Avoid unnecessary dependencies.\n"""


def sample_spec() -> str:
    return """# Spec\n\n## Context\n- Add project background and goals here.\n\n## Requirements\n- Requirement 1\n- Requirement 2\n\n## Non-goals\n- Out of scope items\n"""


def _seed_doc(path: Path, force: bool, content: str) -> None:
    if path.exists() and not force:
        return
    path.write_text(content, encoding="utf-8")


def write_repo_config(repo_root: Path, force: bool = False) -> Path:
    config_path = repo_root / CONFIG_FILENAME
    if config_path.exists() and not force:
        return config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(DEFAULT_REPO_CONFIG, f, sort_keys=False)
    return config_path


def write_hub_config(hub_root: Path, force: bool = False) -> Path:
    config_path = hub_root / CONFIG_FILENAME
    if config_path.exists() and not force:
        return config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(DEFAULT_HUB_CONFIG, f, sort_keys=False)
    return config_path


def seed_repo_files(
    repo_root: Path, force: bool = False, git_required: bool = True
) -> None:
    """
    Initialize a repository's .codex-autorunner directory with defaults.
    This is used by the CLI init path and hub auto-init discovery.
    """
    if git_required and not (repo_root / ".git").exists():
        raise ValueError("Missing .git directory; pass git_required=False to bypass")

    ca_dir = repo_root / ".codex-autorunner"
    ca_dir.mkdir(parents=True, exist_ok=True)

    gitignore_path = ca_dir / ".gitignore"
    if not gitignore_path.exists() or force:
        gitignore_path.write_text(GITIGNORE_CONTENT, encoding="utf-8")

    write_repo_config(repo_root, force=force)

    state_path = ca_dir / "state.json"
    if not state_path.exists() or force:
        atomic_write(
            state_path,
            '{\n  "last_run_id": null,\n  "status": "idle",\n  "last_exit_code": null,\n  "last_run_started_at": null,\n  "last_run_finished_at": null,\n  "runner_pid": null\n}\n',
        )

    log_path = ca_dir / "codex-autorunner.log"
    if not log_path.exists() or force:
        log_path.write_text("", encoding="utf-8")

    _seed_doc(ca_dir / "TODO.md", force, sample_todo())
    _seed_doc(ca_dir / "PROGRESS.md", force, "# Progress\n\n")
    _seed_doc(ca_dir / "OPINIONS.md", force, sample_opinions())
    _seed_doc(ca_dir / "SPEC.md", force, sample_spec())

    # Seed an always-available briefing doc for interactive Codex sessions.
    ensure_about_car_file_for_repo(
        repo_root,
        doc_paths={
            "todo": ca_dir / "TODO.md",
            "progress": ca_dir / "PROGRESS.md",
            "opinions": ca_dir / "OPINIONS.md",
            "spec": ca_dir / "SPEC.md",
        },
        force=force,
    )


def seed_hub_files(hub_root: Path, force: bool = False) -> None:
    """
    Initialize a hub workspace with defaults and a manifest.
    """
    ca_dir = hub_root / ".codex-autorunner"
    ca_dir.mkdir(parents=True, exist_ok=True)

    gitignore_path = ca_dir / ".gitignore"
    if not gitignore_path.exists() or force:
        gitignore_path.write_text(GITIGNORE_CONTENT, encoding="utf-8")

    write_hub_config(hub_root, force=force)

    manifest_path = hub_root / DEFAULT_HUB_CONFIG["hub"]["manifest"]
    load_manifest(manifest_path, hub_root)

    hub_state_path = ca_dir / "hub_state.json"
    if not hub_state_path.exists() or force:
        atomic_write(
            hub_state_path,
            '{\n  "last_scan_at": null,\n  "repos": []\n}\n',
        )
