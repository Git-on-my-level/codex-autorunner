from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from ..core import drafts as draft_utils

WorkspaceDocKind = Literal["active_context", "decisions", "spec"]
WORKSPACE_DOC_KINDS: tuple[WorkspaceDocKind, ...] = (
    "active_context",
    "decisions",
    "spec",
)


@dataclass
class WorkspaceFile:
    name: str
    path: str  # path relative to the workspace directory (POSIX)
    is_pinned: bool = False
    modified_at: str | None = None


def _normalize_kind(kind: str) -> WorkspaceDocKind:
    key = (kind or "").strip().lower()
    if key not in WORKSPACE_DOC_KINDS:
        raise ValueError("invalid workspace doc kind")
    return cast(WorkspaceDocKind, key)


def workspace_dir(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "workspace"


PINNED_DOC_FILENAMES = {f"{kind}.md" for kind in WORKSPACE_DOC_KINDS}


@dataclass
class WorkspaceNode:
    name: str
    path: str  # relative to workspace dir
    type: Literal["file", "folder"]
    is_pinned: bool = False
    modified_at: str | None = None
    size: int | None = None  # files only
    children: list["WorkspaceNode"] | None = None  # folders only


def normalize_workspace_rel_path(repo_root: Path, rel_path: str) -> tuple[Path, str]:
    """Normalize a user-supplied workspace path and ensure it stays in-tree.

    We accept POSIX-style relative paths only, then resolve the full path and
    verify the result is still under the workspace directory. This guards
    against ".." traversal and symlink escapes that CodeQL flagged.
    """

    base = workspace_dir(repo_root).resolve(strict=False)
    base_real = base.resolve(strict=False)
    cleaned = (rel_path or "").strip()
    if not cleaned:
        raise ValueError("invalid workspace file path")

    relative = PurePosixPath(cleaned)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid workspace file path")

    candidate = (base / relative).resolve(strict=False)
    try:
        rel_posix = candidate.relative_to(base_real).as_posix()
    except ValueError:
        raise ValueError("invalid workspace file path") from None

    return candidate, rel_posix


def sanitize_workspace_filename(filename: str) -> str:
    """Return a safe filename for workspace uploads.

    We strip any directory components, collapse whitespace, and guard against
    empty names. Caller is responsible for applying any per-workspace policy
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


def workspace_doc_path(repo_root: Path, kind: str) -> Path:
    key = _normalize_kind(kind)
    return workspace_dir(repo_root) / f"{key}.md"


def read_workspace_file(
    repo_root: Path, rel_path: str
) -> str:  # codeql[py/path-injection]
    path, _ = normalize_workspace_rel_path(repo_root, rel_path)
    if (
        path.is_dir()
    ):  # codeql[py/path-injection] validated by normalize_workspace_rel_path
        raise ValueError("path points to a directory")
    if (
        not path.exists()
    ):  # codeql[py/path-injection] validated by normalize_workspace_rel_path
        return ""
    return path.read_text(
        encoding="utf-8"
    )  # codeql[py/path-injection] validated by normalize_workspace_rel_path


def write_workspace_file(  # codeql[py/path-injection]
    repo_root: Path, rel_path: str, content: str
) -> str:
    path, rel_posix = normalize_workspace_rel_path(repo_root, rel_path)
    if (
        path.exists() and path.is_dir()
    ):  # codeql[py/path-injection] validated by normalize_workspace_rel_path
        raise ValueError("path points to a directory")
    path.parent.mkdir(
        parents=True, exist_ok=True
    )  # codeql[py/path-injection] validated by normalize_workspace_rel_path
    path.write_text(
        content or "", encoding="utf-8"
    )  # codeql[py/path-injection] validated by normalize_workspace_rel_path
    try:
        rel = path.relative_to(repo_root).as_posix()
        draft_utils.invalidate_drafts_for_path(repo_root, rel)
        state_key = f"workspace_{rel_posix.replace('/', '_')}"
        draft_utils.remove_draft(repo_root, state_key)
    except Exception:
        # best effort; do not block writes
        pass
    return path.read_text(encoding="utf-8")


def read_workspace_doc(repo_root: Path, kind: str) -> str:
    path = workspace_doc_path(repo_root, kind)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_workspace_doc(  # codeql[py/path-injection]
    repo_root: Path, kind: str, content: str
) -> str:
    path = workspace_doc_path(repo_root, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        content or "", encoding="utf-8"
    )  # codeql[py/path-injection] workspace_doc_path is deterministic
    try:
        rel = path.relative_to(repo_root).as_posix()
        draft_utils.invalidate_drafts_for_path(repo_root, rel)
        state_key = f"workspace_{rel.replace('/', '_')}"
        draft_utils.remove_draft(repo_root, state_key)
    except Exception:
        pass
    return path.read_text(encoding="utf-8")


def _format_mtime(path: Path) -> str | None:
    if not path.exists():
        return None
    ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return ts.isoformat()


def list_workspace_files(
    repo_root: Path,
) -> list[WorkspaceFile]:  # codeql[py/path-injection]
    base = workspace_dir(repo_root)
    base.mkdir(parents=True, exist_ok=True)

    pinned: list[WorkspaceFile] = []
    for kind in WORKSPACE_DOC_KINDS:
        path = workspace_doc_path(repo_root, kind)
        rel = path.relative_to(base).as_posix()
        pinned.append(
            WorkspaceFile(
                name=path.name,
                path=rel,
                is_pinned=True,
                modified_at=_format_mtime(path),
            )
        )

    others: list[WorkspaceFile] = []
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
                WorkspaceFile(
                    name=file_path.name,
                    path=rel,
                    is_pinned=False,
                    modified_at=_format_mtime(file_path),
                )
            )

    others.sort(key=lambda f: f.path)
    return [*pinned, *others]


def _sort_workspace_children(path: Path) -> tuple[int, str]:
    # Folders first, then files, both alphabetized (case-insensitive)
    return (0 if path.is_dir() else 1, path.name.lower())


def _is_within_workspace(base_real: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(base_real)
        return True
    except Exception:
        return False


def _file_node(base: Path, path: Path, is_pinned: bool = False) -> WorkspaceNode:
    rel = path.relative_to(base).as_posix()
    size: int | None = None
    if path.exists() and path.is_file():
        try:
            size = path.stat().st_size
        except OSError:
            size = None
    return WorkspaceNode(
        name=path.name,
        path=rel,
        type="file",
        is_pinned=is_pinned,
        modified_at=_format_mtime(path),
        size=size,
    )


def _build_workspace_tree(base: Path, path: Path) -> WorkspaceNode:
    is_symlink = path.is_symlink()
    is_folder = path.is_dir() and not is_symlink
    is_pinned = path.name in PINNED_DOC_FILENAMES and path.parent == base

    if not is_folder:
        return _file_node(base, path, is_pinned=is_pinned)

    children: list[WorkspaceNode] = []
    for child in sorted(path.iterdir(), key=_sort_workspace_children):
        # Avoid duplicating pinned docs surfaced at the root list
        if child.parent == base and child.name in PINNED_DOC_FILENAMES:
            continue
        # Skip symlink escapes that resolve outside the workspace
        if child.is_symlink() and not _is_within_workspace(base.resolve(), child):
            continue
        children.append(_build_workspace_tree(base, child))

    return WorkspaceNode(
        name=path.name,
        path=path.relative_to(base).as_posix(),
        type="folder",
        is_pinned=False,
        modified_at=_format_mtime(path),
        children=children,
    )


def list_workspace_tree(repo_root: Path) -> list[WorkspaceNode]:
    """Return hierarchical workspace structure (folders + files)."""

    base = workspace_dir(repo_root)
    base.mkdir(parents=True, exist_ok=True)
    base_real = base.resolve()

    nodes: list[WorkspaceNode] = []

    # Pinned docs first (even if missing)
    for name in sorted(PINNED_DOC_FILENAMES):
        pinned_path = base / name
        nodes.append(_file_node(base, pinned_path, is_pinned=True))

    for child in sorted(base.iterdir(), key=_sort_workspace_children):
        if child.parent == base and child.name in PINNED_DOC_FILENAMES:
            continue
        if child.is_symlink() and not _is_within_workspace(base_real, child):
            continue
        nodes.append(_build_workspace_tree(base, child))

    return nodes
