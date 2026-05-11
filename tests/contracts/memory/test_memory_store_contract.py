"""
Contract tests for MemoryStore implementations.

Every adapter registered in ``MEMORY_STORE_FACTORIES`` (see
``tests/contracts/conftest.py``) must satisfy these contracts.

Note: the filesystem memory store adapter only accepts keys from the
``CONTEXTSPACE_DOC_KINDS`` catalog (``active_context``, ``decisions``,
``spec``).  Contract tests use only those valid keys.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from codex_autorunner.core import adapters as adapter_exports
from codex_autorunner.core.adapters import FilesystemMemoryStore
from codex_autorunner.core.domain.refs import MemoryRef, ScopeRef, ScopeRefError
from codex_autorunner.core.ports.memory_store import MemoryDoc, MemoryDocs
from tests.contracts.conftest import MEMORY_STORE_FACTORIES

_doc_content = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 \n.#-*_",
    min_size=1,
    max_size=200,
)

_valid_kinds = st.sampled_from(["active_context", "decisions", "spec"])


def _factory_name(class_name: str, suffix: str) -> str:
    assert class_name.endswith(suffix)
    return class_name[: -len(suffix)].lower()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestMemoryStoreContract:
    """Invariants every MemoryStore must satisfy."""

    def test_exported_memory_store_adapters_have_contract_factories(self) -> None:
        registered = {name for name, _ in MEMORY_STORE_FACTORIES}
        expected = {
            _factory_name(name, "MemoryStore")
            for name in adapter_exports.__all__
            if name.endswith("MemoryStore")
        }
        assert registered == expected

    def test_load_missing_returns_none(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="spec")
        assert _run(store.load(ref)) is None

    def test_save_then_load_roundtrip(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="active_context")
        doc = MemoryDoc(key="active_context", content="hello world")
        _run(store.save(ref, doc))
        loaded = _run(store.load(ref))
        assert loaded is not None
        assert loaded.key == "active_context"
        assert loaded.content == "hello world"

    def test_save_returns_same_doc(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="decisions")
        doc = MemoryDoc(key="decisions", content="data")
        returned = _run(store.save(ref, doc))
        assert returned.key == doc.key
        assert returned.content == doc.content

    def test_save_overwrites_existing(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="spec")
        _run(store.save(ref, MemoryDoc(key="spec", content="v1")))
        _run(store.save(ref, MemoryDoc(key="spec", content="v2")))
        loaded = _run(store.load(ref))
        assert loaded is not None
        assert loaded.content == "v2"

    def test_delete_existing_returns_true(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="decisions")
        _run(store.save(ref, MemoryDoc(key="decisions", content="bye")))
        assert _run(store.delete(ref)) is True

    def test_delete_then_load_returns_none(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="spec")
        _run(store.save(ref, MemoryDoc(key="spec", content="gone")))
        _run(store.delete(ref))
        assert _run(store.load(ref)) is None

    def test_delete_missing_returns_false(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="spec")
        assert _run(store.delete(ref)) is False

    def test_load_missing_scope_raises_scope_error(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        ref = MemoryRef(scope=ScopeRef(kind="repo", id="missing-repo"), key="spec")
        with pytest.raises(ScopeRefError, match="Unknown repo scope"):
            _run(store.load(ref))

    def test_save_missing_scope_raises_scope_error(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        ref = MemoryRef(
            scope=ScopeRef(kind="worktree", id="missing-wt", parent_repo_id="repo-1"),
            key="spec",
        )
        with pytest.raises(ScopeRefError, match="Unknown worktree scope"):
            _run(store.save(ref, MemoryDoc(key="spec", content="content")))

    def test_invalid_key_is_rejected_without_writing(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref = MemoryRef(scope=scope, key="../outside")
        assert _run(store.load(ref)) is None
        assert _run(store.delete(ref)) is False
        with pytest.raises(ValueError):
            _run(store.save(ref, MemoryDoc(key="../outside", content="nope")))

    def test_load_scope_returns_memory_docs(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        ref1 = MemoryRef(scope=scope, key="active_context")
        ref2 = MemoryRef(scope=scope, key="spec")
        _run(store.save(ref1, MemoryDoc(key="active_context", content="ctx content")))
        _run(store.save(ref2, MemoryDoc(key="spec", content="spec content")))
        result = _run(store.load_scope(scope))
        assert isinstance(result, MemoryDocs)
        assert result.scope == scope
        keys = {d.key for d in result.docs}
        assert "active_context" in keys
        assert "spec" in keys

    def test_load_scope_empty_scope_returns_empty_docs(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        scope = ScopeRef(kind="repo", id="repo-1")
        result = _run(store.load_scope(scope))
        assert isinstance(result, MemoryDocs)
        assert result.docs == []

    def test_writes_scoped_to_same_scope(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        repo_scope = ScopeRef(kind="repo", id="repo-1")
        wt_scope = ScopeRef(kind="worktree", id="wt-1", parent_repo_id="repo-1")
        ref_repo = MemoryRef(scope=repo_scope, key="active_context")
        ref_wt = MemoryRef(scope=wt_scope, key="active_context")
        _run(
            store.save(
                ref_repo,
                MemoryDoc(key="active_context", content="repo memory"),
            )
        )
        _run(
            store.save(
                ref_wt,
                MemoryDoc(key="active_context", content="worktree memory"),
            )
        )
        repo_doc = _run(store.load(ref_repo))
        wt_doc = _run(store.load(ref_wt))
        assert repo_doc is not None
        assert repo_doc.content == "repo memory"
        assert wt_doc is not None
        assert wt_doc.content == "worktree memory"

    def test_has_required_protocol_methods(
        self,
        memory_store_factory: Callable[[Path], FilesystemMemoryStore],
        tmp_path: Path,
    ) -> None:
        store = memory_store_factory(tmp_path)
        assert callable(getattr(store, "load", None))
        assert callable(getattr(store, "load_scope", None))
        assert callable(getattr(store, "save", None))
        assert callable(getattr(store, "delete", None))


class TestMemoryWriteReadInvariant:
    """Hypothesis-based invariants: write then read through same scope."""

    @given(content=_doc_content, key=_valid_kinds)
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_save_load_preserves_content(self, content: str, key: str) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            from codex_autorunner.core.adapters import (
                FilesystemMemoryStore,
                FilesystemScopeResolver,
            )
            from tests.contracts.conftest import _make_hub_tree

            hub_root, manifest = _make_hub_tree(tmp)
            resolver = FilesystemScopeResolver(hub_root, manifest)
            store = FilesystemMemoryStore(resolver)
            scope = ScopeRef(kind="repo", id="repo-1")
            ref = MemoryRef(scope=scope, key=key)
            doc = MemoryDoc(key=key, content=content)
            _run(store.save(ref, doc))
            loaded = _run(store.load(ref))
            assert loaded is not None
            assert loaded.content == content
            assert loaded.key == key
