from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.apps import (
    apply_app_entrypoint,
    get_installed_app,
    install_app,
    list_installed_app_tools,
    load_app_manifest,
    run_installed_app_tool,
)
from codex_autorunner.core.apps.hooks import (
    AppHookPoint,
    execute_matching_installed_app_hooks,
    list_installed_app_hooks,
    matches_installed_app_hook,
)
from codex_autorunner.core.config import CONFIG_FILENAME, load_hub_config
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.state_roots import resolve_repo_state_root
from codex_autorunner.tickets.files import read_ticket_frontmatter

FIXTURE_APP_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "fixtures"
    / "apps"
    / "echo-workflow"
)


def _init_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], repo_path, check=True)
    run_git(["config", "user.email", "test@example.com"], repo_path, check=True)
    run_git(["config", "user.name", "Test User"], repo_path, check=True)
    run_git(["checkout", "-b", "main"], repo_path, check=True)


def _commit_repo(repo_path: Path, message: str) -> str:
    run_git(["add", "."], repo_path, check=True)
    run_git(["commit", "-m", message], repo_path, check=True)
    return (run_git(["rev-parse", "HEAD"], repo_path, check=True).stdout or "").strip()


def _configure_apps_repo(hub_root: Path, app_repo: Path) -> None:
    config_path = hub_root / CONFIG_FILENAME
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw["apps"] = {
        "enabled": True,
        "repos": [
            {
                "id": "local",
                "url": str(app_repo),
                "trusted": True,
                "default_ref": "main",
            }
        ],
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _copy_fixture_to_app_repo(app_repo: Path) -> None:
    app_root = app_repo / "apps" / "echo-workflow"
    shutil.copytree(FIXTURE_APP_DIR, app_root, dirs_exist_ok=True)


def _setup_fixture_env(tmp_path: Path) -> tuple[Path, Path]:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    app_repo = tmp_path / "app_repo"
    _init_repo(app_repo)
    _configure_apps_repo(hub_root, app_repo)
    _copy_fixture_to_app_repo(app_repo)
    _commit_repo(app_repo, "add echo-workflow fixture app")

    hub_config = load_hub_config(hub_root)
    install_result = install_app(
        hub_config, hub_root, repo_root, "local:apps/echo-workflow"
    )
    assert install_result.changed is True
    assert install_result.app.app_id == "fixture.echo-workflow"
    return repo_root, install_result.app.lock.commit_sha


class TestFixtureManifestParsing:
    def test_fixture_manifest_loads(self) -> None:
        manifest = load_app_manifest(FIXTURE_APP_DIR / "car-app.yaml")

        assert manifest.id == "fixture.echo-workflow"
        assert manifest.name == "Echo Workflow"
        assert manifest.version == "0.1.0"
        assert manifest.entrypoint is not None
        assert manifest.entrypoint.path == "templates/entry.md"

    def test_fixture_manifest_declares_two_tools(self) -> None:
        manifest = load_app_manifest(FIXTURE_APP_DIR / "car-app.yaml")

        assert len(manifest.tools) == 2
        assert "record-state" in manifest.tools
        assert "render-summary" in manifest.tools
        assert manifest.tools["render-summary"].outputs[0].kind == "markdown"
        assert (
            manifest.tools["render-summary"].outputs[0].path == "artifacts/summary.md"
        )

    def test_fixture_manifest_declares_hook(self) -> None:
        manifest = load_app_manifest(FIXTURE_APP_DIR / "car-app.yaml")

        assert len(manifest.hooks) == 1
        assert manifest.hooks[0].point == "after_ticket_done"
        assert manifest.hooks[0].entries[0].tool == "record-state"
        assert manifest.hooks[0].entries[0].failure == "warn"

    def test_fixture_manifest_declares_inputs(self) -> None:
        manifest = load_app_manifest(FIXTURE_APP_DIR / "car-app.yaml")

        assert "message" in manifest.inputs
        assert manifest.inputs["message"].required is True
        assert "repeat" in manifest.inputs
        assert manifest.inputs["repeat"].required is False

    def test_fixture_manifest_declares_permissions(self) -> None:
        manifest = load_app_manifest(FIXTURE_APP_DIR / "car-app.yaml")

        assert manifest.permissions.network is False
        assert "state/**" in manifest.permissions.writes
        assert "artifacts/**" in manifest.permissions.writes

    def test_fixture_entrypoint_template_exists(self) -> None:
        template_path = FIXTURE_APP_DIR / "templates" / "entry.md"
        assert template_path.exists()
        content = template_path.read_text(encoding="utf-8")
        assert content.startswith("---")

    def test_fixture_scripts_exist(self) -> None:
        assert (FIXTURE_APP_DIR / "scripts" / "record_state.py").exists()
        assert (FIXTURE_APP_DIR / "scripts" / "render_summary.py").exists()


class TestFixtureInstallAndRun:
    def test_install_creates_repo_local_layout(self, tmp_path: Path) -> None:
        repo_root, _commit_sha = _setup_fixture_env(tmp_path)
        installed = get_installed_app(repo_root, "fixture.echo-workflow")

        assert installed is not None
        assert installed.bundle_verified is True
        assert (installed.paths.bundle_root / "car-app.yaml").exists()
        assert (installed.paths.bundle_root / "scripts" / "record_state.py").exists()
        assert (installed.paths.bundle_root / "scripts" / "render_summary.py").exists()
        assert (installed.paths.bundle_root / "templates" / "entry.md").exists()

    def test_list_tools_returns_fixture_tools(self, tmp_path: Path) -> None:
        repo_root, _ = _setup_fixture_env(tmp_path)

        tools = list_installed_app_tools(repo_root, "fixture.echo-workflow")

        tool_ids = {t.tool_id for t in tools}
        assert tool_ids == {"record-state", "render-summary"}

    def test_record_state_tool_writes_jsonl(self, tmp_path: Path) -> None:
        repo_root, _ = _setup_fixture_env(tmp_path)

        result = run_installed_app_tool(
            repo_root,
            "fixture.echo-workflow",
            "record-state",
            extra_argv=["hello world"],
            flow_run_id="run-fixture",
            ticket_id="tkt-echo",
        )

        assert result.exit_code == 0
        state_root = (
            resolve_repo_state_root(repo_root)
            / "apps"
            / "fixture.echo-workflow"
            / "state"
        )
        records_path = state_root / "records.jsonl"
        assert records_path.exists()
        records = [
            json.loads(line)
            for line in records_path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(records) == 1
        assert records[0]["message"] == "hello world"
        assert records[0]["app_id"] == "fixture.echo-workflow"
        assert records[0]["flow_run_id"] == "run-fixture"
        assert records[0]["ticket_id"] == "tkt-echo"

    def test_render_summary_produces_artifact(self, tmp_path: Path) -> None:
        repo_root, _ = _setup_fixture_env(tmp_path)

        run_installed_app_tool(
            repo_root,
            "fixture.echo-workflow",
            "record-state",
            extra_argv=["alpha"],
        )
        run_installed_app_tool(
            repo_root,
            "fixture.echo-workflow",
            "record-state",
            extra_argv=["beta"],
        )

        result = run_installed_app_tool(
            repo_root,
            "fixture.echo-workflow",
            "render-summary",
        )

        assert result.exit_code == 0
        assert len(result.outputs) == 1
        assert result.outputs[0].kind == "markdown"
        assert result.outputs[0].relative_path == "artifacts/summary.md"
        assert result.outputs[0].absolute_path.exists()

        summary = result.outputs[0].absolute_path.read_text(encoding="utf-8")
        assert "alpha" in summary
        assert "beta" in summary
        assert "Total records: 2" in summary


class TestFixtureApply:
    def test_apply_creates_ticket_from_entrypoint(self, tmp_path: Path) -> None:
        repo_root, _ = _setup_fixture_env(tmp_path)

        result = apply_app_entrypoint(
            repo_root,
            "fixture.echo-workflow",
            app_inputs={"message": "fixture test"},
        )

        assert result.ticket_path.exists()
        assert result.ticket_index >= 1
        assert result.app.app_id == "fixture.echo-workflow"

        frontmatter, body = _parse_ticket(result.ticket_path)
        assert frontmatter["app"] == "fixture.echo-workflow"
        assert frontmatter["agent"] == "codex"
        assert "## App Inputs" in body
        assert "`message`" in body

    def test_apply_injects_provenance(self, tmp_path: Path) -> None:
        repo_root, _ = _setup_fixture_env(tmp_path)

        result = apply_app_entrypoint(
            repo_root,
            "fixture.echo-workflow",
            app_inputs={"message": "prov test"},
        )

        frontmatter, _body = _parse_ticket(result.ticket_path)
        assert "app_commit" in frontmatter
        assert "app_manifest_sha" in frontmatter
        assert "app_bundle_sha" in frontmatter
        assert frontmatter["app_source"] == "local:apps/echo-workflow@main"


class TestFixtureHooks:
    def test_after_ticket_done_hook_fires(self, tmp_path: Path) -> None:
        repo_root, _ = _setup_fixture_env(tmp_path)

        hooks = list_installed_app_hooks(repo_root, AppHookPoint.AFTER_TICKET_DONE)
        assert len(hooks) == 1
        assert hooks[0].tool_id == "record-state"
        assert hooks[0].app_id == "fixture.echo-workflow"

        _ticket_path, frontmatter = _make_ticket_frontmatter(
            tmp_path,
            """---
ticket_id: "tkt_echo_hook"
agent: codex
done: true
app: "fixture.echo-workflow"
---
body
""",
        )

        assert matches_installed_app_hook(hooks[0], ticket_frontmatter=frontmatter)

        result = execute_matching_installed_app_hooks(
            repo_root,
            "after_ticket_done",
            flow_run_id="run-hook-test",
            ticket_id="tkt_echo_hook",
            ticket_path=_ticket_path,
            ticket_frontmatter=frontmatter,
        )

        assert result.paused is False
        assert result.failed is False
        assert len(result.executions) == 1
        assert result.executions[0].exit_code == 0

        state_root = (
            resolve_repo_state_root(repo_root)
            / "apps"
            / "fixture.echo-workflow"
            / "state"
        )
        records_path = state_root / "records.jsonl"
        assert records_path.exists()
        records = [
            json.loads(line)
            for line in records_path.read_text(encoding="utf-8").splitlines()
        ]
        assert any(r["hook_point"] == "after_ticket_done" for r in records)


def _parse_ticket(ticket_path: Path):
    from codex_autorunner.tickets.frontmatter import parse_markdown_frontmatter

    return parse_markdown_frontmatter(ticket_path.read_text(encoding="utf-8"))


def _make_ticket_frontmatter(tmp_path: Path, raw: str):
    ticket_path = tmp_path / "ticket.md"
    ticket_path.write_text(raw, encoding="utf-8")
    frontmatter, errors = read_ticket_frontmatter(ticket_path)
    assert not errors
    assert frontmatter is not None
    return ticket_path, frontmatter
