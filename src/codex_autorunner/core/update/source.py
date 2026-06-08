from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Union

BUILD_ARTIFACT_DIRS = ("build", "dist", ".eggs")
BUILD_ARTIFACT_GLOBS = ("*.egg-info", "src/*.egg-info")
CACHE_RECOVERY_HINTS = (
    "unresolved deltas left after unpacking",
    "unpack-objects failed",
    "pack has bad object",
    "bad object",
    "index file corrupt",
    "object file is empty",
    "unable to read sha1 file",
)
CMD_TIMEOUT_SECONDS = 300

__all__ = (
    "BUILD_ARTIFACT_DIRS",
    "BUILD_ARTIFACT_GLOBS",
    "CACHE_RECOVERY_HINTS",
    "CMD_TIMEOUT_SECONDS",
    "cache_refresh_failure_is_retryable",
    "cleanup_build_artifacts",
    "prepare_update_source",
    "refresh_failure_is_retryable",
    "reset_cache_for_retry",
)


def run_git_cmd(cmd: list[str], cwd: Path) -> None:
    """Run a git subprocess command, raising on failure."""
    try:
        subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        detail = f"Command failed: {' '.join(cmd)}\nStdout: {exc.stdout}\nStderr: {exc.stderr}"
        raise RuntimeError(detail) from exc


def _remove_update_artifact(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def cleanup_build_artifacts(update_dir: Path, logger: logging.Logger) -> list[str]:
    removed: list[str] = []
    for rel_path in BUILD_ARTIFACT_DIRS:
        path = update_dir / rel_path
        if _remove_update_artifact(path):
            removed.append(rel_path)
    for pattern in BUILD_ARTIFACT_GLOBS:
        for path in sorted(update_dir.glob(pattern)):
            if _remove_update_artifact(path):
                removed.append(str(path.relative_to(update_dir)))
    if removed:
        logger.info(
            "Removed cached update build artifacts: %s",
            ", ".join(removed),
        )
    return removed


def refresh_failure_is_retryable(output_lines: Sequence[str]) -> bool:
    if not output_lines:
        return False
    haystack = "\n".join(output_lines).lower()
    if "codex-autorunner" not in haystack:
        return False
    if "no such file or directory" not in haystack:
        return False
    if "build/" not in haystack and "build\\" not in haystack:
        return False
    return (
        "failed building wheel for codex-autorunner" in haystack
        or "building wheel for codex-autorunner" in haystack
    )


def cache_refresh_failure_is_retryable(
    error: Union[BaseException, str],
) -> bool:
    message = str(error).lower()
    return any(hint in message for hint in CACHE_RECOVERY_HINTS)


def is_valid_git_repo(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def reset_cache_for_retry(
    update_dir: Path,
    *,
    logger: logging.Logger,
) -> bool:
    if not update_dir.exists() or not is_valid_git_repo(update_dir):
        logger.warning(
            "Skipping update refresh retry; cache at %s is not a valid git repo.",
            update_dir,
        )
        return False
    try:
        logger.warning(
            "Refresh failed with a retryable stale-build-artifact error; resetting tracked files and cleaning build artifacts in %s before retrying once.",
            update_dir,
        )
        run_git_cmd(["git", "reset", "--hard", "FETCH_HEAD"], cwd=update_dir)
        cleanup_build_artifacts(update_dir, logger)
    except (RuntimeError, OSError) as exc:
        logger.warning(
            "Aggressive update cache cleanup failed; refresh retry skipped. %s",
            exc,
        )
        return False
    return True


def prepare_update_source(
    update_dir: Path,
    repo_url: str,
    repo_ref: str,
    logger: logging.Logger,
) -> None:
    update_dir.parent.mkdir(parents=True, exist_ok=True)

    updated = False
    if update_dir.exists() and (update_dir / ".git").exists():
        if not is_valid_git_repo(update_dir):
            logger.warning(
                "Update cache exists but is not a valid git repo; removing %s",
                update_dir,
            )
            shutil.rmtree(update_dir)
        else:
            logger.info(
                "Updating source in %s from %s (%s)",
                update_dir,
                repo_url,
                repo_ref,
            )
            try:
                try:
                    run_git_cmd(
                        ["git", "remote", "set-url", "origin", repo_url],
                        cwd=update_dir,
                    )
                except (RuntimeError, OSError):
                    run_git_cmd(
                        ["git", "remote", "add", "origin", repo_url],
                        cwd=update_dir,
                    )
                run_git_cmd(["git", "fetch", "origin", repo_ref], cwd=update_dir)
                run_git_cmd(["git", "reset", "--hard", "FETCH_HEAD"], cwd=update_dir)
                updated = True
            except (RuntimeError, OSError) as exc:
                if not cache_refresh_failure_is_retryable(exc):
                    raise
                logger.warning(
                    "Update cache refresh failed with recoverable git corruption; removing %s and recloning. %s",
                    update_dir,
                    exc,
                )
                shutil.rmtree(update_dir)

    if not updated:
        if update_dir.exists():
            shutil.rmtree(update_dir)
        logger.info("Cloning %s into %s", repo_url, update_dir)
        run_git_cmd(["git", "clone", repo_url, str(update_dir)], cwd=update_dir.parent)
        run_git_cmd(["git", "fetch", "origin", repo_ref], cwd=update_dir)
        run_git_cmd(["git", "reset", "--hard", "FETCH_HEAD"], cwd=update_dir)

    cleanup_build_artifacts(update_dir, logger)
