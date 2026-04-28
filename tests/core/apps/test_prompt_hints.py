from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.apps.prompt_hints import (
    MAX_APPS,
    MAX_HINT_BYTES,
    MAX_TOOLS_PER_APP,
    build_installed_apps_prompt_hint,
)


def _write_installed_app(
    repo_root: Path,
    app_id: str,
    version: str = "0.1.0",
    tools: dict[str, dict] | None = None,
) -> None:
    apps_root = repo_root / ".codex-autorunner" / "apps"
    app_root = apps_root / app_id
    bundle_root = app_root / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)

    tools_yaml = ""
    if tools:
        lines = []
        for tid, info in tools.items():
            lines.append(
                f"  {tid}:\n"
                f"    description: {info.get('description', '')}\n"
                f'    argv: {info.get("argv", ["python3", "scripts/run.py"])}'
            )
        tools_yaml = "\ntools:\n" + "\n".join(lines)

    (bundle_root / "car-app.yaml").write_text(
        f"schema_version: 1\n"
        f"id: {app_id}\n"
        f"name: {app_id}\n"
        f"version: {version}\n"
        f"description: test app{tools_yaml}\n",
        encoding="utf-8",
    )
    (app_root / "state").mkdir(exist_ok=True)
    (app_root / "artifacts").mkdir(exist_ok=True)

    lock = {
        "id": app_id,
        "version": version,
        "source_repo_id": "test",
        "source_url": "https://example.com/repo",
        "source_path": f"apps/{app_id}",
        "source_ref": "main",
        "commit_sha": "a" * 40,
        "manifest_sha": "b" * 64,
        "bundle_sha": "c" * 64,
        "trusted": True,
        "installed_at": "2026-01-01T00:00:00Z",
    }
    (app_root / "app.lock.json").write_text(
        json.dumps(lock, indent=2) + "\n", encoding="utf-8"
    )


def test_no_apps_installed_returns_empty(tmp_path: Path) -> None:
    hint = build_installed_apps_prompt_hint(tmp_path)
    assert hint == ""


def test_no_apps_dir_returns_empty(tmp_path: Path) -> None:
    (tmp_path / ".codex-autorunner").mkdir()
    hint = build_installed_apps_prompt_hint(tmp_path)
    assert hint == ""


def test_one_app_with_tools(tmp_path: Path) -> None:
    _write_installed_app(
        tmp_path,
        "blessed.autoresearch",
        version="0.1.0",
        tools={
            "record-iteration": {"description": "Record iteration"},
            "render-card": {"description": "Render card"},
        },
    )

    hint = build_installed_apps_prompt_hint(tmp_path)

    assert "Installed CAR apps:" in hint
    assert "blessed.autoresearch v0.1.0" in hint
    assert "car apps run blessed.autoresearch record-iteration -- ..." in hint
    assert "car apps run blessed.autoresearch render-card -- ..." in hint
    assert ".codex-autorunner/apps/blessed.autoresearch/state/" in hint
    assert ".codex-autorunner/apps/blessed.autoresearch/artifacts/" in hint
    assert "<CAR_" not in hint


def test_app_with_no_tools(tmp_path: Path) -> None:
    _write_installed_app(tmp_path, "simple.app", version="1.0.0")

    hint = build_installed_apps_prompt_hint(tmp_path)

    assert "simple.app v1.0.0" in hint
    assert "Tools:" not in hint
    assert ".codex-autorunner/apps/simple.app/state/" in hint


def test_many_tools_truncation(tmp_path: Path) -> None:
    tools = {f"tool-{i:03d}": {"description": f"Tool {i}"} for i in range(20)}
    _write_installed_app(tmp_path, "many.tools", version="2.0.0", tools=tools)

    hint = build_installed_apps_prompt_hint(tmp_path)

    shown_count = hint.count("car apps run many.tools")
    assert shown_count == MAX_TOOLS_PER_APP
    assert "15 more tools" in hint


def test_many_apps_truncation(tmp_path: Path) -> None:
    for i in range(MAX_APPS + 5):
        _write_installed_app(
            tmp_path,
            f"app.num-{i:03d}",
            version="1.0.0",
            tools={"run": {"description": "Run"}},
        )

    hint = build_installed_apps_prompt_hint(tmp_path)

    app_count = hint.count("v1.0.0")
    assert app_count == MAX_APPS


def test_hint_size_is_bounded(tmp_path: Path) -> None:
    for i in range(MAX_APPS + 5):
        long_tools = {
            f"tool-with-very-long-name-{j:04d}": {
                "description": f"A very long description for tool number {j}"
            }
            for j in range(MAX_TOOLS_PER_APP + 5)
        }
        _write_installed_app(
            tmp_path,
            f"app.with-long-names-{i:04d}",
            version="99.99.99",
            tools=long_tools,
        )

    hint = build_installed_apps_prompt_hint(tmp_path)

    assert len(hint.encode("utf-8")) <= MAX_HINT_BYTES


def test_stale_app_lock_graceful_handling(tmp_path: Path) -> None:
    apps_root = tmp_path / ".codex-autorunner" / "apps"
    stale_root = apps_root / "stale.broken"
    stale_root.mkdir(parents=True)
    (stale_root / "app.lock.json").write_text("not valid json{{{", encoding="utf-8")
    (stale_root / "bundle").mkdir()
    _write_installed_app(
        tmp_path, "good.app", version="1.0.0", tools={"run": {"description": "Run"}}
    )

    hint = build_installed_apps_prompt_hint(tmp_path)

    assert "good.app v1.0.0" in hint
    assert "stale.broken" not in hint


def test_missing_manifest_graceful_handling(tmp_path: Path) -> None:
    apps_root = tmp_path / ".codex-autorunner" / "apps"
    missing_root = apps_root / "missing.manifest"
    missing_root.mkdir(parents=True)
    lock = {
        "id": "missing.manifest",
        "version": "0.1.0",
        "source_repo_id": "test",
        "source_url": "https://example.com",
        "source_path": "apps/x",
        "source_ref": "main",
        "commit_sha": "a" * 40,
        "manifest_sha": "b" * 64,
        "bundle_sha": "c" * 64,
        "trusted": True,
        "installed_at": "2026-01-01T00:00:00Z",
    }
    (missing_root / "app.lock.json").write_text(
        json.dumps(lock) + "\n", encoding="utf-8"
    )
    (missing_root / "bundle").mkdir()
    _write_installed_app(tmp_path, "ok.app", version="1.0.0")

    hint = build_installed_apps_prompt_hint(tmp_path)

    assert "ok.app v1.0.0" in hint
    assert "missing.manifest" not in hint


def test_no_app_state_or_script_contents_leaked(tmp_path: Path) -> None:
    apps_root = tmp_path / ".codex-autorunner" / "apps"
    _write_installed_app(
        tmp_path,
        "secret.app",
        version="1.0.0",
        tools={"run": {"description": "Run things"}},
    )
    state_file = apps_root / "secret.app" / "state" / "data.json"
    state_file.write_text('{"secret": "leaked-key-12345"}', encoding="utf-8")
    script_file = apps_root / "secret.app" / "bundle" / "scripts" / "run.py"
    script_file.parent.mkdir(parents=True, exist_ok=True)
    script_file.write_text(
        "#!/usr/bin/env python3\nsecret_code_here()", encoding="utf-8"
    )

    hint = build_installed_apps_prompt_hint(tmp_path)

    assert "leaked-key-12345" not in hint
    assert "secret_code_here" not in hint
    assert "secret.app v1.0.0" in hint
