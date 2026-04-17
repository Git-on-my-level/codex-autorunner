from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

DEFAULT_PYTEST_TEMP_ROOT_MAX_BYTES = 5 * 1024 * 1024 * 1024
_TEMP_ENV_KEYS = ("TMPDIR", "TMP", "TEMP")


def _pytest_process_token(environ: dict[str, str]) -> str:
    worker_id = environ.get("PYTEST_XDIST_WORKER")
    if worker_id:
        return worker_id
    return f"p{os.getpid():x}"


def _repo_runtime_key(repo_root: Path) -> str:
    return hashlib.sha1(
        str(repo_root.expanduser().resolve(strict=False)).encode("utf-8")
    ).hexdigest()[:10]


def _load_pytest_temp_cleanup_module(repo_root: Path) -> ModuleType:
    src_dir = repo_root / "src"
    src_path = str(src_dir)
    if sys.path[:1] != [src_path] and src_path not in sys.path:
        sys.path.insert(0, src_path)
    return importlib.import_module("codex_autorunner.core.pytest_temp_cleanup")


def _system_temp_root(repo_root: Path) -> Path:
    cleanup_module = _load_pytest_temp_cleanup_module(repo_root)
    return cleanup_module.system_temp_root()


@dataclass(frozen=True)
class HermeticTestRoots:
    repo_root: Path
    run_token: str
    process_token: str
    temp_root_max_bytes: int
    runtime_key: str
    pytest_runtime_root: Path
    pytest_temp_root: Path
    pytest_temp_run_root: Path
    pytest_process_root: Path
    pytest_tmp_root: Path
    pytest_home_root: Path
    pytest_opencode_state_root: Path
    pytest_global_state_root: Path

    @classmethod
    def from_repo_root(
        cls, repo_root: Path, *, environ: dict[str, str] | None = None
    ) -> HermeticTestRoots:
        env = os.environ if environ is None else environ
        resolved_repo_root = Path(repo_root).resolve(strict=False)
        runtime_key = _repo_runtime_key(resolved_repo_root)
        run_token = env.setdefault("CAR_PYTEST_RUN_TOKEN", uuid.uuid4().hex[:8])
        process_token = _pytest_process_token(env)
        temp_root_max_bytes = int(
            env.get(
                "CAR_PYTEST_TEMP_ROOT_MAX_BYTES",
                str(DEFAULT_PYTEST_TEMP_ROOT_MAX_BYTES),
            )
        )

        pytest_runtime_root = (
            _system_temp_root(resolved_repo_root) / f"cp-{runtime_key}"
        )
        pytest_temp_root = pytest_runtime_root / "t"
        pytest_temp_run_root = pytest_temp_root / run_token
        pytest_process_root = pytest_temp_run_root / process_token
        pytest_tmp_root = pytest_process_root / "tmp"
        pytest_home_root = pytest_process_root / "home"
        pytest_opencode_state_root = (
            resolved_repo_root / ".codex-autorunner" / "pytest-opencode-state"
        )
        pytest_global_state_root = (
            pytest_opencode_state_root / run_token / process_token
        )

        return cls(
            repo_root=resolved_repo_root,
            run_token=run_token,
            process_token=process_token,
            temp_root_max_bytes=temp_root_max_bytes,
            runtime_key=runtime_key,
            pytest_runtime_root=pytest_runtime_root,
            pytest_temp_root=pytest_temp_root,
            pytest_temp_run_root=pytest_temp_run_root,
            pytest_process_root=pytest_process_root,
            pytest_tmp_root=pytest_tmp_root,
            pytest_home_root=pytest_home_root,
            pytest_opencode_state_root=pytest_opencode_state_root,
            pytest_global_state_root=pytest_global_state_root,
        )

    def load_pytest_temp_cleanup_module(self) -> ModuleType:
        return _load_pytest_temp_cleanup_module(self.repo_root)

    def prepare_process_environment(self) -> None:
        self.pytest_tmp_root.mkdir(parents=True, exist_ok=True)
        xdg_cache_home = self.pytest_home_root / ".cache"
        xdg_config_home = self.pytest_home_root / ".config"
        xdg_data_home = self.pytest_home_root / ".local" / "share"
        xdg_state_home = self.pytest_home_root / ".local" / "state"
        for path in (
            self.pytest_home_root,
            xdg_cache_home,
            xdg_config_home,
            xdg_data_home,
            xdg_state_home,
        ):
            path.mkdir(parents=True, exist_ok=True)

        for key in _TEMP_ENV_KEYS:
            os.environ[key] = str(self.pytest_tmp_root)
        os.environ["HOME"] = str(self.pytest_home_root)
        os.environ["USERPROFILE"] = str(self.pytest_home_root)
        os.environ["XDG_CACHE_HOME"] = str(xdg_cache_home)
        os.environ["XDG_CONFIG_HOME"] = str(xdg_config_home)
        os.environ["XDG_DATA_HOME"] = str(xdg_data_home)
        os.environ["XDG_STATE_HOME"] = str(xdg_state_home)
        tempfile.tempdir = None

    def prune_inactive_pytest_temp_runs(
        self, *, min_age_seconds: float = 300.0
    ) -> None:
        cleanup_module = self.load_pytest_temp_cleanup_module()
        cleanup_module.cleanup_repo_pytest_temp_runs(
            self.repo_root,
            keep_run_tokens={self.run_token},
            min_age_seconds=min_age_seconds,
        )

    def prune_inactive_repo_temp_roots(self, *, min_age_seconds: float = 300.0) -> None:
        cleanup_module = self.load_pytest_temp_cleanup_module()
        cleanup_module.cleanup_repo_managed_temp_paths(
            self.repo_root,
            keep_run_tokens={self.run_token},
            min_age_seconds=min_age_seconds,
        )

    def prune_old_opencode_state_runs(self, *, max_age_seconds: int = 86400) -> None:
        if not self.pytest_opencode_state_root.exists():
            return
        cutoff = time.time() - max_age_seconds
        for path in self.pytest_opencode_state_root.iterdir():
            if path.name == self.run_token:
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True)

    def prior_state_run_roots(self) -> set[Path]:
        if not self.pytest_opencode_state_root.exists():
            return set()
        return {
            path
            for path in self.pytest_opencode_state_root.iterdir()
            if path.is_dir() and path.name != self.run_token
        }
