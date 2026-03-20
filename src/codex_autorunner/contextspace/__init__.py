"""Contextspace shared-doc helpers.

Contextspace is limited to the canonical shared docs under
`.codex-autorunner/contextspace/`.
"""

from .paths import (
    CONTEXTSPACE_DOC_KINDS,
    ContextspaceDocKind,
    contextspace_dir,
    contextspace_doc_path,
    normalize_contextspace_doc_kind,
    read_contextspace_doc,
    write_contextspace_doc,
)

__all__ = [
    "CONTEXTSPACE_DOC_KINDS",
    "PINNED_DOC_FILENAMES",
    "ContextspaceDocKind",
    "contextspace_dir",
    "contextspace_doc_path",
    "normalize_contextspace_doc_kind",
    "read_contextspace_doc",
    "write_contextspace_doc",
]
