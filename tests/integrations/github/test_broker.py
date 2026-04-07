from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import codex_autorunner.integrations.github.broker as github_broker
from codex_autorunner.integrations.github.broker import GitHubCliBroker
from codex_autorunner.integrations.github.service import GitHubError, GitHubService


def test_rate_limit_status_uses_shared_broker_cache_across_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAR_GLOBAL_STATE_ROOT", str(tmp_path / "global-state"))
    calls = {"count": 0}
    payload = {
        "resources": {
            "core": {"remaining": 4999, "limit": 5000, "reset": 2147483647},
            "graphql": {"remaining": 4999, "limit": 5000, "reset": 2147483647},
        }
    }

    def _runner(
        args: list[str], *, cwd: Path, timeout_seconds: int, check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = cwd, timeout_seconds, check
        calls["count"] += 1
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    service_one = GitHubService(tmp_path / "repo-one", raw_config={}, gh_runner=_runner)
    service_two = GitHubService(tmp_path / "repo-two", raw_config={}, gh_runner=_runner)

    first = service_one.rate_limit_status()
    second = service_two.rate_limit_status()

    assert first == payload
    assert second == payload
    assert calls["count"] == 1


def test_rate_limit_cache_uses_config_root_for_relative_global_state(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_one = hub_root / "workspace" / "repo-one"
    repo_two = hub_root / "workspace" / "repo-two"
    repo_one.mkdir(parents=True)
    repo_two.mkdir(parents=True)
    calls = {"count": 0}
    payload = {
        "resources": {
            "core": {"remaining": 4999, "limit": 5000, "reset": 2147483647},
            "graphql": {"remaining": 4999, "limit": 5000, "reset": 2147483647},
        }
    }

    def _runner(
        args: list[str], *, cwd: Path, timeout_seconds: int, check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = cwd, timeout_seconds, check
        calls["count"] += 1
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    raw_config = {"state_roots": {"global": ".shared-state"}}
    service_one = GitHubService(
        repo_one,
        raw_config=raw_config,
        config_root=hub_root,
        gh_runner=_runner,
    )
    service_two = GitHubService(
        repo_two,
        raw_config=raw_config,
        config_root=hub_root,
        gh_runner=_runner,
    )

    assert service_one.rate_limit_status() == payload
    assert service_two.rate_limit_status() == payload
    assert calls["count"] == 1
    assert (hub_root / ".shared-state" / "github" / "github-cli.sqlite3").exists()


def test_rate_limit_hit_sets_shared_cooldown_across_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAR_GLOBAL_STATE_ROOT", str(tmp_path / "global-state"))
    rate_limited_calls = {"count": 0}
    interactive_calls = {"count": 0}
    polling_calls = {"count": 0}

    def _rate_limited_runner(
        args: list[str], *, cwd: Path, timeout_seconds: int, check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = args, cwd, timeout_seconds, check
        rate_limited_calls["count"] += 1
        raise GitHubError(
            "Command failed: gh pr view 17 --json ...: API rate limit exceeded",
            status_code=429,
        )

    def _interactive_runner(
        args: list[str], *, cwd: Path, timeout_seconds: int, check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = args, cwd, timeout_seconds, check
        interactive_calls["count"] += 1
        return subprocess.CompletedProcess(
            ["gh", "pr", "view", "18"],
            0,
            stdout=json.dumps({"number": 18, "title": "Interactive bypass"}),
            stderr="",
        )

    def _polling_runner(
        args: list[str], *, cwd: Path, timeout_seconds: int, check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = args, cwd, timeout_seconds, check
        polling_calls["count"] += 1
        return subprocess.CompletedProcess(
            ["gh", "pr", "view", "19"],
            0,
            stdout=json.dumps({"number": 19}),
            stderr="",
        )

    rate_limited_service = GitHubService(
        tmp_path / "repo-one",
        raw_config={},
        traffic_class="polling",
        gh_runner=_rate_limited_runner,
    )
    interactive_service = GitHubService(
        tmp_path / "repo-two",
        raw_config={},
        traffic_class="interactive",
        gh_runner=_interactive_runner,
    )
    polling_service = GitHubService(
        tmp_path / "repo-three",
        raw_config={},
        traffic_class="polling",
        gh_runner=_polling_runner,
    )

    with pytest.raises(GitHubError, match="rate limit exceeded"):
        rate_limited_service.pr_view(number=17)

    interactive = interactive_service.pr_view(number=18)
    assert interactive["number"] == 18

    assert rate_limited_calls["count"] == 1
    assert interactive_calls["count"] == 1

    with pytest.raises(GitHubError, match="global cooldown active"):
        polling_service.pr_view(number=19)

    assert polling_calls["count"] == 0


def test_mutation_invalidates_shared_read_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAR_GLOBAL_STATE_ROOT", str(tmp_path / "global-state"))
    pr_view_calls = {"count": 0}
    comment_calls = {"count": 0}

    def _runner(
        args: list[str], *, cwd: Path, timeout_seconds: int, check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = cwd, timeout_seconds, check
        if args[1:3] == ["pr", "view"]:
            pr_view_calls["count"] += 1
            payload = {
                "number": 17,
                "title": f"PR snapshot {pr_view_calls['count']}",
                "state": "OPEN",
                "author": {"login": "reviewer"},
                "labels": [],
                "files": [],
                "additions": 0,
                "deletions": 0,
                "changedFiles": 0,
                "headRefName": "feature/test",
                "baseRefName": "main",
                "headRefOid": "abc123",
                "isDraft": False,
                "url": "https://github.com/acme/widgets/pull/17",
                "body": "cached body",
            }
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(payload),
                stderr="",
            )
        if args[:4] == ["gh", "api", "-X", "POST"]:
            comment_calls["count"] += 1
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({"id": 1}),
                stderr="",
            )
        raise AssertionError(f"unexpected args: {args}")

    repo_root = tmp_path / "repo"
    service_one = GitHubService(repo_root, raw_config={}, gh_runner=_runner)
    service_two = GitHubService(repo_root, raw_config={}, gh_runner=_runner)
    service_three = GitHubService(
        repo_root,
        raw_config={},
        gh_runner=_runner,
    )

    first = service_one.pr_view(number=17)
    cached = service_two.pr_view(number=17)
    mutation = service_three.create_issue_comment(
        owner="acme",
        repo="widgets",
        number=17,
        body="invalidate cache",
    )
    refreshed = service_two.pr_view(number=17)

    assert first["title"] == "PR snapshot 1"
    assert cached["title"] == "PR snapshot 1"
    assert refreshed["title"] == "PR snapshot 2"
    assert pr_view_calls["count"] == 2
    assert comment_calls["count"] == 1
    assert mutation["id"] == 1


def test_concurrent_cached_read_uses_single_shared_runner_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAR_GLOBAL_STATE_ROOT", str(tmp_path / "global-state"))
    repo_root = tmp_path / "repo"
    runner_calls = {"count": 0}
    release_first_call = threading.Event()
    first_call_started = threading.Event()

    def _runner(
        args: list[str], *, cwd: Path, timeout_seconds: int, check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = cwd, timeout_seconds, check
        runner_calls["count"] += 1
        if runner_calls["count"] == 1:
            first_call_started.set()
            assert release_first_call.wait(timeout=2.0)
            time.sleep(0.2)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {
                    "number": 17,
                    "title": "Shared cached result",
                    "state": "OPEN",
                    "author": {"login": "reviewer"},
                    "labels": [],
                    "files": [],
                    "additions": 0,
                    "deletions": 0,
                    "changedFiles": 0,
                    "headRefName": "feature/test",
                    "baseRefName": "main",
                    "headRefOid": "abc123",
                    "isDraft": False,
                    "url": "https://github.com/acme/widgets/pull/17",
                    "body": "cached body",
                }
            ),
            stderr="",
        )

    service_one = GitHubService(repo_root, raw_config={}, gh_runner=_runner)
    service_two = GitHubService(repo_root, raw_config={}, gh_runner=_runner)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(service_one.pr_view, number=17)
        assert first_call_started.wait(timeout=2.0)
        second_future = executor.submit(service_two.pr_view, number=17)
        release_first_call.set()
        first = first_future.result(timeout=2.0)
        second = second_future.result(timeout=2.0)

    assert first["title"] == "Shared cached result"
    assert second["title"] == "Shared cached result"
    assert runner_calls["count"] == 1


def test_slow_concurrent_cached_read_waits_for_shared_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAR_GLOBAL_STATE_ROOT", str(tmp_path / "global-state"))
    repo_root = tmp_path / "repo"
    runner_calls = {"count": 0}
    release_first_call = threading.Event()
    first_call_started = threading.Event()

    def _runner(
        args: list[str], *, cwd: Path, timeout_seconds: int, check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = cwd, timeout_seconds, check
        runner_calls["count"] += 1
        if runner_calls["count"] == 1:
            first_call_started.set()
            assert release_first_call.wait(timeout=5.0)
            time.sleep(3.2)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {
                    "number": 17,
                    "title": "Shared cached result",
                    "state": "OPEN",
                    "author": {"login": "reviewer"},
                    "labels": [],
                    "files": [],
                    "additions": 0,
                    "deletions": 0,
                    "changedFiles": 0,
                    "headRefName": "feature/test",
                    "baseRefName": "main",
                    "headRefOid": "abc123",
                    "isDraft": False,
                    "url": "https://github.com/acme/widgets/pull/17",
                    "body": "cached body",
                }
            ),
            stderr="",
        )

    service_one = GitHubService(repo_root, raw_config={}, gh_runner=_runner)
    service_two = GitHubService(repo_root, raw_config={}, gh_runner=_runner)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(service_one.pr_view, number=17)
        assert first_call_started.wait(timeout=2.0)
        second_future = executor.submit(service_two.pr_view, number=17)
        release_first_call.set()
        first = first_future.result(timeout=6.0)
        second = second_future.result(timeout=6.0)

    assert first["title"] == "Shared cached result"
    assert second["title"] == "Shared cached result"
    assert runner_calls["count"] == 1


def test_waiting_cacheable_read_respects_check_false_on_cooldown(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _runner(
        args: list[str], *, cwd: Path, timeout_seconds: int, check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"runner should not execute: {args}")

    broker = GitHubCliBroker(
        repo_root=repo_root,
        raw_config={},
        config_root=repo_root,
        gh_path="gh",
        runner=_runner,
        error_factory=lambda message, status_code: GitHubError(
            message, status_code=status_code
        ),
    )
    args = ["pr", "view", "17", "--json", "number,title"]
    cache_policy = github_broker._command_cache_policy(args)
    assert cache_policy is not None
    lease_key = broker._lease_key(args, cache_policy=cache_policy, cwd=repo_root)
    assert broker._claim_lease(lease_key, owner="other-owner", ttl_seconds=60)
    broker._write_state(
        "cooldown",
        {
            "cooldown_until": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reason": "rate_limit_hit",
            "traffic_class": "polling",
        },
    )

    proc = broker.run(
        args,
        cwd=repo_root,
        timeout_seconds=5,
        check=False,
        traffic_class="polling",
    )

    assert proc.returncode == 1
    assert "global cooldown active" in proc.stderr


def test_waiting_cacheable_read_honors_timeout_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAR_GLOBAL_STATE_ROOT", str(tmp_path / "global-state"))
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    clock = {"now": 1000.0}

    def _monotonic() -> float:
        return clock["now"]

    def _sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(github_broker.time, "monotonic", _monotonic)
    monkeypatch.setattr(github_broker.time, "sleep", _sleep)

    def _runner(
        args: list[str], *, cwd: Path, timeout_seconds: int, check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"runner should not execute: {args}")

    broker = GitHubCliBroker(
        repo_root=repo_root,
        raw_config={},
        config_root=repo_root,
        gh_path="gh",
        runner=_runner,
        error_factory=lambda message, status_code: GitHubError(
            message, status_code=status_code
        ),
    )
    args = ["pr", "view", "17", "--json", "number,title"]
    cache_policy = github_broker._command_cache_policy(args)
    assert cache_policy is not None
    lease_key = broker._lease_key(args, cache_policy=cache_policy, cwd=repo_root)
    assert broker._claim_lease(lease_key, owner="other-owner", ttl_seconds=60)

    with pytest.raises(
        GitHubError, match="timed out while waiting for shared cache lease"
    ):
        broker.run(
            args,
            cwd=repo_root,
            timeout_seconds=1,
            check=True,
            traffic_class="polling",
        )
