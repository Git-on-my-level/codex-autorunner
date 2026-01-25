from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

WorkspaceDocKind = Literal["active_context", "decisions", "spec"]
WORKSPACE_DOC_KINDS: tuple[WorkspaceDocKind, ...] = (
    "active_context",
    "decisions",
    "spec",
)


def _normalize_kind(kind: str) -> WorkspaceDocKind:
    key = (kind or "").strip().lower()
    if key not in WORKSPACE_DOC_KINDS:
        raise ValueError("invalid workspace doc kind")
    return cast(WorkspaceDocKind, key)


def workspace_dir(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "workspace"


def workspace_doc_path(repo_root: Path, kind: str) -> Path:
    key = _normalize_kind(kind)
    return workspace_dir(repo_root) / f"{key}.md"


def read_workspace_doc(repo_root: Path, kind: str) -> str:
    path = workspace_doc_path(repo_root, kind)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_workspace_doc(repo_root: Path, kind: str, content: str) -> str:
    path = workspace_doc_path(repo_root, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")
    return path.read_text(encoding="utf-8")
