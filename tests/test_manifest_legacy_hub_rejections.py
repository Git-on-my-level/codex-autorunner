from pathlib import Path

from codex_autorunner.manifest import load_manifest_with_issues


def test_manifest_strips_removed_workspace_section(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    ws_section = "agent_" + "workspaces"
    manifest_path.write_text(
        f"version: 3\nrepos: []\n{ws_section}: []\n",
        encoding="utf-8",
    )
    manifest, _issues = load_manifest_with_issues(manifest_path, hub_root)
    assert manifest.version == 3
    text = manifest_path.read_text(encoding="utf-8")
    assert ws_section not in text
    assert "version: 3" in text


def test_manifest_strips_removed_agents_block(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_agent = "".join(("zero", "claw"))
    manifest_path.write_text(
        f"version: 3\nrepos: []\nagents:\n  {legacy_agent}: {{}}\n",
        encoding="utf-8",
    )
    manifest, _issues = load_manifest_with_issues(manifest_path, hub_root)
    assert manifest.version == 3
    text = manifest_path.read_text(encoding="utf-8")
    assert legacy_agent not in text
    assert "agents:" not in text
