from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

_PYTEST_RUNTIME_TEMP_SUBDIR = "t"
_DEFAULT_LSOF_TIMEOUT_SECONDS = 10.0
_TEMP_ENV_KEYS = ("TMPDIR", "TMP", "TEMP")


@dataclass(frozen=True)
class TempRootProcess:
    pid: int
    command: str
    descriptor: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class TempPathScanResult:
    path: Path
    bytes: int
    active_processes: tuple[TempRootProcess, ...] = ()
    scan_error: str | None = None


@dataclass(frozen=True)
class TempCleanupSummary:
    scanned: int
    deleted: int
    active: int
    failed: int
    bytes_before: int
    bytes_after: int
    deleted_paths: tuple[Path, ...] = ()
    active_paths: tuple[Path, ...] = ()
    failed_paths: tuple[str, ...] = ()
    active_processes: tuple[TempRootProcess, ...] = ()


def repo_pytest_runtime_root(repo_root: Path, *, temp_base: Path | None = None) -> Path:
    key = hashlib.sha1(
        str(Path(repo_root).expanduser().resolve(strict=False)).encode("utf-8")
    ).hexdigest()[:10]
    base_root = (
        Path(temp_base).expanduser().resolve(strict=False)
        if temp_base is not None
        else system_temp_root()
    )
    return base_root / f"cp-{key}"


def repo_pytest_temp_root(repo_root: Path, *, temp_base: Path | None = None) -> Path:
    return (
        repo_pytest_runtime_root(repo_root, temp_base=temp_base)
        / _PYTEST_RUNTIME_TEMP_SUBDIR
    )


def cleanup_repo_pytest_temp_runs(
    repo_root: Path,
    *,
    keep_run_tokens: set[str] | None = None,
    dry_run: bool = False,
    temp_base: Path | None = None,
) -> TempCleanupSummary:
    temp_root = repo_pytest_temp_root(repo_root, temp_base=temp_base)
    keep = {token for token in (keep_run_tokens or set()) if token}
    if not temp_root.exists():
        return TempCleanupSummary(
            scanned=0,
            deleted=0,
            active=0,
            failed=0,
            bytes_before=0,
            bytes_after=0,
        )
    paths = [
        path
        for path in sorted(temp_root.iterdir())
        if path.name not in keep and path.is_dir()
    ]
    summary = cleanup_temp_paths(paths, dry_run=dry_run)
    if not dry_run:
        _remove_empty_parent_dirs(
            temp_root,
            stop_before=repo_pytest_runtime_root(repo_root, temp_base=temp_base).parent,
        )
    return summary


def cleanup_temp_paths(
    paths: Iterable[Path],
    *,
    dry_run: bool = False,
    scan_fn: Callable[[Path], TempPathScanResult] | None = None,
) -> TempCleanupSummary:
    scanner = scan_fn or scan_temp_path
    deleted_paths: list[Path] = []
    active_paths: list[Path] = []
    failed_paths: list[str] = []
    active_processes: list[TempRootProcess] = []
    scanned = deleted = active = failed = 0
    bytes_before = bytes_after = 0

    for candidate in paths:
        path = Path(candidate)
        if not path.exists():
            continue
        scanned += 1
        scan = scanner(path)
        bytes_before += scan.bytes
        if scan.scan_error is not None:
            failed += 1
            bytes_after += scan.bytes
            failed_paths.append(f"{path}: {scan.scan_error}")
            continue
        if scan.active_processes:
            active += 1
            bytes_after += scan.bytes
            active_paths.append(path)
            active_processes.extend(scan.active_processes)
            continue
        if dry_run:
            deleted += 1
            deleted_paths.append(path)
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            failed += 1
            remaining_bytes = _tree_size_bytes(path)
            bytes_after += remaining_bytes
            failed_paths.append(f"{path}: {exc}")
            continue
        deleted += 1
        deleted_paths.append(path)
        _remove_empty_parent_dirs(path.parent, stop_before=path.anchor)

    return TempCleanupSummary(
        scanned=scanned,
        deleted=deleted,
        active=active,
        failed=failed,
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        deleted_paths=tuple(deleted_paths),
        active_paths=tuple(active_paths),
        failed_paths=tuple(failed_paths),
        active_processes=tuple(_dedupe_processes(active_processes)),
    )


