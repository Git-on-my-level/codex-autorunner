from pathlib import Path

import pytest

from codex_autorunner.manifest import ManifestError, load_manifest_with_issues


def test_manifest_rejects_removed_workspace_section(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    ws_section = "agent_" + "workspaces"
    manifest_path.write_text(
        f"version: 3\nrepos: []\n{ws_section}: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError) as excinfo:
        load_manifest_with_issues(manifest_path, hub_root)
    assert ws_section in str(excinfo.value)


def test_manifest_rejects_removed_agents_block(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_agent = "".join(("zero", "claw"))
    manifest_path.write_text(
        f"version: 3\nrepos: []\nagents:\n  {legacy_agent}: {{}}\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError) as excinfo:
        load_manifest_with_issues(manifest_path, hub_root)
    assert legacy_agent in str(excinfo.value)
