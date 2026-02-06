"""Contextspace shared-doc helpers (active context, decisions, spec).

Contextspace docs are optional and live under `.codex-autorunner/contextspace/`.
They are distinct from tickets, which live under `.codex-autorunner/tickets/`.
"""

from .paths import (
    CONTEXTSPACE_DOC_KINDS,
    PINNED_DOC_FILENAMES,
    ContextspaceDocKind,
    ContextspaceFile,
    ContextspaceNode,
    contextspace_dir,
    contextspace_doc_path,
    list_contextspace_files,
    list_contextspace_tree,
    normalize_contextspace_rel_path,
    read_contextspace_doc,
    read_contextspace_file,
    sanitize_contextspace_filename,
    write_contextspace_doc,
    write_contextspace_file,
)

__all__ = [
    "CONTEXTSPACE_DOC_KINDS",
    "ContextspaceDocKind",
    "ContextspaceFile",
    "ContextspaceNode",
    "PINNED_DOC_FILENAMES",
    "contextspace_dir",
    "contextspace_doc_path",
    "list_contextspace_files",
    "list_contextspace_tree",
    "normalize_contextspace_rel_path",
    "read_contextspace_doc",
    "read_contextspace_file",
    "sanitize_contextspace_filename",
    "write_contextspace_doc",
    "write_contextspace_file",
]
