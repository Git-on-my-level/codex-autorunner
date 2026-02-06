from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from ..core import drafts as draft_utils
from ..core.logging_utils import log_event

ContextspaceDocKind = Literal["active_context", "decisions", "spec"]
CONTEXTSPACE_DOC_KINDS: tuple[ContextspaceDocKind, ...] = (
    "active_context",
    "decisions",
    "spec",
)

logger = logging.getLogger(__name__)


@dataclass
class ContextspaceFile:
    name: str
    path: str  # path relative to the contextspace directory (POSIX)
    is_pinned: bool = False
    modified_at: str | None = None


def _normalize_kind(kind: str) -> ContextspaceDocKind:
    key = (kind or "").strip().lower()
    if key not in CONTEXTSPACE_DOC_KINDS:
        raise ValueError("invalid contextspace doc kind")
    return cast(ContextspaceDocKind, key)


def contextspace_dir(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "contextspace"


PINNED_DOC_FILENAMES = {f"{kind}.md" for kind in CONTEXTSPACE_DOC_KINDS}


@dataclass
class ContextspaceNode:
    name: str
    path: str  # relative to contextspace dir
    type: Literal["file", "folder"]
    is_pinned: bool = False
    modified_at: str | None = None
    size: int | None = None  # files only
    children: list["ContextspaceNode"] | None = None  # folders only


def normalize_contextspace_rel_path(repo_root: Path, rel_path: str) -> tuple[Path, str]:
    """Normalize a user-supplied contextspace path and ensure it stays in-tree.

    We accept POSIX-style relative paths only, then resolve the full path and
    verify the result is still under the contextspace directory. This guards
    against ".." traversal and symlink escapes that CodeQL flagged.
    """

    base = contextspace_dir(repo_root).resolve(strict=False)
    base_real = base.resolve(strict=False)
    cleaned = (rel_path or "").strip()
    if not cleaned:
        raise ValueError("invalid contextspace file path")

    relative = PurePosixPath(cleaned)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid contextspace file path")

    candidate = (base / relative).resolve(strict=False)
    try:
        rel_posix = candidate.relative_to(base_real).as_posix()
    except ValueError:
        raise ValueError("invalid contextspace file path") from None

    return candidate, rel_posix


def sanitize_contextspace_filename(filename: str) -> str:
    """Return a safe filename for contextspace uploads.

    We strip any directory components, collapse whitespace, and guard against
    empty names. Caller is responsible for applying any per-contextspace policy
    (e.g., overwrite vs. reject).
    """

    cleaned = (filename or "").strip()
    # Drop any path fragments that may be embedded in the upload
    base = PurePosixPath(cleaned).name
    # Remove remaining separators/backslashes that PurePosixPath.name could keep
    base = base.replace("/", "").replace("\\", "")
    if base in {".", ".."}:
        base = ""
    # Collapse whitespace to single spaces to keep names readable
    base = " ".join(base.split())
    if not base:
        return "upload"
    return base


def contextspace_doc_path(repo_root: Path, kind: str) -> Path:
    key = _normalize_kind(kind)
    return contextspace_dir(repo_root) / f"{key}.md"


def read_contextspace_file(
    repo_root: Path, rel_path: str
) -> str:  # codeql[py/path-injection]
    path, _ = normalize_contextspace_rel_path(repo_root, rel_path)
    if (
        path.is_dir()
    ):  # codeql[py/path-injection] validated by normalize_contextspace_rel_path
        raise ValueError("path points to a directory")
    if (
        not path.exists()
    ):  # codeql[py/path-injection] validated by normalize_contextspace_rel_path
        return ""
    try:
        return path.read_text(  # codeql[py/path-injection] validated by normalize_contextspace_rel_path
            encoding="utf-8"
        )
    except UnicodeDecodeError as exc:
        raise ValueError(
            "file is not UTF-8 text; download it to view binary data"
        ) from exc


def write_contextspace_file(  # codeql[py/path-injection]
    repo_root: Path, rel_path: str, content: str
) -> str:
    path, rel_posix = normalize_contextspace_rel_path(repo_root, rel_path)
    if (
        path.exists() and path.is_dir()
    ):  # codeql[py/path-injection] validated by normalize_contextspace_rel_path
        raise ValueError("path points to a directory")
    path.parent.mkdir(
        parents=True, exist_ok=True
    )  # codeql[py/path-injection] validated by normalize_contextspace_rel_path
    path.write_text(
        content or "", encoding="utf-8"
    )  # codeql[py/path-injection] validated by normalize_contextspace_rel_path
    rel = path.relative_to(repo_root).as_posix()
    state_key = f"contextspace_{rel_posix.replace('/', '_')}"
    try:
        draft_utils.invalidate_drafts_for_path(repo_root, rel)
        draft_utils.remove_draft(repo_root, state_key)
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "contextspace.draft_invalidation_failed",
            repo_root=str(repo_root),
            rel_path=rel_posix,
            state_key=state_key,
            exc=exc,
        )
        logger.debug(
            "contextspace draft invalidation failed for %s (repo_root=%s)",
            rel_posix,
            repo_root,
            exc_info=True,
        )
    return path.read_text(encoding="utf-8")


