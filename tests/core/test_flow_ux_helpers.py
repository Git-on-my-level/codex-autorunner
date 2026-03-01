from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core.flows import ux_helpers
from codex_autorunner.core.flows.worker_process import FlowWorkerHealth
from codex_autorunner.integrations.github.service import RepoInfo


def test_bootstrap_check_ready_when_tickets_exist(tmp_path):
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "--\nagent: codex\ndone: false\n--\n", encoding="utf-8"
    )

    result = ux_helpers.bootstrap_check(tmp_path)

    assert result.status == "ready"


def test_bootstrap_check_needs_issue_with_github(tmp_path):
    class DummyGH:
        def __init__(self, repo_root):
            self.repo_root = repo_root

        def gh_available(self):
            return True

        def gh_authenticated(self):
            return True

        def repo_info(self):
            return RepoInfo(
                name_with_owner="org/repo", url="https://github.com/org/repo"
            )

    result = ux_helpers.bootstrap_check(tmp_path, github_service_factory=DummyGH)

    assert result.status == "needs_issue"
    assert result.github_available is True
    assert result.repo_slug == "org/repo"


def test_seed_issue_from_plan():
    content = ux_helpers.seed_issue_from_text("do things")
    assert "do things" in content


def test_seed_issue_from_github(tmp_path):
    class DummyGH:
        def __init__(self, repo_root):
            self.repo_root = repo_root

        def gh_available(self):
            return True

        def gh_authenticated(self):
            return True

        def validate_issue_same_repo(self, issue_ref):
            assert issue_ref == "#5"
            return 5

        def issue_view(self, number: int):
            return {
                "number": number,
                "title": "Example",
                "url": "https://github.com/org/repo/issues/5",
                "state": "open",
                "author": {"login": "alice"},
                "body": "Body text",
            }

        def repo_info(self):
            return RepoInfo(
                name_with_owner="org/repo", url="https://github.com/org/repo"
            )

    result = ux_helpers.seed_issue_from_github(
        tmp_path, "#5", github_service_factory=DummyGH
    )

    assert result.issue_number == 5
    assert result.repo_slug == "org/repo"
    assert "# Issue #5" in result.content
    assert "alice" in result.content


def test_ensure_worker_closes_spawned_stream_handles(monkeypatch, tmp_path: Path):
    class DummyStream:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    proc = SimpleNamespace(pid=123)
    stdout = DummyStream()
    stderr = DummyStream()

    health = FlowWorkerHealth(
        status="dead",
        pid=None,
        cmdline=[],
        artifact_path=tmp_path / ".codex-autorunner" / "flows" / "run",
    )
    health.artifact_path.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        ux_helpers, "check_worker_health", lambda *_args, **_kwargs: health
    )
    monkeypatch.setattr(
        ux_helpers, "clear_worker_metadata", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        ux_helpers,
        "spawn_flow_worker",
        lambda *_args, **_kwargs: (proc, stdout, stderr),
    )

    result = ux_helpers.ensure_worker(tmp_path, "3022db08-82b8-40dd-8cfa-d04eb0fcded2")

    assert result["status"] == "spawned"
    assert result["proc"] is proc
    assert result["stdout"] is None
    assert result["stderr"] is None
    assert stdout.closed is True
    assert stderr.closed is True
