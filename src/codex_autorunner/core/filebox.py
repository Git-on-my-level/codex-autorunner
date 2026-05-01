from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, List


@dataclass(frozen=True)
class FileBoxEntry:
    name: str
    box: str
    size: int | None
    modified_at: str | None
    source: str
    path: Path


BOXES = ("inbox", "outbox")


def filebox_root(repo_root: Path) -> Path:
    return Path(repo_root) / ".codex-autorunner" / "filebox"


def inbox_dir(repo_root: Path) -> Path:
    return filebox_root(repo_root) / "inbox"


def outbox_dir(repo_root: Path) -> Path:
    return filebox_root(repo_root) / "outbox"


def outbox_pending_dir(repo_root: Path) -> Path:
    # Preserves Telegram pending semantics while keeping everything under the shared FileBox.
    return outbox_dir(repo_root) / "pending"


def outbox_sent_dir(repo_root: Path) -> Path:
    return outbox_dir(repo_root) / "sent"


def ensure_structure(repo_root: Path) -> None:
    _ensure_filebox_ancestors(repo_root)
    for path in (
        inbox_dir(repo_root),
        outbox_dir(repo_root),
        outbox_pending_dir(repo_root),
        outbox_sent_dir(repo_root),
    ):
        if path.is_symlink():
            raise ValueError(f"FileBox path must not be a symlink: {path}")
        path.mkdir(parents=True, exist_ok=True)


def empty_listing() -> dict[str, list[Any]]:
    return {box: [] for box in BOXES}


def sanitize_filename(name: str) -> str:
    base = Path(name or "").name
    if not base or base in {".", ".."}:
        raise ValueError("Missing filename")
    # Reject any path separators or traversal segments up-front.
    if name != base or "/" in name or "\\" in name:
        raise ValueError("Invalid filename")
    parts = Path(base).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Invalid filename")
    return base


def _gather_files(entries: Iterable[tuple[str, Path]], box: str) -> List[FileBoxEntry]:
    collected: List[FileBoxEntry] = []
    for source, folder in entries:
        if folder.is_symlink() or not folder.exists():
            continue
        try:
            for path in folder.iterdir():
                try:
                    stat_result = path.lstat()
                    if not _is_single_regular_file(stat_result):
                        continue
                    collected.append(
                        FileBoxEntry(
                            name=path.name,
                            box=box,
                            size=stat_result.st_size,
                            modified_at=_format_mtime(stat_result.st_mtime),
                            source=source,
                            path=path,
                        )
                    )
                except OSError:
                    continue
        except OSError:
            continue
    return collected


