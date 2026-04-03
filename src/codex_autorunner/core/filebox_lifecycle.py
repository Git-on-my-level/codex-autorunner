from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .filebox import FileBoxEntry, filebox_root, inbox_dir, sanitize_filename

LIFECYCLE_BOXES = ("consumed", "dismissed")


def consumed_dir(repo_root: Path) -> Path:
    return filebox_root(repo_root) / "consumed"


def dismissed_dir(repo_root: Path) -> Path:
    return filebox_root(repo_root) / "dismissed"


def ensure_lifecycle_structure(repo_root: Path) -> None:
    for path in (
        consumed_dir(repo_root),
        dismissed_dir(repo_root),
    ):
        path.mkdir(parents=True, exist_ok=True)


def consume_inbox_file(repo_root: Path, filename: str) -> FileBoxEntry:
    return _move_from_inbox(repo_root, filename, target_box="consumed")


def dismiss_inbox_file(repo_root: Path, filename: str) -> FileBoxEntry:
    return _move_from_inbox(repo_root, filename, target_box="dismissed")


def list_consumed_files(repo_root: Path) -> list[FileBoxEntry]:
    ensure_lifecycle_structure(repo_root)
    entries: list[FileBoxEntry] = []
    for box in LIFECYCLE_BOXES:
        folder = _lifecycle_dir(repo_root, box)
        entries.extend(_list_box_entries(folder, box=box))
    return sorted(entries, key=lambda entry: (entry.box, entry.name))


def unconsume_inbox_file(repo_root: Path, filename: str) -> FileBoxEntry:
    ensure_lifecycle_structure(repo_root)
    safe_name = sanitize_filename(filename)
    existing_sources = _find_archived_sources(repo_root, safe_name)
    if not existing_sources:
        raise FileNotFoundError(f"Inbox archive file not found: {safe_name}")

    target = _resolve_child_path(inbox_dir(repo_root), safe_name, create_dir=True)
    if target.exists():
        raise FileExistsError(f"Inbox already contains {safe_name}")

    existing_sources[0].rename(target)
    return _build_entry(target, box="inbox")


def _move_from_inbox(
    repo_root: Path, filename: str, *, target_box: str
) -> FileBoxEntry:
    ensure_lifecycle_structure(repo_root)
    safe_name = sanitize_filename(filename)
    source = _resolve_child_path(inbox_dir(repo_root), safe_name)
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Inbox file not found: {safe_name}")

    target = _allocate_archive_target(_lifecycle_dir(repo_root, target_box), safe_name)

    source.rename(target)
    return _build_entry(target, box=target_box)


def _lifecycle_dir(repo_root: Path, box: str) -> Path:
    if box == "consumed":
        return consumed_dir(repo_root)
    if box == "dismissed":
        return dismissed_dir(repo_root)
    raise ValueError("Invalid lifecycle box")


def _list_box_entries(folder: Path, *, box: str) -> list[FileBoxEntry]:
    if not folder.exists():
        return []
    entries: list[FileBoxEntry] = []
    try:
        iterator = folder.iterdir()
    except OSError:
        return []
    for path in iterator:
        try:
            if not path.is_file():
                continue
            entries.append(_build_entry(path, box=box))
        except OSError:
            continue
    return sorted(entries, key=lambda entry: entry.name)


def _find_archived_sources(repo_root: Path, filename: str) -> list[Path]:
    sources: list[Path] = []
    for box in LIFECYCLE_BOXES:
        candidate = _resolve_child_path(_lifecycle_dir(repo_root, box), filename)
        try:
            if candidate.exists() and candidate.is_file():
                sources.append(candidate)
        except OSError:
            continue
    return sorted(sources, key=_path_mtime, reverse=True)


def _allocate_archive_target(root: Path, filename: str) -> Path:
    target = _resolve_child_path(root, filename, create_dir=True)
    if not target.exists():
        return target

    stem, suffix = _split_name(filename)
    index = 2
    while True:
        candidate = _resolve_child_path(root, f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _split_name(filename: str) -> tuple[str, str]:
    path = Path(filename)
    suffix = "".join(path.suffixes)
    if suffix and filename != suffix:
        return filename[: -len(suffix)], suffix
    return filename, ""


def _build_entry(path: Path, *, box: str) -> FileBoxEntry:
    stat = path.stat()
    return FileBoxEntry(
        name=path.name,
        box=box,
        size=stat.st_size,
        modified_at=_format_mtime(stat.st_mtime),
        source="filebox",
        path=path,
    )


def _resolve_child_path(root: Path, filename: str, *, create_dir: bool = False) -> Path:
    safe_name = sanitize_filename(filename)
    if create_dir:
        root.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve()
    candidate = (resolved_root / safe_name).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Invalid filename") from exc
    if candidate.parent != resolved_root:
        raise ValueError("Invalid filename")
    return candidate


def _path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _format_mtime(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


__all__ = [
    "LIFECYCLE_BOXES",
    "consume_inbox_file",
    "consumed_dir",
    "dismiss_inbox_file",
    "dismissed_dir",
    "ensure_lifecycle_structure",
    "list_consumed_files",
    "unconsume_inbox_file",
]
