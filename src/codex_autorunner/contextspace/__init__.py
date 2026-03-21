"""Contextspace shared-doc helpers.

Contextspace is limited to the canonical shared docs under
`.codex-autorunner/contextspace/`.
"""

from .catalog import CONTEXTSPACE_DOC_CATALOG, ContextspaceDocCatalogEntry
from .paths import (
    CONTEXTSPACE_DOC_KINDS,
    ContextspaceDocKind,
    contextspace_dir,
    contextspace_doc_catalog,
    contextspace_doc_entry,
    contextspace_doc_path,
    normalize_contextspace_doc_kind,
    read_contextspace_doc,
    read_contextspace_docs,
    serialize_contextspace_doc_catalog,
    write_contextspace_doc,
)

__all__ = [
    "CONTEXTSPACE_DOC_CATALOG",
    "CONTEXTSPACE_DOC_KINDS",
    "ContextspaceDocCatalogEntry",
    "ContextspaceDocKind",
    "contextspace_doc_catalog",
    "contextspace_doc_entry",
    "contextspace_dir",
    "contextspace_doc_path",
    "normalize_contextspace_doc_kind",
    "read_contextspace_doc",
    "read_contextspace_docs",
    "serialize_contextspace_doc_catalog",
    "write_contextspace_doc",
]
