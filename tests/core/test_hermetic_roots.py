from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from tests.support import hermetic_roots as roots_module


def test_from_repo_root_builds_per_run_per_worker_paths(
    tmp_path: Path, monkeypatch
) -> None:
    system_tmp = tmp_path / "system-tmp"
    monkeypatch.setattr(roots_module, "_system_temp_root", lambda _repo: system_tmp)

    repo_root = tmp_path / "repo"
    env = {
        "CAR_PYTEST_RUN_TOKEN": "run-123",
        "PYTEST_XDIST_WORKER": "gw7",
        "CAR_PYTEST_TEMP_ROOT_MAX_BYTES": "2048",
    }

    roots = roots_module.HermeticTestRoots.from_repo_root(repo_root, environ=env)

    assert roots.run_token == "run-123"
    assert roots.process_token == "gw7"
    assert roots.temp_root_max_bytes == 2048
    assert roots.pytest_temp_run_root == roots.pytest_temp_root / "run-123"
    assert roots.pytest_process_root == roots.pytest_temp_run_root / "gw7"
    assert roots.pytest_tmp_root == roots.pytest_process_root / "tmp"
    assert roots.pytest_home_root == roots.pytest_process_root / "home"
    assert roots.pytest_global_state_root == (
        repo_root.resolve(strict=False)
        / ".codex-autorunner"
        / "pytest-opencode-state"
        / "run-123"
        / "gw7"
    )


def test_prepare_process_environment_sets_hermetic_temp_and_home(
    tmp_path: Path, monkeypatch
) -> None:
    system_tmp = tmp_path / "system-tmp"
    monkeypatch.setattr(roots_module, "_system_temp_root", lambda _repo: system_tmp)

    roots = roots_module.HermeticTestRoots.from_repo_root(
        tmp_path / "repo",
        environ={"CAR_PYTEST_RUN_TOKEN": "run-1", "PYTEST_XDIST_WORKER": "gw1"},
    )

    tracked_keys = (
        "TMPDIR",
        "TMP",
        "TEMP",
        "HOME",
        "USERPROFILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    )
    previous = {key: os.environ.get(key) for key in tracked_keys}
    try:
        roots.prepare_process_environment()

        assert os.environ["TMPDIR"] == str(roots.pytest_tmp_root)
        assert os.environ["TMP"] == str(roots.pytest_tmp_root)
        assert os.environ["TEMP"] == str(roots.pytest_tmp_root)
        assert os.environ["HOME"] == str(roots.pytest_home_root)
        assert os.environ["USERPROFILE"] == str(roots.pytest_home_root)
        assert os.environ["XDG_CACHE_HOME"] == str(roots.pytest_home_root / ".cache")
        assert os.environ["XDG_CONFIG_HOME"] == str(roots.pytest_home_root / ".config")
        assert os.environ["XDG_DATA_HOME"] == str(
            roots.pytest_home_root / ".local" / "share"
        )
        assert os.environ["XDG_STATE_HOME"] == str(
            roots.pytest_home_root / ".local" / "state"
        )

        assert roots.pytest_tmp_root.exists()
        assert roots.pytest_home_root.exists()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_prune_inactive_pytest_temp_runs_routes_through_cleanup_module(
    tmp_path: Path, monkeypatch
) -> None:
    system_tmp = tmp_path / "system-tmp"
    monkeypatch.setattr(roots_module, "_system_temp_root", lambda _repo: system_tmp)
    calls: list[tuple[Path, set[str], float]] = []

    def _cleanup_repo_pytest_temp_runs(
        repo_root: Path, *, keep_run_tokens: set[str], min_age_seconds: float
    ) -> None:
        calls.append((repo_root, keep_run_tokens, min_age_seconds))

    monkeypatch.setattr(
        roots_module,
        "_load_pytest_temp_cleanup_module",
        lambda _repo: SimpleNamespace(
            cleanup_repo_pytest_temp_runs=_cleanup_repo_pytest_temp_runs
        ),
    )

    roots = roots_module.HermeticTestRoots.from_repo_root(
        tmp_path / "repo",
        environ={"CAR_PYTEST_RUN_TOKEN": "run-z", "PYTEST_XDIST_WORKER": "gw2"},
    )

    roots.prune_inactive_pytest_temp_runs(min_age_seconds=123.0)

    assert calls == [(roots.repo_root, {"run-z"}, 123.0)]