def read_contextspace_doc(repo_root: Path, kind: str) -> str:
    path = contextspace_doc_path(repo_root, kind)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_contextspace_doc(  # codeql[py/path-injection]
    repo_root: Path, kind: str, content: str
) -> str:
    path = contextspace_doc_path(repo_root, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        content or "", encoding="utf-8"
    )  # codeql[py/path-injection] contextspace_doc_path is deterministic
    rel = path.relative_to(repo_root).as_posix()
    state_key = f"contextspace_{rel.replace('/', '_')}"
    try:
        draft_utils.invalidate_drafts_for_path(repo_root, rel)
        draft_utils.remove_draft(repo_root, state_key)
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "contextspace.draft_invalidation_failed",
            repo_root=str(repo_root),
            rel_path=rel,
            state_key=state_key,
            kind=kind,
            exc=exc,
        )
        logger.debug(
            "contextspace draft invalidation failed for %s (repo_root=%s kind=%s)",
            rel,
            repo_root,
            kind,
            exc_info=True,
        )
    return path.read_text(encoding="utf-8")


def _format_mtime(path: Path) -> str | None:
    if not path.exists():
        return None
    ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return ts.isoformat()


def list_contextspace_files(
    repo_root: Path,
) -> list[ContextspaceFile]:  # codeql[py/path-injection]
    base = contextspace_dir(repo_root)
    base.mkdir(parents=True, exist_ok=True)

    pinned: list[ContextspaceFile] = []
    for kind in CONTEXTSPACE_DOC_KINDS:
        path = contextspace_doc_path(repo_root, kind)
        rel = path.relative_to(base).as_posix()
        pinned.append(
            ContextspaceFile(
                name=path.name,
                path=rel,
                is_pinned=True,
                modified_at=_format_mtime(path),
            )
        )

    others: list[ContextspaceFile] = []
    if base.exists():
        for file_path in base.rglob("*"):
            if file_path.is_dir():
                continue
            try:
                rel = file_path.relative_to(base).as_posix()
            except ValueError:
                continue
            if any(rel == pinned_file.path for pinned_file in pinned):
                continue
            others.append(
                ContextspaceFile(
                    name=file_path.name,
                    path=rel,
                    is_pinned=False,
                    modified_at=_format_mtime(file_path),
                )
            )

    others.sort(key=lambda f: f.path)
    return [*pinned, *others]


def _sort_contextspace_children(path: Path) -> tuple[int, str]:
    # Folders first, then files, both alphabetized (case-insensitive)
    return (0 if path.is_dir() else 1, path.name.lower())


def _is_within_contextspace(base_real: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(base_real)
        return True
    except Exception:
        return False


def _file_node(base: Path, path: Path, is_pinned: bool = False) -> ContextspaceNode:
    rel = path.relative_to(base).as_posix()
    size: int | None = None
    if path.exists() and path.is_file():
        try:
            size = path.stat().st_size
        except OSError:
            size = None
    return ContextspaceNode(
        name=path.name,
        path=rel,
        type="file",
        is_pinned=is_pinned,
        modified_at=_format_mtime(path),
        size=size,
    )


def _build_contextspace_tree(base: Path, path: Path) -> ContextspaceNode:
    is_symlink = path.is_symlink()
    is_folder = path.is_dir() and not is_symlink
    is_pinned = path.name in PINNED_DOC_FILENAMES and path.parent == base

    if not is_folder:
        return _file_node(base, path, is_pinned=is_pinned)

    children: list[ContextspaceNode] = []
    for child in sorted(path.iterdir(), key=_sort_contextspace_children):
        # Avoid duplicating pinned docs surfaced at the root list
        if child.parent == base and child.name in PINNED_DOC_FILENAMES:
            continue
        # Skip symlink escapes that resolve outside the contextspace
        if child.is_symlink() and not _is_within_contextspace(base.resolve(), child):
            continue
        children.append(_build_contextspace_tree(base, child))

    return ContextspaceNode(
        name=path.name,
        path=path.relative_to(base).as_posix(),
        type="folder",
        is_pinned=False,
        modified_at=_format_mtime(path),
        children=children,
    )


def list_contextspace_tree(repo_root: Path) -> list[ContextspaceNode]:
    """Return hierarchical contextspace structure (folders + files)."""

    base = contextspace_dir(repo_root)
    base.mkdir(parents=True, exist_ok=True)
    base_real = base.resolve()

    nodes: list[ContextspaceNode] = []

    # Pinned docs first (even if missing)
    for name in sorted(PINNED_DOC_FILENAMES):
        pinned_path = base / name
        nodes.append(_file_node(base, pinned_path, is_pinned=True))

    for child in sorted(base.iterdir(), key=_sort_contextspace_children):
        if child.parent == base and child.name in PINNED_DOC_FILENAMES:
            continue
        if child.is_symlink() and not _is_within_contextspace(base_real, child):
            continue
        nodes.append(_build_contextspace_tree(base, child))

    return nodes
