from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


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
    for path in (
        inbox_dir(repo_root),
        outbox_dir(repo_root),
        outbox_pending_dir(repo_root),
        outbox_sent_dir(repo_root),
    ):
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
        if not folder.exists():
            continue
        try:
            for path in folder.iterdir():
                try:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                    collected.append(
                        FileBoxEntry(
                            name=path.name,
                            box=box,
                            size=stat.st_size if stat else None,
                            modified_at=_format_mtime(stat.st_mtime) if stat else None,
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


def _target_path(repo_root: Path, box: str, filename: str) -> Path:
    """Return a resolved path within the FileBox, rejecting traversal attempts."""

    safe_name = sanitize_filename(filename)
    target_dir = _box_dir(repo_root, box)
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


def save_file(repo_root: Path, box: str, filename: str, data: bytes) -> Path:
    if box not in BOXES:
        raise ValueError("Invalid box")
    ensure_structure(repo_root)
    path = _target_path(repo_root, box, filename)
    path.write_bytes(data)
    return path


def resolve_file(repo_root: Path, box: str, filename: str) -> FileBoxEntry | None:
    if box not in BOXES:
        return None
    path = _target_path(repo_root, box, filename)
    if not path.exists() or not path.is_file():
        return None
    stat = path.stat()
    return FileBoxEntry(
        name=path.name,
        box=box,
        size=stat.st_size,
        modified_at=_format_mtime(stat.st_mtime),
        source="filebox",
        path=path,
    )


def delete_file(repo_root: Path, box: str, filename: str) -> bool:
    if box not in BOXES:
        return False
    path = _target_path(repo_root, box, filename)
    if not path.exists() or not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def list_regular_files(folder: Path) -> List[Path]:
    if not folder.exists():
        return []
    files: List[Path] = []
    try:
        for path in folder.iterdir():
            try:
                if path.is_file():
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
    if not folder.exists():
        return 0
    deleted = 0
    try:
        for path in folder.iterdir():
            try:
                if path.is_file():
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
    "outbox_dir",
    "outbox_pending_dir",
    "outbox_sent_dir",
    "resolve_file",
    "sanitize_filename",
    "save_file",
]
