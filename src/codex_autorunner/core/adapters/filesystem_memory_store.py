from __future__ import annotations

from pathlib import Path
from typing import Optional

from ...contextspace.paths import (
    contextspace_doc_path,
    read_contextspace_docs,
    write_contextspace_doc,
)
from ..domain.refs import MemoryRef, ScopeRef
from ..ports.memory_store import MemoryDoc, MemoryDocs
from ..ports.scope_resolver import ScopeResolver


class FilesystemMemoryStore:
    def __init__(self, scope_resolver: ScopeResolver) -> None:
        self._resolver = scope_resolver

    def _workspace_root(self, scope: ScopeRef) -> Optional[Path]:
        resolved = self._resolver.resolve(scope)
        if resolved.workspace_root is None:
            return None
        return Path(resolved.workspace_root)

    async def load(self, ref: MemoryRef) -> Optional[MemoryDoc]:
        root = self._workspace_root(ref.scope)
        if root is None:
            return None
        try:
            path = contextspace_doc_path(root, ref.key)
        except ValueError:
            return None
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8")
        return MemoryDoc(key=ref.key, content=content)

    async def load_scope(self, scope: ScopeRef) -> MemoryDocs:
        root = self._workspace_root(scope)
        if root is None:
            return MemoryDocs(scope=scope, docs=[])
        raw = read_contextspace_docs(root)
        docs = [
            MemoryDoc(key=kind, content=content)
            for kind, content in raw.items()
            if content
        ]
        return MemoryDocs(scope=scope, docs=docs)

    async def save(self, ref: MemoryRef, doc: MemoryDoc) -> MemoryDoc:
        root = self._workspace_root(ref.scope)
        if root is None:
            raise ValueError(
                f"Cannot save memory for scope without workspace_root: {ref.scope}"
            )
        write_contextspace_doc(root, ref.key, doc.content)
        return doc

    async def delete(self, ref: MemoryRef) -> bool:
        root = self._workspace_root(ref.scope)
        if root is None:
            return False
        try:
            path = contextspace_doc_path(root, ref.key)
        except ValueError:
            return False
        if not path.exists():
            return False
        path.unlink()
        return True


__all__ = ["FilesystemMemoryStore"]
