import json
import shlex
from pathlib import Path
from typing import Optional

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app
from codex_autorunner.core.force_attestation import (
    FORCE_ATTESTATION_REQUIRED_ERROR,
    FORCE_ATTESTATION_REQUIRED_PHRASE,
)
from codex_autorunner.core.hub import (
    HubSupervisor,
    LockStatus,
    RepoSnapshot,
    RepoStatus,
)


def _snapshot(
    base_path: Path,
    repo_id: str,
    *,
    kind: str,
    worktree_of: Optional[str] = None,
    branch: Optional[str] = None,
) -> RepoSnapshot:
    return RepoSnapshot(
        id=repo_id,
        path=base_path / repo_id,
        display_name=repo_id,
        enabled=True,
        auto_run=False,
        worktree_setup_commands=None,
        kind=kind,
        worktree_of=worktree_of,
        branch=branch,
        exists_on_disk=True,
        is_clean=True,
        initialized=True,
        init_error=None,
        status=RepoStatus.IDLE,
        lock_status=LockStatus.UNLOCKED,
        last_run_id=None,
        last_run_started_at=None,
        last_run_finished_at=None,
        last_exit_code=None,
        runner_pid=None,
    )


def test_cli_hub_worktree_list_filters_worktrees(tmp_path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    base = _snapshot(tmp_path, "base", kind="base")
    worktree = _snapshot(
        tmp_path, "base--feature", kind="worktree", worktree_of="base", branch="feature"
    )

    def _fake_list(self, *, use_cache: bool = True):
        return [base, worktree]

    monkeypatch.setattr(HubSupervisor, "list_repos", _fake_list)

    runner = CliRunner()
    result = runner.invoke(app, ["hub", "worktree", "list", "--path", str(hub_root)])
    assert result.exit_code == 0

    lines = [line for line in result.output.splitlines() if "base--feature" in line]
    assert len(lines) >= 1
    assert "cmd=car hub worktree archive base--feature" in result.output


def test_cli_hub_worktree_scan_filters_worktrees_json(tmp_path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    base = _snapshot(tmp_path, "base", kind="base")
    worktree = _snapshot(
        tmp_path, "base--feature", kind="worktree", worktree_of="base", branch="feature"
    )

    def _fake_scan(self):
        return [base, worktree]

    monkeypatch.setattr(HubSupervisor, "scan", _fake_scan)

    runner = CliRunner()
    result = runner.invoke(
        app, ["hub", "worktree", "scan", "--path", str(hub_root), "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [item["id"] for item in payload["worktrees"]] == ["base--feature"]
    assert (
        payload["worktrees"][0]["recommended_command"]
        == f"car hub worktree archive base--feature --path {shlex.quote(str(hub_root))}"
    )
    assert payload["worktrees"][0]["recommended_actions"] == [
        f"car hub worktree archive base--feature --path {shlex.quote(str(hub_root))}",
        f"car hub worktree cleanup base--feature --path {shlex.quote(str(hub_root))}",
        f"car hub destination show base--feature --path {shlex.quote(str(hub_root))}",
    ]


def test_cli_hub_worktree_create_prints_details(tmp_path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    worktree = _snapshot(
        tmp_path, "base--feature", kind="worktree", worktree_of="base", branch="feature"
    )

    def _fake_create(self, *, base_repo_id, branch, force=False, start_point=None):
        return worktree

    monkeypatch.setattr(HubSupervisor, "create_worktree", _fake_create)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "worktree",
            "create",
            "base",
            "feature",
            "--path",
            str(hub_root),
        ],
    )
    assert result.exit_code == 0
    assert "base--feature" in result.output
    assert "feature" in result.output
    assert str(worktree.path) in result.output


def test_cli_hub_scan_includes_recommended_commands(tmp_path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    base = _snapshot(tmp_path, "base", kind="base")
    worktree = _snapshot(
        tmp_path, "base--feature", kind="worktree", worktree_of="base", branch="feature"
    )

    def _fake_scan(self):
        return [base, worktree]

    monkeypatch.setattr(HubSupervisor, "scan", _fake_scan)

    runner = CliRunner()
    result = runner.invoke(app, ["hub", "scan", "--path", str(hub_root)])
    assert result.exit_code == 0
    assert "base" in result.output
    assert "base--feature" in result.output
    assert "car hub worktree archive base--feature" in result.output


def test_cli_hub_worktree_cleanup_calls_supervisor(tmp_path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    calls = {}

    def _fake_cleanup(
        self,
        *,
        worktree_repo_id,
        delete_branch=False,
        delete_remote=False,
        archive=True,
        force_archive=False,
        archive_note=None,
        force=False,
        archive_profile=None,
    ):
        calls["worktree_repo_id"] = worktree_repo_id
        calls["delete_branch"] = delete_branch
        calls["delete_remote"] = delete_remote
        calls["archive"] = archive
        calls["force_archive"] = force_archive
        calls["archive_note"] = archive_note
        calls["force"] = force
        calls["archive_profile"] = archive_profile

    monkeypatch.setattr(HubSupervisor, "cleanup_worktree", _fake_cleanup)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "worktree",
            "cleanup",
            "wt-1",
            "--path",
            str(hub_root),
            "--no-archive",
        ],
    )
    assert result.exit_code == 0
    assert calls["worktree_repo_id"] == "wt-1"
    assert calls["archive"] is False
    assert calls["force"] is False
    assert calls["archive_profile"] is None


def test_cli_hub_worktree_cleanup_archives_by_default(tmp_path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    calls = {}

    def _fake_cleanup(
        self,
        *,
        worktree_repo_id,
        delete_branch=False,
        delete_remote=False,
        archive=True,
        force_archive=False,
        archive_note=None,
        force=False,
        archive_profile=None,
    ):
        calls["worktree_repo_id"] = worktree_repo_id
        calls["archive"] = archive
        calls["force"] = force
        calls["archive_profile"] = archive_profile

    monkeypatch.setattr(HubSupervisor, "cleanup_worktree", _fake_cleanup)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "worktree",
            "cleanup",
            "wt-1",
            "--path",
            str(hub_root),
        ],
    )
    assert result.exit_code == 0
    assert calls["worktree_repo_id"] == "wt-1"
    assert calls["archive"] is True
    assert calls["force"] is False
    assert calls["archive_profile"] is None


def test_cli_hub_worktree_cleanup_forwards_force_flag(tmp_path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    calls = {}

    def _fake_cleanup(
        self,
        *,
        worktree_repo_id,
        delete_branch=False,
        delete_remote=False,
        archive=True,
        force_archive=False,
        archive_note=None,
        force=False,
        force_attestation=None,
        archive_profile=None,
    ):
        calls["worktree_repo_id"] = worktree_repo_id
        calls["force"] = force
        calls["force_attestation"] = force_attestation
        calls["archive_profile"] = archive_profile

    monkeypatch.setattr(HubSupervisor, "cleanup_worktree", _fake_cleanup)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "worktree",
            "cleanup",
            "wt-1",
            "--path",
            str(hub_root),
            "--force",
            "--force-attestation",
            "cleanup active worktree",
        ],
    )
    assert result.exit_code == 0
    assert calls["worktree_repo_id"] == "wt-1"
    assert calls["force"] is True
    assert calls["force_attestation"] == {
        "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
        "user_request": "cleanup active worktree",
        "target_scope": "hub.worktree.cleanup:wt-1",
    }
    assert calls["archive_profile"] is None


def test_cli_hub_worktree_cleanup_force_requires_attestation(tmp_path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "worktree",
            "cleanup",
            "wt-1",
            "--path",
            str(hub_root),
            "--force",
        ],
    )
    assert result.exit_code == 1
    assert FORCE_ATTESTATION_REQUIRED_ERROR in result.output


