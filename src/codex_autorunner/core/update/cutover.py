"""Atomic symlink cutover and car wrapper sync for staged updates."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path


class CutoverError(RuntimeError):
    """Raised when symlink cutover or wrapper sync fails."""


class CutoverManager:
    """Manage CURRENT/PREV venv symlinks and post-cutover cleanup."""

    def __init__(
        self,
        *,
        current_venv_link: Path,
        prev_venv_link: Path,
        pipx_root: Path,
        keep_old_venvs: int = 3,
        logger: logging.Logger | None = None,
    ) -> None:
        self.current_venv_link = current_venv_link.expanduser()
        self.prev_venv_link = prev_venv_link.expanduser()
        self.pipx_root = pipx_root.expanduser()
        self.keep_old_venvs = max(0, int(keep_old_venvs))
        self.logger = logger or logging.getLogger(__name__)

    def initialize_current_link(
        self,
        default_target: Path,
        *,
        detected_candidates: tuple[Path, ...] = (),
    ) -> Path:
        """Ensure ``current_venv_link`` exists and return its resolved target."""
        default_target = default_target.expanduser().resolve()
        if self.current_venv_link.is_symlink():
            current_target = self.resolve_link(self.current_venv_link)
            if current_target is None:
                raise CutoverError(
                    f"Unable to resolve current venv from {self.current_venv_link}."
                )
            return current_target

        if not self._is_usable_venv(default_target):
            candidate_text = (
                ", ".join(str(path) for path in detected_candidates)
                if detected_candidates
                else "none"
            )
            raise CutoverError(
                "Unable to initialize current venv link because the default target "
                f"is not a usable venv: {default_target}. Detected candidate venvs: "
                f"{candidate_text}."
            )
        else:
            self.logger.info(
                "Initializing %s -> %s", self.current_venv_link, default_target
            )
            self._symlink(default_target, self.current_venv_link)
            current_target = self.resolve_link(self.current_venv_link)
        if current_target is None:
            raise CutoverError(
                f"Unable to resolve current venv from {self.current_venv_link}."
            )
        return current_target

    def prepare(self, current_target: Path) -> None:
        """Point ``prev_venv_link`` at the live venv before cutover."""
        current_target = current_target.expanduser().resolve()
        self.logger.info("Switching %s -> %s", self.prev_venv_link, current_target)
        self._symlink(current_target, self.prev_venv_link)

    def flip_to(self, candidate: Path) -> None:
        """Atomically point ``current_venv_link`` at the staged candidate."""
        candidate = candidate.expanduser().resolve()
        self.logger.info("Switching %s -> %s", self.current_venv_link, candidate)
        self._symlink(candidate, self.current_venv_link)

    def rollback_to(self, previous: Path) -> None:
        """Restore ``current_venv_link`` to a previous venv target."""
        previous = previous.expanduser().resolve()
        self.logger.info("Rolling back %s -> %s", self.current_venv_link, previous)
        self._symlink(previous, self.current_venv_link)

    def sync_car_wrapper(
        self,
        *,
        package_src: Path,
        local_bin: Path,
        car_wrapper_path: Path | None = None,
    ) -> Path:
        """Install or refresh the user-facing ``car`` CLI wrapper script."""
        local_bin = local_bin.expanduser()
        wrapper_path = (
            car_wrapper_path.expanduser()
            if car_wrapper_path is not None
            else local_bin / "car"
        )
        script_path = package_src / "scripts" / "install-car-cli-wrapper.sh"
        if script_path.is_file():
            env = os.environ.copy()
            env["CURRENT_VENV_LINK"] = str(self.current_venv_link)
            env["LOCAL_BIN"] = str(local_bin)
            env["CAR_WRAPPER_PATH"] = str(wrapper_path)
            subprocess.run(
                ["bash", str(script_path)],
                check=True,
                env=env,
                timeout=60,
            )
            return wrapper_path

        self._write_car_wrapper(wrapper_path)
        return wrapper_path

    def prune_old_venvs(self) -> list[Path]:
        """Remove old ``codex-autorunner.next-*`` venvs beyond ``keep_old_venvs``."""
        venvs_dir = self.pipx_root / "venvs"
        staged = sorted(
            venvs_dir.glob("codex-autorunner.next-*"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if len(staged) <= self.keep_old_venvs:
            return []

        current_real = self.resolve_link(self.current_venv_link)
        prev_real = self.resolve_link(self.prev_venv_link)
        removed: list[Path] = []
        for old in staged[self.keep_old_venvs :]:
            old_real = self.resolve_link(old)
            if old_real is not None and old_real in {current_real, prev_real}:
                continue
            self.logger.info("Pruning old staged venv %s", old)
            shutil.rmtree(old, ignore_errors=True)
            wheelhouse = Path(f"{old}.wheelhouse")
            if wheelhouse.exists():
                shutil.rmtree(wheelhouse, ignore_errors=True)
            removed.append(old)
        return removed

    @staticmethod
    def resolve_link(link: Path) -> Path | None:
        """Resolve a symlink target to an absolute path when it exists."""
        try:
            resolved = link.expanduser().resolve(strict=False)
        except OSError:
            return None
        return resolved if resolved.exists() else None

    @staticmethod
    def _is_usable_venv(path: Path) -> bool:
        python_bin = path / "bin" / "python"
        return path.is_dir() and python_bin.exists() and os.access(python_bin, os.X_OK)

    def _symlink(self, target: Path, link: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        tmp_link = link.with_name(f"{link.name}.tmp-{os.getpid()}")
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        os.symlink(str(target), str(tmp_link))
        os.replace(tmp_link, link)

    def _write_car_wrapper(self, wrapper_path: Path) -> None:
        current_link = self.current_venv_link
        wrapper_path.parent.mkdir(parents=True, exist_ok=True)
        content = f"""#!/usr/bin/env bash
set -euo pipefail

# Installed hub venv python: {current_link}/bin/python
current_venv_link="${{CAR_CURRENT_VENV_LINK:-{current_link}}}"
python_bin="${{current_venv_link}}/bin/python"

if [[ ! -L "${{current_venv_link}}" ]]; then
  echo "car: active codex-autorunner venv symlink is missing: ${{current_venv_link}}" >&2
  echo "car: run the hub refresh script to recreate it, or set CAR_CURRENT_VENV_LINK." >&2
  exit 127
fi

if [[ ! -x "${{python_bin}}" ]]; then
  echo "car: active codex-autorunner Python is missing or not executable: ${{python_bin}}" >&2
  echo "car: current symlink target: $(readlink "${{current_venv_link}}" 2>/dev/null || echo unknown)" >&2
  exit 127
fi

exec "${{python_bin}}" -m codex_autorunner.cli "$@"
"""
        wrapper_path.write_text(content, encoding="utf-8")
        wrapper_path.chmod(0o755)
        marker = f"{current_link}/bin/python"
        if marker not in wrapper_path.read_text(encoding="utf-8"):
            raise CutoverError(
                f"car wrapper verification failed: {wrapper_path} does not dispatch "
                f"through {marker}"
            )
        self.logger.info(
            "Installed car wrapper at %s -> %s/bin/python",
            wrapper_path,
            current_link,
        )
