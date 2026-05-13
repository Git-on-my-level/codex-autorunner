from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tests import conftest as test_conftest


def test_prior_run_flow_worker_cleanup_ignores_orphaned_flow_workers(
    tmp_path: Path,
) -> None:
    process = SimpleNamespace(
        command="python -m codex_autorunner flow worker --repo /tmp/repo --run-id 1"
    )
    cleanup_module = SimpleNamespace(find_processes_using_path=lambda _root: (process,))
    hermetic_roots = SimpleNamespace(
        load_pytest_temp_cleanup_module=lambda: cleanup_module
    )

    assert (
        test_conftest._has_active_non_flow_worker_processes(tmp_path, hermetic_roots)
        is False
    )


def test_prior_run_flow_worker_cleanup_skips_active_pytest_session(
    tmp_path: Path,
) -> None:
    process = SimpleNamespace(command="python -m pytest -n 8")
    cleanup_module = SimpleNamespace(find_processes_using_path=lambda _root: (process,))
    hermetic_roots = SimpleNamespace(
        load_pytest_temp_cleanup_module=lambda: cleanup_module
    )

    assert (
        test_conftest._has_active_non_flow_worker_processes(tmp_path, hermetic_roots)
        is True
    )