def test_cli_hub_worktree_cleanup_prints_docker_cleanup_status(
    tmp_path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    def _fake_cleanup(
        self,
        *,
        worktree_repo_id,
        delete_branch=False,
        delete_remote=False,
        archive=True,
        force_archive=False,
        archive_note=None,
        force=False,
        archive_profile=None,
    ):
        _ = (
            self,
            worktree_repo_id,
            delete_branch,
            delete_remote,
            archive,
            force_archive,
            archive_note,
            force,
            archive_profile,
        )
        return {
            "status": "ok",
            "docker_cleanup": {
                "status": "removed",
                "container_name": "car-ws-abcd1234",
                "message": "container stopped and removed",
            },
        }

    monkeypatch.setattr(HubSupervisor, "cleanup_worktree", _fake_cleanup)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "worktree",
            "cleanup",
            "wt-1",
            "--path",
            str(hub_root),
            "--no-archive",
        ],
    )
    assert result.exit_code == 0
    assert "docker_cleanup=removed" in result.output
    assert "container=car-ws-abcd1234" in result.output


def test_cli_hub_worktree_archive_uses_cleanup_with_archive(
    tmp_path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    calls = {}

    def _fake_cleanup(
        self,
        *,
        worktree_repo_id,
        delete_branch=False,
        delete_remote=False,
        archive=True,
        force_archive=False,
        archive_note=None,
        force=False,
        force_attestation=None,
        archive_profile=None,
    ):
        calls["worktree_repo_id"] = worktree_repo_id
        calls["delete_branch"] = delete_branch
        calls["delete_remote"] = delete_remote
        calls["archive"] = archive
        calls["force_archive"] = force_archive
        calls["archive_note"] = archive_note
        calls["force"] = force
        calls["force_attestation"] = force_attestation
        calls["archive_profile"] = archive_profile
        calls["has_backend_orchestrator_builder"] = (
            self._backend_orchestrator_builder is not None
        )

    monkeypatch.setattr(HubSupervisor, "cleanup_worktree", _fake_cleanup)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "worktree",
            "archive",
            "wt-1",
            "--path",
            str(hub_root),
            "--delete-branch",
            "--delete-remote",
            "--force",
            "--force-archive",
            "--force-attestation",
            "archive forced worktree",
            "--archive-note",
            "save state",
        ],
    )
    assert result.exit_code == 0
    assert calls["worktree_repo_id"] == "wt-1"
    assert calls["delete_branch"] is True
    assert calls["delete_remote"] is True
    assert calls["archive"] is True
    assert calls["force_archive"] is True
    assert calls["archive_note"] == "save state"
    assert calls["force"] is True
    assert calls["archive_profile"] is None
    assert calls["force_attestation"] == {
        "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
        "user_request": "archive forced worktree",
        "target_scope": "hub.worktree.archive:wt-1",
    }
    assert calls["has_backend_orchestrator_builder"] is True


