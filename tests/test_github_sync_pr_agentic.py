import subprocess
from pathlib import Path

import pytest

from codex_autorunner.github import GitHubError, GitHubService


def _ok_completed(
    stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["x"], returncode=0, stdout=stdout, stderr=stderr
    )


def test_sync_pr_invokes_codex_with_small_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    svc = GitHubService(
        repo_root,
        raw_config={
            "codex": {
                "binary": "codex",
                "args": ["--yolo", "exec"],
                "models": {"small": "gpt-5.1-codex-mini", "large": None},
            },
            "github": {"sync_agent_timeout_seconds": 123},
        },
    )

    # Avoid calling real gh/git.
    monkeypatch.setattr(svc, "gh_available", lambda: True)
    monkeypatch.setattr(svc, "gh_authenticated", lambda: True)
    monkeypatch.setattr(
        svc,
        "repo_info",
        lambda: type(
            "R", (), {"default_branch": "main", "name_with_owner": "o/r", "url": "u"}
        )(),
    )
    monkeypatch.setattr(svc, "current_branch", lambda **_: "feature/test")
    monkeypatch.setattr(svc, "is_clean", lambda **_: True)
    monkeypatch.setattr(svc, "pr_for_branch", lambda **_: None)
    monkeypatch.setattr(svc, "read_link_state", lambda: {"issue": {"number": 7}})

    run_calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        run_calls.append({"cmd": cmd, "kwargs": kwargs})
        # Codex agent run
        if cmd and cmd[0] == "codex":
            assert "--model" in cmd
            assert "gpt-5.1-codex-mini" in cmd
            assert kwargs["cwd"] == str(repo_root)
            assert kwargs["timeout"] == 123
            return _ok_completed(stdout="done")
        # gh pr create
        if cmd[:3] == ["gh", "pr", "create"]:
            return _ok_completed(stdout="https://github.com/o/r/pull/1\n")
        return _ok_completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    out = svc.sync_pr(draft=True)
    assert out["status"] == "ok"
    assert any(c["cmd"] and c["cmd"][0] == "codex" for c in run_calls)


def test_sync_pr_omits_model_when_small_is_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    svc = GitHubService(
        repo_root,
        raw_config={
            "codex": {
                "binary": "codex",
                "args": ["--yolo", "exec"],
                "models": {"small": None},
            },
            "github": {"sync_agent_timeout_seconds": 5},
        },
    )

    monkeypatch.setattr(svc, "gh_available", lambda: True)
    monkeypatch.setattr(svc, "gh_authenticated", lambda: True)
    monkeypatch.setattr(
        svc,
        "repo_info",
        lambda: type(
            "R", (), {"default_branch": "main", "name_with_owner": "o/r", "url": "u"}
        )(),
    )
    monkeypatch.setattr(svc, "current_branch", lambda **_: "feature/test")
    monkeypatch.setattr(svc, "is_clean", lambda **_: True)
    monkeypatch.setattr(svc, "pr_for_branch", lambda **_: None)
    monkeypatch.setattr(svc, "read_link_state", lambda: {})

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "codex":
            assert "--model" not in cmd
            return _ok_completed(stdout="done")
        if cmd[:3] == ["gh", "pr", "create"]:
            return _ok_completed(stdout="https://github.com/o/r/pull/1\n")
        return _ok_completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = svc.sync_pr(draft=True)
    assert out["status"] == "ok"


def test_sync_pr_surfaces_agent_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    svc = GitHubService(
        repo_root,
        raw_config={
            "codex": {
                "binary": "codex",
                "args": ["--yolo", "exec"],
                "models": {"small": "gpt-5.1-codex-mini"},
            },
            "github": {"sync_agent_timeout_seconds": 5},
        },
    )

    monkeypatch.setattr(svc, "gh_available", lambda: True)
    monkeypatch.setattr(svc, "gh_authenticated", lambda: True)
    monkeypatch.setattr(
        svc,
        "repo_info",
        lambda: type(
            "R", (), {"default_branch": "main", "name_with_owner": "o/r", "url": "u"}
        )(),
    )
    monkeypatch.setattr(svc, "current_branch", lambda **_: "feature/test")
    monkeypatch.setattr(svc, "is_clean", lambda **_: True)

    gh_called = {"pr_create": False}

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "codex":
            return subprocess.CompletedProcess(
                args=cmd, returncode=3, stdout="some stdout\n", stderr="agent failed\n"
            )
        if cmd[:3] == ["gh", "pr", "create"]:
            gh_called["pr_create"] = True
            return _ok_completed(stdout="https://github.com/o/r/pull/1\n")
        return _ok_completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(GitHubError) as exc:
        svc.sync_pr(draft=True)
    msg = str(exc.value)
    assert "Codex sync agent failed" in msg
    assert "cmd:" in msg
    assert "agent failed" in msg
    assert gh_called["pr_create"] is False