def scan_temp_path(
    path: Path, *, lsof_timeout_seconds: float = _DEFAULT_LSOF_TIMEOUT_SECONDS
) -> TempPathScanResult:
    root = Path(path)
    bytes_used = _tree_size_bytes(root)
    if not root.exists():
        return TempPathScanResult(path=root, bytes=0)
    try:
        active_processes = find_processes_using_path(
            root, timeout_seconds=lsof_timeout_seconds
        )
    except RuntimeError as exc:
        return TempPathScanResult(path=root, bytes=bytes_used, scan_error=str(exc))
    return TempPathScanResult(
        path=root,
        bytes=bytes_used,
        active_processes=active_processes,
    )


def find_processes_using_path(
    root: Path, *, timeout_seconds: float = _DEFAULT_LSOF_TIMEOUT_SECONDS
) -> tuple[TempRootProcess, ...]:
    path = Path(root)
    if not path.exists():
        return ()
    try:
        result = subprocess.run(
            ["lsof", "-n", "-P", "-F0pcfn", "+D", str(path)],
            capture_output=True,
            text=False,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return ()
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"lsof timed out after {timeout_seconds:.1f}s") from exc
    except OSError as exc:
        raise RuntimeError(f"lsof failed: {exc}") from exc

    if result.returncode not in (0, 1):
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        detail = stderr or f"exit code {result.returncode}"
        raise RuntimeError(f"lsof failed: {detail}")

    if result.returncode == 1 or not result.stdout:
        return ()

    current_pid: int | None = None
    current_command = ""
    current_fd: str | None = None
    processes: list[TempRootProcess] = []
    for field in result.stdout.split(b"\0"):
        if not field:
            continue
        code = chr(field[0])
        value = field[1:].decode("utf-8", errors="replace")
        if code == "p":
            current_pid = int(value)
            current_command = ""
            current_fd = None
        elif code == "c":
            current_command = value
        elif code == "f":
            current_fd = value
        elif code == "n" and current_pid is not None:
            processes.append(
                TempRootProcess(
                    pid=current_pid,
                    command=current_command or "?",
                    descriptor=current_fd,
                    path=value,
                )
            )
    return tuple(_dedupe_processes(processes))


def system_temp_root() -> Path:
    original_tempdir = tempfile.tempdir
    original_env = {key: os.environ.get(key) for key in _TEMP_ENV_KEYS}
    try:
        for key in _TEMP_ENV_KEYS:
            os.environ.pop(key, None)
        tempfile.tempdir = None
        return Path(tempfile.gettempdir()).resolve(strict=False)
    finally:
        tempfile.tempdir = original_tempdir
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _tree_size_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    if root.is_file():
        try:
            return root.stat().st_size
        except OSError:
            return 0
    total = 0
    for candidate in root.rglob("*"):
        try:
            if candidate.is_file():
                total += candidate.stat().st_size
        except OSError:
            continue
    return total


def _remove_empty_parent_dirs(start: Path, *, stop_before: str | Path) -> None:
    stop = Path(stop_before)
    current = Path(start)
    while True:
        if current == stop or str(current) == str(stop):
            return
        try:
            current.rmdir()
        except OSError:
            return
        parent = current.parent
        if parent == current:
            return
        current = parent


def _dedupe_processes(
    processes: Iterable[TempRootProcess],
) -> list[TempRootProcess]:
    seen: set[tuple[int, str, str | None]] = set()
    deduped: list[TempRootProcess] = []
    for process in sorted(
        processes,
        key=lambda item: (
            item.pid,
            item.command,
            item.path or "",
            item.descriptor or "",
        ),
    ):
        key = (process.pid, process.command, process.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(process)
    return deduped
