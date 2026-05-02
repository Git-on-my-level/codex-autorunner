from __future__ import annotations

import errno
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Hashable, Iterable, TypeVar

_PYTEST_RUNTIME_TEMP_SUBDIR = "t"
_DEFAULT_LSOF_TIMEOUT_SECONDS = 10.0
_TEMP_ENV_KEYS = ("TMPDIR", "TMP", "TEMP")
_CAR_TEMP_DIR_PREFIXES = (
    "car-cached-",
    "car-hub-profile-",
    "idle-cpu-soak-",
    "car-nocache-",
    "car-patch-",
)
_RMTREE_RETRY_ERRNOS = {
    errno.ENOTEMPTY,
    errno.EBUSY,
}
_RMTREE_RETRY_ATTEMPTS = 8
_RMTREE_RETRY_SLEEP_SECONDS = 0.1
_T = TypeVar("_T", bound=Hashable)


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


def candidate_system_temp_roots() -> tuple[Path, ...]:
    roots: dict[Path, Path] = {}
    for candidate in (system_temp_root(),):
        normalized = Path(candidate).expanduser().resolve(strict=False)
        roots.setdefault(normalized, normalized)
        for alias in _system_temp_root_aliases(normalized):
            resolved_alias = alias.expanduser().resolve(strict=False)
            roots.setdefault(resolved_alias, resolved_alias)
    return tuple(sorted(roots.values(), key=lambda path: str(path)))


def existing_repo_pytest_runtime_roots(
    repo_root: Path,
    *,
    temp_base: Path | None = None,
) -> tuple[Path, ...]:
    runtime_root = repo_pytest_runtime_root(repo_root, temp_base=temp_base)
    if temp_base is not None:
        return (runtime_root,) if runtime_root.exists() else ()

    roots: dict[Path, Path] = {}
    for base_root in candidate_system_temp_roots():
        candidate = repo_pytest_runtime_root(repo_root, temp_base=base_root)
        if not candidate.exists():
            continue
        normalized = candidate.resolve(strict=False)
        roots.setdefault(normalized, candidate)
    return tuple(sorted(roots.values(), key=lambda path: str(path)))


def cleanup_repo_pytest_temp_runs(
    repo_root: Path,
    *,
    keep_run_tokens: set[str] | None = None,
    dry_run: bool = False,
    temp_base: Path | None = None,
    min_age_seconds: float = 0.0,
) -> TempCleanupSummary:
    runtime_roots = existing_repo_pytest_runtime_roots(repo_root, temp_base=temp_base)
    if temp_base is not None and not runtime_roots:
        temp_root = repo_pytest_temp_root(repo_root, temp_base=temp_base)
        runtime_roots = (temp_root.parent,) if temp_root.exists() else ()
    if not runtime_roots:
        return TempCleanupSummary(
            scanned=0,
            deleted=0,
            active=0,
            failed=0,
            bytes_before=0,
            bytes_after=0,
        )
    keep = {token for token in (keep_run_tokens or set()) if token}
    cutoff = time.time() - max(0.0, float(min_age_seconds))
    summaries: list[TempCleanupSummary] = []
    for runtime_root in runtime_roots:
        temp_root = runtime_root / _PYTEST_RUNTIME_TEMP_SUBDIR
        if not temp_root.exists():
            continue
        paths = []
        for path in sorted(temp_root.iterdir()):
            if path.name in keep or not path.is_dir():
                continue
            if min_age_seconds > 0.0:
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                except OSError:
                    continue
            paths.append(path)
        summary = cleanup_temp_paths(paths, dry_run=dry_run)
        summaries.append(summary)
        if not dry_run:
            _remove_empty_parent_dirs(temp_root, stop_before=runtime_root.parent)
    return _combine_cleanup_summaries(summaries)


def discover_repo_temp_paths(
    repo_root: Path,
    *,
    temp_base: Path | None = None,
) -> tuple[Path, ...]:
    candidate_roots: tuple[Path, ...]
    if temp_base is not None:
        candidate_roots = (Path(temp_base),)
    else:
        candidate_roots = candidate_system_temp_roots()
    discovered: dict[Path, Path] = {}
    for base_root in candidate_roots:
        root = Path(base_root).expanduser().resolve(strict=False)
        if not root.exists() or not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            if _is_repo_owned_temp_path(child):
                normalized = child.resolve(strict=False)
                discovered.setdefault(normalized, child)
    return tuple(sorted(discovered.values(), key=lambda path: str(path)))