def _format_mtime(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def list_filebox(repo_root: Path) -> Dict[str, List[FileBoxEntry]]:
    ensure_structure(repo_root)
    return {
        box: _gather_files([("filebox", _box_dir(repo_root, box))], box)
        for box in BOXES
    }


def _box_dir(repo_root: Path, box: str) -> Path:
    if box == "inbox":
        return inbox_dir(repo_root)
    if box == "outbox":
        return outbox_dir(repo_root)
    raise ValueError("Invalid filebox")


def _ensure_filebox_ancestors(repo_root: Path) -> None:
    root_path = Path(repo_root)
    if root_path.is_symlink():
        raise ValueError(f"FileBox path must not be a symlink: {root_path}")
    control_root = Path(repo_root) / ".codex-autorunner"
    root = filebox_root(repo_root)
    for path in (control_root, root):
        if path.is_symlink():
            raise ValueError(f"FileBox path must not be a symlink: {path}")


def _open_box_dir(repo_root: Path, box: str) -> tuple[int, Path]:
    _ensure_filebox_ancestors(repo_root)
    target_dir = _box_dir(repo_root, box)
    if target_dir.is_symlink():
        raise ValueError("Invalid filebox")
    target_dir.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        dir_fd = os.open(target_dir, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("Invalid filebox") from exc
        raise
    return dir_fd, target_dir.resolve()


def _is_single_regular_file(stat_result: os.stat_result) -> bool:
    return stat.S_ISREG(stat_result.st_mode) and stat_result.st_nlink == 1


def _target_path(repo_root: Path, box: str, filename: str) -> Path:
    """Return a resolved path within the FileBox, rejecting traversal attempts."""

    safe_name = sanitize_filename(filename)
    _ensure_filebox_ancestors(repo_root)
    target_dir = _box_dir(repo_root, box)
    if target_dir.is_symlink():
        raise ValueError("Invalid filebox")
    target_dir.mkdir(parents=True, exist_ok=True)

    root = target_dir.resolve()
    candidate = (root / safe_name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Invalid filename") from exc
    if candidate.parent != root:
        # Disallow sneaky path tricks that resolve inside nested folders.
        raise ValueError("Invalid filename")
    return candidate


def _unresolved_target_path(repo_root: Path, box: str, filename: str) -> Path:
    safe_name = sanitize_filename(filename)
    _ensure_filebox_ancestors(repo_root)
    target_dir = _box_dir(repo_root, box)
    if target_dir.is_symlink():
        raise ValueError("Invalid filebox")
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir.resolve() / safe_name


def _existing_regular_entry(repo_root: Path, box: str, filename: str) -> Path | None:
    safe_name = sanitize_filename(filename)
    _ensure_filebox_ancestors(repo_root)
    target_dir = _box_dir(repo_root, box)
    if target_dir.is_symlink():
        raise ValueError("Invalid filebox")
    target_dir.mkdir(parents=True, exist_ok=True)
    root = target_dir.resolve()
    try:
        for candidate in root.iterdir():
            try:
                if candidate.name != safe_name:
                    continue
                file_stat = candidate.lstat()
                if not _is_single_regular_file(file_stat):
                    return None
                if candidate.parent != root:
                    return None
                return candidate
            except OSError:
                return None
    except OSError:
        return None
    return None


def _entry_from_stat(path: Path, box: str, stat_result: os.stat_result) -> FileBoxEntry:
    return FileBoxEntry(
        name=path.name,
        box=box,
        size=stat_result.st_size,
        modified_at=_format_mtime(stat_result.st_mtime),
        source="filebox",
        path=path,
    )


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def read_file(
    repo_root: Path, box: str, filename: str
) -> tuple[FileBoxEntry, bytes] | None:
    if box not in BOXES:
        return None
    safe_name = sanitize_filename(filename)
    dir_fd, root = _open_box_dir(repo_root, box)
    file_fd: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_fd = os.open(safe_name, flags, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                return None
            raise
        file_stat = os.fstat(file_fd)
        if not _is_single_regular_file(file_stat):
            return None
        entry = _entry_from_stat(root / safe_name, box, file_stat)
        return entry, _read_all(file_fd)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(dir_fd)


def open_file(
    repo_root: Path, box: str, filename: str
) -> tuple[FileBoxEntry, BinaryIO] | None:
    if box not in BOXES:
        return None
    safe_name = sanitize_filename(filename)
    dir_fd, root = _open_box_dir(repo_root, box)
    file_fd: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_fd = os.open(safe_name, flags, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                return None
            raise
        file_stat = os.fstat(file_fd)
        if not _is_single_regular_file(file_stat):
            return None
        entry = _entry_from_stat(root / safe_name, box, file_stat)
        handle = os.fdopen(file_fd, "rb")
        file_fd = None
        return entry, handle
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(dir_fd)


def save_file(repo_root: Path, box: str, filename: str, data: bytes) -> Path:
    if box not in BOXES:
        raise ValueError("Invalid box")
    ensure_structure(repo_root)
    safe_name = sanitize_filename(filename)
    dir_fd, root = _open_box_dir(repo_root, box)
    file_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_fd = os.open(safe_name, flags, 0o666, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError("Invalid filename") from exc
            raise
        file_stat = os.fstat(file_fd)
        if not _is_single_regular_file(file_stat):
            raise ValueError("Invalid filename")
        os.ftruncate(file_fd, 0)
        view = memoryview(data)
        while view:
            written = os.write(file_fd, view)
            view = view[written:]
        return root / safe_name
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(dir_fd)


def resolve_file(repo_root: Path, box: str, filename: str) -> FileBoxEntry | None:
    if box not in BOXES:
        return None
    path = _existing_regular_entry(repo_root, box, filename)
    if path is None:
        return None
    stat_result = path.lstat()
    return _entry_from_stat(path, box, stat_result)


def delete_file(repo_root: Path, box: str, filename: str) -> bool:
    if box not in BOXES:
        return False
    path = _existing_regular_entry(repo_root, box, filename)
    if path is None:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        raise
    return True


def list_regular_files(folder: Path) -> List[Path]:
    if folder.is_symlink() or not folder.exists():
        return []
    files: List[Path] = []
    try:
        for path in folder.iterdir():
            try:
                stat_result = path.lstat()
                if _is_single_regular_file(stat_result):
                    files.append(path)
            except OSError:
                continue
    except OSError:
        return []

    def _mtime(entry: Path) -> float:
        try:
            return entry.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(files, key=_mtime, reverse=True)


def delete_regular_files(folder: Path) -> int:
    if folder.is_symlink() or not folder.exists():
        return 0
    deleted = 0
    try:
        for path in folder.iterdir():
            try:
                stat_result = path.lstat()
                if _is_single_regular_file(stat_result):
                    path.unlink()
                    deleted += 1
            except OSError:
                continue
    except OSError:
        return deleted
    return deleted


__all__ = [
    "BOXES",
    "FileBoxEntry",
    "delete_regular_files",
    "delete_file",
    "empty_listing",
    "filebox_root",
    "inbox_dir",
    "list_regular_files",
    "list_filebox",
    "open_file",
    "outbox_dir",
    "outbox_pending_dir",
    "outbox_sent_dir",
    "read_file",
    "resolve_file",
    "sanitize_filename",
    "save_file",
]
