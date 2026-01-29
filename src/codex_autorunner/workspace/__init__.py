"""Workspace docs helpers (active context, decisions, spec).

Workspace docs are optional and live under `.codex-autorunner/workspace/`.
They are distinct from tickets, which live under `.codex-autorunner/tickets/`.
"""

import hashlib
from pathlib import Path

from ..core.utils import canonicalize_path
from .paths import (
    WORKSPACE_DOC_KINDS,
    WorkspaceDocKind,
    read_workspace_doc,
    workspace_doc_path,
    write_workspace_doc,
)

WORKSPACE_ID_HEX_LEN = 12


def canonical_workspace_root(path: Path) -> Path:
    return canonicalize_path(path)


def workspace_id_for_path(path: Path) -> str:
    canonical = canonical_workspace_root(path)
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
    return digest[:WORKSPACE_ID_HEX_LEN]


__all__ = [
    "WORKSPACE_DOC_KINDS",
    "WorkspaceDocKind",
    "workspace_doc_path",
    "read_workspace_doc",
    "write_workspace_doc",
    "canonical_workspace_root",
    "workspace_id_for_path",
]
