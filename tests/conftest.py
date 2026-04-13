"""Test harness configuration.

This repo uses a `src/` layout. In some developer environments an older
installed `codex_autorunner` package can shadow the local sources.

Ensure tests always import the in-repo code.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

DEFAULT_NON_INTEGRATION_TIMEOUT_SECONDS = 120
_OPENCODE_PROCESS_KIND = "opencode"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYTEST_RUNTIME_KEY = hashlib.sha1(
    str(_REPO_ROOT.expanduser().resolve(strict=False)).encode("utf-8")
).hexdigest()[:10]
_PYTEST_OPENCODE_STATE_ROOT = _REPO_ROOT / ".codex-autorunner" / "pytest-opencode-state"
_PYTEST_RUN_TOKEN = os.environ.setdefault("CAR_PYTEST_RUN_TOKEN", uuid.uuid4().hex[:8])
_PYTEST_TEMP_ROOT_MAX_BYTES = int(
    os.environ.get("CAR_PYTEST_TEMP_ROOT_MAX_BYTES", str(5 * 1024 * 1024 * 1024))
)
os.environ.setdefault("CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS", "1")


def _pytest_process_token() -> str:
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if worker_id:
        return worker_id
    return f"p{os.getpid():x}"


def _pytest_temp_env_root() -> Path:
    return _pytest_temp_run_root() / _pytest_process_token()


def _pytest_temp_run_root() -> Path:
    return _PYTEST_TEMP_ROOT / _PYTEST_RUN_TOKEN


def _pytest_global_state_root() -> Path:
    return _PYTEST_OPENCODE_STATE_ROOT / _PYTEST_RUN_TOKEN / _pytest_process_token()


def _load_pytest_temp_cleanup_module():
    src_dir = _REPO_ROOT / "src"
    src_path = str(src_dir)
    if sys.path[:1] != [src_path] and src_path not in sys.path:
        sys.path.insert(0, src_path)
    return importlib.import_module("codex_autorunner.core.pytest_temp_cleanup")


def _system_temp_root() -> Path:
    cleanup_module = _load_pytest_temp_cleanup_module()
    return cleanup_module.system_temp_root()


def _prepare_repo_local_tmpdir() -> None:
    temp_root = _pytest_temp_env_root()
    temp_root.mkdir(parents=True, exist_ok=True)
    for key in ("TMPDIR", "TMP", "TEMP"):
        os.environ[key] = str(temp_root)
    tempfile.tempdir = None


_PYTEST_RUNTIME_ROOT = _system_temp_root() / f"cp-{_PYTEST_RUNTIME_KEY}"
_PYTEST_TEMP_ROOT = _PYTEST_RUNTIME_ROOT / "t"


def _prune_old_runtime_dirs(
    root: Path, *, keep: set[str], max_age_seconds: int
) -> None:
    if not root.exists():
        return
    cutoff = time.time() - max_age_seconds
    for path in root.iterdir():
        if path.name in keep:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)


def _prune_inactive_pytest_temp_runs(*, keep: set[str]) -> None:
    cleanup_module = _load_pytest_temp_cleanup_module()
    cleanup_module.cleanup_repo_pytest_temp_runs(_REPO_ROOT, keep_run_tokens=keep)


def _format_temp_processes(processes: tuple[object, ...]) -> str:
    parts: list[str] = []
    for process in processes[:10]:
        pid = getattr(process, "pid", "?")
        command = getattr(process, "command", "?")
        path = getattr(process, "path", None)
        if path:
            parts.append(f"pid={pid} command={command} path={path}")
        else:
            parts.append(f"pid={pid} command={command}")
    remaining = len(processes) - len(parts)
    if remaining > 0:
        parts.append(f"... plus {remaining} more")
    return "; ".join(parts)


_prepare_repo_local_tmpdir()
_prune_inactive_pytest_temp_runs(keep={_PYTEST_RUN_TOKEN})
_prune_old_runtime_dirs(
    _PYTEST_OPENCODE_STATE_ROOT, keep={_PYTEST_RUN_TOKEN}, max_age_seconds=86400
)


_ORIGINAL_UNRAISABLE_HOOK = sys.unraisablehook


def _silence_event_loop_closed_unraisable(unraisable: sys.UnraisableHookArgs) -> None:
    exc = unraisable.exc_value
    if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
        # Suppress noisy asyncio transport __del__ warnings that can surface when
        # cancelling restart tasks during teardown.
        return
    _ORIGINAL_UNRAISABLE_HOOK(unraisable)


sys.unraisablehook = _silence_event_loop_closed_unraisable


def write_test_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def pytest_configure(config: pytest.Config) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src"
    src_path = str(src_dir)
    if sys.path[:1] != [src_path] and src_path not in sys.path:
        sys.path.insert(0, src_path)
    config.addinivalue_line(
        "markers",
        "docker_managed_cleanup: snapshot CAR-managed docker containers and remove any new ones after the test",
    )


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """
    Apply a default per-test timeout to non-integration tests.

    This relies on `pytest-timeout` when installed; if it isn't installed, the
    marker is inert but still documents the intent.
    """
    _ = session, config
    for item in items:
        if item.get_closest_marker("integration") is not None:
            continue
        item.add_marker(pytest.mark.timeout(DEFAULT_NON_INTEGRATION_TIMEOUT_SECONDS))


@pytest.fixture(autouse=True)
def _stub_surface_startup_handshakes_for_non_handshake_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = Path(str(request.node.fspath))
    if path.name in {"test_discord_hub_handshake.py", "test_telegram_hub_handshake.py"}:
        return

    from codex_autorunner.core.hub_control_plane.http_client import (
        HttpHubControlPlaneClient,
    )
    from codex_autorunner.core.hub_control_plane.models import HandshakeCompatibility
    from codex_autorunner.integrations.discord.service import DiscordBotService
    from codex_autorunner.integrations.telegram.service import TelegramBotService

    async def _discord_handshake_ok(self: DiscordBotService) -> bool:
        if isinstance(getattr(self, "_hub_client", None), HttpHubControlPlaneClient):
            self._hub_client = None
            self._hub_handshake_compatibility = HandshakeCompatibility(
                state="incompatible",
                reason="test fallback uses local shared-state stubs",
            )
        else:
            self._hub_handshake_compatibility = HandshakeCompatibility(
                state="compatible"
            )
        return True

    async def _telegram_handshake_ok(self: TelegramBotService) -> bool:
        if isinstance(getattr(self, "_hub_client", None), HttpHubControlPlaneClient):
            self._hub_client = None
            self._hub_handshake_compatibility = HandshakeCompatibility(
                state="incompatible",
                reason="test fallback uses local shared-state stubs",
            )
        else:
            self._hub_handshake_compatibility = HandshakeCompatibility(
                state="compatible"
            )
        return True

    monkeypatch.setattr(
        DiscordBotService, "_perform_hub_handshake", _discord_handshake_ok
    )
    monkeypatch.setattr(
        TelegramBotService, "_perform_hub_handshake", _telegram_handshake_ok
    )


def _list_car_managed_docker_containers() -> dict[str, tuple[str, ...]] | None:
    try:
        version_proc = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if version_proc.returncode != 0:
        return None

    try:
        ps_proc = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=ca.managed=true",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if ps_proc.returncode != 0:
        return None
    names = [line.strip() for line in ps_proc.stdout.splitlines() if line.strip()]
    if not names:
        return {}

    try:
        inspect_proc = subprocess.run(
            ["docker", "inspect", *names],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if inspect_proc.returncode != 0:
        return None

    try:
        payload = json.loads(inspect_proc.stdout or "[]")
    except json.JSONDecodeError:
        return None

    containers: dict[str, tuple[str, ...]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip().lstrip("/")
        if not name:
            continue
        mounts = item.get("Mounts")
        sources: list[str] = []
        if isinstance(mounts, list):
            for mount in mounts:
                if not isinstance(mount, dict):
                    continue
                if str(mount.get("Type") or "").strip() != "bind":
                    continue
                source = str(mount.get("Source") or "").strip()
                if source:
                    sources.append(source)
        containers[name] = tuple(sources)
    return containers


def _path_is_within(candidate: str, root: Path) -> bool:
    try:
        candidate_path = Path(candidate).expanduser().resolve(strict=False)
    except Exception:
        return False
    root_path = root.expanduser().resolve(strict=False)
    return candidate_path == root_path or root_path in candidate_path.parents


def _remove_car_managed_docker_containers(container_names: set[str]) -> list[str]:
    failures: list[str] = []
    for name in sorted(container_names):
        try:
            proc = subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            failures.append(f"{name}: docker rm -f timed out or docker is unavailable")
            continue
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip() or "unknown error"
            failures.append(f"{name}: {detail}")
    return failures


def _iter_opencode_registry_roots(root: Path) -> set[Path]:
    if not root.exists():
        return set()
    return {
        path.parents[2]
        for path in root.rglob(".codex-autorunner/processes/opencode")
        if path.is_dir()
    }


def _force_reap_attestation(root: Path) -> dict[str, str]:
    from codex_autorunner.core.force_attestation import (
        FORCE_ATTESTATION_REQUIRED_PHRASE,
    )

    return {
        "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
        "user_request": "pytest cleanup fixture reaping temp-rooted managed processes",
        "target_scope": f"tests.cleanup:{root}",
    }


def _reap_opencode_processes(roots: set[Path], *, force: bool) -> list[str]:
    from codex_autorunner.core.managed_processes import (
        list_process_records,
        reap_managed_processes,
    )

    failures: list[str] = []
    for root in sorted(roots):
        try:
            records = list_process_records(root, _OPENCODE_PROCESS_KIND)
        except ValueError:
            continue
        if not records:
            continue
        reap_kwargs: dict[str, object] = {}
        if force:
            reap_kwargs["force"] = True
            reap_kwargs["force_attestation"] = _force_reap_attestation(root)
        reap_managed_processes(root, **reap_kwargs)
        try:
            remaining = list_process_records(root, _OPENCODE_PROCESS_KIND)
        except ValueError:
            continue
        if not remaining:
            continue
        details = ", ".join(
            f"workspace_id={record.workspace_id} pid={record.pid} pgid={record.pgid}"
            for record in remaining
        )
        failures.append(f"{root}: {details}")
    return failures


@pytest.fixture(autouse=True)
def docker_managed_cleanup(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("docker_managed_cleanup") is None:
        yield
        return

    tmp_path = request.getfixturevalue("tmp_path")
    yield
    after = _list_car_managed_docker_containers()
    if after is None:
        return
    leaked = {
        name
        for name, sources in after.items()
        if any(_path_is_within(source, tmp_path) for source in sources)
    }
    if not leaked:
        return
    failures = _remove_car_managed_docker_containers(leaked)
    if failures:
        pytest.fail(
            "Failed to clean up CAR-managed docker containers: " + "; ".join(failures)
        )


@pytest.fixture(autouse=True)
def _configure_opencode_global_state_root(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_path = str(getattr(request.node, "path", request.node.nodeid))
    if "opencode" not in node_path:
        yield
        return
    monkeypatch.setenv("CAR_GLOBAL_STATE_ROOT", str(_pytest_global_state_root()))
    yield


@pytest.fixture(scope="session", autouse=True)
def _cleanup_codex_app_server_clients(_cleanup_pytest_temp_runs_session) -> None:
    """
    Ensure any CodexAppServerClient restart tasks are cancelled after the suite.

    Some tests intentionally crash the app-server to exercise auto-restart
    behavior; if an instance slips through without an explicit close, the
    pending restart task can emit \"Task was destroyed\" noise when the event
    loop shuts down. Running this cleanup keeps `make check` quiet for agents.
    """
    yield
    # Import lazily to avoid impacting non-app-server test collection time.
    import anyio

    from codex_autorunner.integrations.app_server.client import _close_all_clients

    anyio.run(_close_all_clients)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_opencode_processes_session(_cleanup_pytest_temp_runs_session) -> None:
    """
    Reap stale prior-run OpenCode records and fail if this run leaks any.
    """
    prior_state_roots = (
        {
            path
            for path in _PYTEST_OPENCODE_STATE_ROOT.iterdir()
            if path.is_dir() and path.name != _PYTEST_RUN_TOKEN
        }
        if _PYTEST_OPENCODE_STATE_ROOT.exists()
        else set()
    )
    _reap_opencode_processes(prior_state_roots, force=False)
    yield
    failures = _reap_opencode_processes({_pytest_global_state_root()}, force=True)
    if failures:
        raise AssertionError(
            "Leaked OpenCode managed processes remained after the test session: "
            + "; ".join(failures)
        )


@pytest.fixture(scope="session", autouse=True)
def _cleanup_pytest_temp_runs_session() -> None:
    yield
    cleanup_module = _load_pytest_temp_cleanup_module()
    env_root = _pytest_temp_env_root()
    summary = cleanup_module.cleanup_temp_paths((env_root,))
    failures: list[str] = []
    if summary.active_paths:
        failures.append(
            "active processes remained under pytest temp root: "
            + _format_temp_processes(summary.active_processes)
        )
    if summary.failed_paths:
        failures.append("; ".join(summary.failed_paths))
    if summary.bytes_before > _PYTEST_TEMP_ROOT_MAX_BYTES:
        gib = summary.bytes_before / float(1024**3)
        failures.append(
            "pytest temp root exceeded size guard "
            f"({gib:.2f} GiB > {_PYTEST_TEMP_ROOT_MAX_BYTES / float(1024**3):.2f} GiB)"
        )
    if env_root.exists():
        failures.append(f"pytest temp env root still exists after cleanup: {env_root}")
    if failures:
        raise AssertionError(" ; ".join(failures))


@pytest.fixture(autouse=True)
async def _cleanup_codex_app_server_clients_per_test() -> None:
    """
    Per-test cleanup so pending restart tasks are cancelled before the event loop
    for an async test tears down (avoids \"Task was destroyed\" noise).
    """
    yield
    from codex_autorunner.integrations.app_server.client import _close_all_clients

    await _close_all_clients()
    pending_restart_tasks = [
        t
        for t in asyncio.all_tasks()
        if t.get_coro().__qualname__.endswith(
            "CodexAppServerClient._restart_after_disconnect"
        )
    ]
    for t in pending_restart_tasks:
        t.cancel()
    if pending_restart_tasks:
        await asyncio.gather(*pending_restart_tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
def _cleanup_opencode_processes_per_test(request: pytest.FixtureRequest) -> None:
    yield
    roots = {_pytest_global_state_root()}
    tmp_path = request.node.funcargs.get("tmp_path")
    if tmp_path is not None:
        roots.update(_iter_opencode_registry_roots(tmp_path))
    failures = _reap_opencode_processes(roots, force=True)
    if failures:
        pytest.fail(
            "Leaked OpenCode managed processes remained after test teardown: "
            + "; ".join(failures)
        )


@pytest.fixture()
def hub_env(tmp_path: Path):
    """Create a minimal hub with a single initialized repo mounted under `/repos/<id>`."""

    # Import lazily so `pytest_configure()` can prepend the local src/ directory
    # before any `codex_autorunner` modules are loaded.
    from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
    from codex_autorunner.core.config import load_hub_config
    from codex_autorunner.manifest import load_manifest, save_manifest

    @dataclass(frozen=True)
    class HubEnv:
        hub_root: Path
        repo_id: str
        repo_root: Path

    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    # Put the repo under the hub's default repos_root (worktrees/ by default).
    repo_id = "repo"
    repo_root = hub_root / "worktrees" / repo_id
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)

    hub_config = load_hub_config(hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(hub_config.manifest_path, manifest, hub_root)

    return HubEnv(hub_root=hub_root, repo_id=repo_id, repo_root=repo_root)


@pytest.fixture()
def repo(hub_env) -> Path:
    """Backwards-compatible repo fixture (the hub's single test repo root)."""
    return hub_env.repo_root


@pytest.fixture()
def hub_root_only(tmp_path: Path) -> Path:
    """Create a minimal hub without any repos, for testing server-dependent commands."""
    from codex_autorunner.bootstrap import seed_hub_files

    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    return hub_root
