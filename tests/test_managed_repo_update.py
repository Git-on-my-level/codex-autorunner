from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.managed_repo_update import refresh_hub_managed_repos
from codex_autorunner.core.ticket_linter_cli import _SCRIPT as LINTER_SCRIPT
from codex_autorunner.core.ticket_linter_cli import LINTER_REL_PATH
from codex_autorunner.manifest import Manifest, save_manifest


def test_refresh_hub_managed_repos_reseeds_repo_helpers(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    repo_root = hub_root / "repos" / "alpha"
    repo_root.mkdir(parents=True)
    seed_repo_files(repo_root, force=True, git_required=False)
    (repo_root / LINTER_REL_PATH).write_text("# stale\n", encoding="utf-8")

    manifest = Manifest(version=3, repos=[])
    manifest.ensure_repo(hub_root, repo_root, repo_id="alpha")
    save_manifest(hub_root / ".codex-autorunner" / "manifest.yml", manifest, hub_root)

    summary = refresh_hub_managed_repos(hub_root)

    assert summary.refreshed_repo_ids == ["alpha"]
    assert summary.missing_repo_ids == []
    assert (repo_root / LINTER_REL_PATH).read_text(encoding="utf-8") == LINTER_SCRIPT


def test_refresh_hub_managed_repos_reports_missing_repo(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    missing_repo = hub_root / "repos" / "missing"
    manifest = Manifest(version=3, repos=[])
    manifest.ensure_repo(hub_root, missing_repo, repo_id="missing")
    save_manifest(hub_root / ".codex-autorunner" / "manifest.yml", manifest, hub_root)

    summary = refresh_hub_managed_repos(hub_root)

    assert summary.refreshed_repo_ids == []
    assert summary.missing_repo_ids == ["missing"]


def test_update_hub_managed_repos_script_refreshes_repo_helpers(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    repo_root = hub_root / "repos" / "beta"
    repo_root.mkdir(parents=True)
    seed_repo_files(repo_root, force=True, git_required=False)
    (repo_root / LINTER_REL_PATH).write_text("# stale\n", encoding="utf-8")

    manifest = Manifest(version=3, repos=[])
    manifest.ensure_repo(hub_root, repo_root, repo_id="beta")
    save_manifest(hub_root / ".codex-autorunner" / "manifest.yml", manifest, hub_root)

    script_path = Path("scripts/update-hub-managed-repos.sh").resolve()
    src_path = str(Path("src").resolve())
    subprocess.run(
        ["bash", str(script_path)],
        check=True,
        cwd=Path.cwd(),
        env={
            **os.environ,
            "HELPER_PYTHON": os.environ.get("PYTHON", sys.executable),
            "HUB_ROOT": str(hub_root),
            "PYTHONPATH": (
                f"{src_path}{os.pathsep}{os.environ['PYTHONPATH']}"
                if os.environ.get("PYTHONPATH")
                else src_path
            ),
        },
    )

    assert (repo_root / LINTER_REL_PATH).read_text(encoding="utf-8") == LINTER_SCRIPT