def test_cli_hub_worktree_archive_surfaces_failure_reason_cleanly(
    tmp_path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    def _fake_cleanup(
        self,
        *,
        worktree_repo_id,
        delete_branch=False,
        delete_remote=False,
        archive=True,
        force_archive=False,
        archive_note=None,
        force=False,
        archive_profile=None,
    ):
        raise ValueError(
            f"Worktree {worktree_repo_id} has uncommitted changes; commit or stash before archiving"
        )

    monkeypatch.setattr(HubSupervisor, "cleanup_worktree", _fake_cleanup)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "worktree",
            "archive",
            "wt-1",
            "--path",
            str(hub_root),
        ],
    )
    assert result.exit_code == 1
    assert (
        "Worktree wt-1 has uncommitted changes; commit or stash before archiving"
        in result.output
    )
    assert "Traceback" not in result.output


def test_cli_hub_worktree_archive_forwards_archive_profile(
    tmp_path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    calls: dict[str, object] = {}

    def _fake_cleanup(self, **kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(HubSupervisor, "cleanup_worktree", _fake_cleanup)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "worktree",
            "archive",
            "wt-1",
            "--path",
            str(hub_root),
            "--archive-profile",
            "full",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["archive_profile"] == "full"


def test_cli_hub_worktree_setup_posts_commands_payload(tmp_path, monkeypatch) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    captured: dict[str, object] = {}

    class _Response:
        status_code = 200
        url = "http://127.0.0.1:4517/hub/repos/base/worktree-setup"
        text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {}

    def _fake_request(
        method, url, json=None, timeout=None, headers=None, follow_redirects=None
    ):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        captured["headers"] = headers
        captured["follow_redirects"] = follow_redirects
        return _Response()

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.commands.utils.httpx.request",
        _fake_request,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "hub",
            "worktree",
            "setup",
            "base",
            "make setup",
            "--path",
            str(hub_root),
        ],
    )
    assert result.exit_code == 0
    assert captured["method"] == "POST"
    assert str(captured["url"]).endswith("/hub/repos/base/worktree-setup")
    assert captured["json"] == {"commands": ["make setup"]}
