from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


@dataclass(frozen=True)
class FileBoxEntry:
    name: str
    box: str  # "inbox" | "outbox"
    size: int | None
    modified_at: str | None
    source: str  # "filebox", "pma", "telegram"
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


def _legacy_paths(repo_root: Path, box: str) -> List[Tuple[str, Path]]:
    root = Path(repo_root)
    paths: List[Tuple[str, Path]] = []
    if box not in BOXES:
        return paths

    # PMA legacy paths
    pma_dir = root / ".codex-autorunner" / "pma" / box
    paths.append(("pma", pma_dir))

    # Telegram legacy paths (topic-scoped). We merge inbox and outbox/pending|sent.
    telegram_root = root / ".codex-autorunner" / "uploads" / "telegram-files"
    if telegram_root.exists():
        for topic in telegram_root.iterdir():
            if not topic.is_dir():
                continue
            if box == "inbox":
                paths.append(("telegram", topic / "inbox"))
            elif box == "outbox":
                paths.append(("telegram", topic / "outbox" / "pending"))
                paths.append(("telegram", topic / "outbox" / "sent"))
    return paths


def _gather_files(entries: Iterable[Tuple[str, Path]], box: str) -> List[FileBoxEntry]:
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


def _dedupe(entries: List[FileBoxEntry]) -> List[FileBoxEntry]:
    # Prefer primary filebox entries over legacy duplicates.
    deduped: Dict[Tuple[str, str], FileBoxEntry] = {}
    for entry in entries:
        key = (entry.box, entry.name)
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = entry
            continue
        if existing.source != "filebox" and entry.source == "filebox":
            deduped[key] = entry
    return list(deduped.values())


def _format_mtime(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


def list_filebox(
    repo_root: Path, *, include_legacy: bool = True
) -> Dict[str, List[FileBoxEntry]]:
    ensure_structure(repo_root)
    results: Dict[str, List[FileBoxEntry]] = {}
    for box in BOXES:
        primaries = _gather_files([("filebox", _box_dir(repo_root, box))], box)
        legacy = (
            _gather_files(_legacy_paths(repo_root, box), box) if include_legacy else []
        )
        results[box] = _dedupe(primaries + legacy)
    return results


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
    safe_name = sanitize_filename(filename)
    paths: List[Tuple[str, Path]] = [("filebox", _box_dir(repo_root, box))]
    paths.extend(_legacy_paths(repo_root, box))
    candidates = _gather_files(paths, box)
    for entry in candidates:
        if entry.name == safe_name:
            return entry
    return None


def delete_file(repo_root: Path, box: str, filename: str) -> bool:
    entry = resolve_file(repo_root, box, filename)
    if entry is None:
        return False
    try:
        entry.path.unlink()
        return True
    except OSError:
        return False


def migrate_legacy(repo_root: Path) -> int:
    """
    Opportunistically copy legacy PMA/Telegram files into the shared FileBox.
    Returns the number of files copied.
    """
    copied = 0
    ensure_structure(repo_root)
    for box in BOXES:
        target_dir = _box_dir(repo_root, box)
        target_dir.mkdir(parents=True, exist_ok=True)
        for _source, folder in _legacy_paths(repo_root, box):
            if not folder.exists():
                continue
            for path in folder.iterdir():
                try:
                    if not path.is_file():
                        continue
                    dest = target_dir / path.name
                    if dest.exists():
                        continue
                    shutil.copy2(path, dest)
                    copied += 1
                except OSError:
                    continue
    return copied


__all__ = [
    "BOXES",
    "FileBoxEntry",
    "delete_file",
    "filebox_root",
    "inbox_dir",
    "list_filebox",
    "migrate_legacy",
    "outbox_dir",
    "outbox_pending_dir",
    "outbox_sent_dir",
    "resolve_file",
    "sanitize_filename",
    "save_file",
]