def cleanup_repo_temp_paths(
    repo_root: Path,
    *,
    dry_run: bool = False,
    temp_base: Path | None = None,
    min_age_seconds: float = 0.0,
) -> TempCleanupSummary:
    paths = discover_repo_temp_paths(repo_root, temp_base=temp_base)
    if not paths:
        return TempCleanupSummary(
            scanned=0,
            deleted=0,
            active=0,
            failed=0,
            bytes_before=0,
            bytes_after=0,
        )
    if min_age_seconds > 0.0:
        cutoff = time.time() - max(0.0, float(min_age_seconds))
        filtered: list[Path] = []
        for path in paths:
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            filtered.append(path)
        paths = tuple(filtered)
    if not paths:
        return TempCleanupSummary(
            scanned=0,
            deleted=0,
            active=0,
            failed=0,
            bytes_before=0,
            bytes_after=0,
        )
    return cleanup_temp_paths(paths, dry_run=dry_run)


def cleanup_repo_managed_temp_paths(
    repo_root: Path,
    *,
    keep_run_tokens: set[str] | None = None,
    dry_run: bool = False,
    temp_base: Path | None = None,
    min_age_seconds: float = 0.0,
) -> TempCleanupSummary:
    return _combine_cleanup_summaries(
        (
            cleanup_repo_pytest_temp_runs(
                repo_root,
                keep_run_tokens=keep_run_tokens,
                dry_run=dry_run,
                temp_base=temp_base,
                min_age_seconds=min_age_seconds,
            ),
            cleanup_repo_temp_paths(
                repo_root,
                dry_run=dry_run,
                temp_base=temp_base,
                min_age_seconds=min_age_seconds,
            ),
        )
    )


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
            _rmtree_with_retries(path)
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


def _rmtree_with_retries(path: Path) -> None:
    attempts = _RMTREE_RETRY_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            if exc.errno not in _RMTREE_RETRY_ERRNOS or attempt >= attempts:
                raise
            time.sleep(_RMTREE_RETRY_SLEEP_SECONDS * attempt)


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


def _system_temp_root_aliases(root: Path) -> tuple[Path, ...]:
    aliases: dict[Path, Path] = {}
    text = str(root)
    if text == "/tmp":
        aliases[Path("/private/tmp")] = Path("/private/tmp")
    elif text == "/private/tmp":
        aliases[Path("/tmp")] = Path("/tmp")
    if text.startswith("/var/"):
        aliases[Path("/private").joinpath(text.lstrip("/"))] = Path(
            "/private"
        ).joinpath(text.lstrip("/"))
    elif text.startswith("/private/var/"):
        aliases[Path("/" + text.removeprefix("/private/"))] = Path(
            "/" + text.removeprefix("/private/")
        )
    return tuple(sorted(aliases.values(), key=lambda path: str(path)))


def _combine_cleanup_summaries(
    summaries: Iterable[TempCleanupSummary],
) -> TempCleanupSummary:
    collected = list(summaries)
    deleted_paths = _dedupe_ordered(
        path for summary in collected for path in summary.deleted_paths
    )
    active_paths = _dedupe_ordered(
        path for summary in collected for path in summary.active_paths
    )
    failed_paths = _dedupe_ordered(
        detail for summary in collected for detail in summary.failed_paths
    )
    active_processes = _dedupe_processes(
        process for summary in collected for process in summary.active_processes
    )
    return TempCleanupSummary(
        scanned=sum(summary.scanned for summary in collected),
        deleted=sum(summary.deleted for summary in collected),
        active=sum(summary.active for summary in collected),
        failed=sum(summary.failed for summary in collected),
        bytes_before=sum(summary.bytes_before for summary in collected),
        bytes_after=sum(summary.bytes_after for summary in collected),
        deleted_paths=tuple(deleted_paths),
        active_paths=tuple(active_paths),
        failed_paths=tuple(failed_paths),
        active_processes=tuple(active_processes),
    )


def _is_repo_owned_temp_path(path: Path) -> bool:
    name = path.name
    if any(name.startswith(prefix) for prefix in _CAR_TEMP_DIR_PREFIXES):
        return True
    if not name.startswith("tmp."):
        return False
    return _looks_like_repo_temp_workspace(path)


def _looks_like_repo_temp_workspace(path: Path) -> bool:
    try:
        names = {child.name for child in path.iterdir()}
    except OSError:
        return False
    has_flows_db = any(
        name in names for name in ("flows.db", "flows.db-wal", "flows.db-shm")
    )
    has_repo_state = ".codex-autorunner" in names
    repo_dir = path / "repo"
    has_repo_checkout = repo_dir.is_dir() and (
        (repo_dir / ".git").exists() or (repo_dir / ".codex-autorunner").exists()
    )
    return (has_repo_checkout and has_flows_db) or (
        has_repo_checkout and has_repo_state
    )


def _tree_size_bytes(root: Path) -> int:
    try:
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
    except OSError:
        return 0


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


def _dedupe_ordered(items: Iterable[_T]) -> list[_T]:
    seen: set[_T] = set()
    deduped: list[_T] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped
