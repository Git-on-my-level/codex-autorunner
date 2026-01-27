from __future__ import annotations

import os
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


def normalize_workspace_rel_path(repo_root: Path, rel_path: str) -> tuple[Path, str]:
    """Normalize a user-supplied workspace path and ensure it stays in-tree.

    We accept POSIX-style relative paths only, then resolve the full path and
    verify the result is still under the workspace directory. This guards
    against ".." traversal and symlink escapes that CodeQL flagged.
    """

    base = workspace_dir(repo_root).resolve()
    cleaned = (rel_path or "").strip()
    if not cleaned:
        raise ValueError("invalid workspace file path")

    relative = PurePosixPath(cleaned)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid workspace file path")

    candidate = (base / relative).resolve()
    # Ensure the resolved path stays under the workspace directory
    if os.path.commonpath([base, candidate]) != str(base):
        raise ValueError("invalid workspace file path")

    rel_posix = candidate.relative_to(base).as_posix()
    return candidate, rel_posix


def workspace_doc_path(repo_root: Path, kind: str) -> Path:
    key = _normalize_kind(kind)
    return workspace_dir(repo_root) / f"{key}.md"


def read_workspace_file(repo_root: Path, rel_path: str) -> str:
    path, _ = normalize_workspace_rel_path(repo_root, rel_path)
    if path.is_dir():
        raise ValueError("path points to a directory")
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_workspace_file(repo_root: Path, rel_path: str, content: str) -> str:
    path, rel_posix = normalize_workspace_rel_path(repo_root, rel_path)
    if path.exists() and path.is_dir():
        raise ValueError("path points to a directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")
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


def write_workspace_doc(repo_root: Path, kind: str, content: str) -> str:
    path = workspace_doc_path(repo_root, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")
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


def list_workspace_files(repo_root: Path) -> list[WorkspaceFile]:
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
