from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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


def test_rate_limit_hit_sets_shared_cooldown_across_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAR_GLOBAL_STATE_ROOT", str(tmp_path / "global-state"))
    rate_limited_calls = {"count": 0}
    blocked_calls = {"count": 0}

    def _rate_limited_runner(
        args: list[str], *, cwd: Path, timeout_seconds: int, check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = args, cwd, timeout_seconds, check
        rate_limited_calls["count"] += 1
        raise GitHubError(
            "Command failed: gh pr view 17 --json ...: API rate limit exceeded",
            status_code=429,
        )

    def _should_not_run(
        args: list[str], *, cwd: Path, timeout_seconds: int, check: bool
    ) -> subprocess.CompletedProcess[str]:
        _ = args, cwd, timeout_seconds, check
        blocked_calls["count"] += 1
        return subprocess.CompletedProcess(
            ["gh", "pr", "view", "17"],
            0,
            stdout="{}",
            stderr="",
        )

    service_one = GitHubService(
        tmp_path / "repo-one",
        raw_config={},
        traffic_class="polling",
        gh_runner=_rate_limited_runner,
    )
    service_two = GitHubService(
        tmp_path / "repo-two",
        raw_config={},
        traffic_class="interactive",
        gh_runner=_should_not_run,
    )

    with pytest.raises(GitHubError, match="rate limit exceeded"):
        service_one.pr_view(number=17)

    with pytest.raises(GitHubError, match="global cooldown active"):
        service_two.pr_view(number=18)

    assert rate_limited_calls["count"] == 1
    assert blocked_calls["count"] == 0
