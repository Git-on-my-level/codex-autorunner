"""
Contract tests for ScopeResolver implementations.

Every adapter registered in ``SCOPE_RESOLVER_FACTORIES`` (see
``tests/contracts/conftest.py``) must satisfy these contracts.
Adding a new adapter without updating the factory will cause CI to
fail when the factory list diverges from actual adapters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from codex_autorunner.core import adapters as adapter_exports
from codex_autorunner.core.adapters import FilesystemScopeResolver
from codex_autorunner.core.domain.refs import ScopeRef, ScopeRefError
from codex_autorunner.core.domain.scope_chain import parent_scope, scope_chain
from codex_autorunner.core.domain.scope_urn import ScopeUrnError, parse_scope_urn
from tests.contracts.conftest import SCOPE_RESOLVER_FACTORIES

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_safe_id = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=32,
)

_safe_path_segment = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- ",
    min_size=1,
    max_size=16,
).filter(str.strip)

_safe_path = st.lists(_safe_path_segment, min_size=1, max_size=4).map("/".join)

scope_ref_strategy = st.one_of(
    st.just(ScopeRef(kind="hub")),
    st.builds(ScopeRef, kind=st.just("repo"), id=_safe_id),
    st.builds(
        ScopeRef,
        kind=st.just("worktree"),
        id=_safe_id,
        parent_repo_id=_safe_id,
    ),
    st.builds(ScopeRef, kind=st.just("filesystem"), path=_safe_path),
)


def _factory_name(class_name: str, suffix: str) -> str:
    assert class_name.endswith(suffix)
    return class_name[: -len(suffix)].lower()


class TestScopeResolverContract:
    """Invariants every ScopeResolver must satisfy."""

    def test_exported_scope_resolver_adapters_have_contract_factories(self) -> None:
        registered = {name for name, _ in SCOPE_RESOLVER_FACTORIES}
        expected = {
            _factory_name(name, "ScopeResolver")
            for name in adapter_exports.__all__
            if name.endswith("ScopeResolver")
        }
        assert registered == expected

    def test_resolve_returns_resolved_scope_with_same_ref(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        hub = ScopeRef(kind="hub")
        result = resolver.resolve(hub)
        assert result.scope == hub

    def test_resolve_hub_has_workspace_root(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        result = resolver.resolve(ScopeRef(kind="hub"))
        assert result.workspace_root is not None

    def test_resolve_repo_returns_workspace_root(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        result = resolver.resolve(ScopeRef(kind="repo", id="repo-1"))
        assert result.workspace_root is not None

    def test_resolve_filesystem_returns_path_as_root(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        fs = ScopeRef(kind="filesystem", path="/tmp/some-place")
        result = resolver.resolve(fs)
        assert result.workspace_root == "/tmp/some-place"

    def test_resolve_missing_repo_scope_raises_scope_error(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        with pytest.raises(ScopeRefError, match="Unknown repo scope"):
            resolver.resolve(ScopeRef(kind="repo", id="missing-repo"))

    def test_resolve_missing_worktree_scope_raises_scope_error(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        with pytest.raises(ScopeRefError, match="Unknown worktree scope"):
            resolver.resolve(
                ScopeRef(kind="worktree", id="missing-wt", parent_repo_id="repo-1")
            )

    def test_resolve_parent_hub_returns_none(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        assert resolver.resolve_parent(ScopeRef(kind="hub")) is None

    def test_resolve_parent_repo_returns_hub(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        parent = resolver.resolve_parent(ScopeRef(kind="repo", id="repo-1"))
        assert parent == ScopeRef(kind="hub")

    def test_resolve_parent_worktree_returns_repo(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        parent = resolver.resolve_parent(
            ScopeRef(kind="worktree", id="wt-1", parent_repo_id="repo-1")
        )
        assert parent == ScopeRef(kind="repo", id="repo-1")

    def test_resolve_children_returns_list_of_scope_refs(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        children = resolver.resolve_children(ScopeRef(kind="hub"))
        assert isinstance(children, list)
        for child in children:
            assert isinstance(child, ScopeRef)

    def test_resolve_children_leaf_returns_empty(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        children = resolver.resolve_children(
            ScopeRef(kind="worktree", id="wt-1", parent_repo_id="repo-1")
        )
        assert children == []

    def test_parent_consistent_with_domain_chain(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        for ref in [
            ScopeRef(kind="hub"),
            ScopeRef(kind="repo", id="repo-1"),
            ScopeRef(kind="worktree", id="wt-1", parent_repo_id="repo-1"),
            ScopeRef(kind="filesystem", path="/tmp/x"),
        ]:
            assert resolver.resolve_parent(ref) == parent_scope(ref)

    def test_has_required_protocol_methods(
        self,
        scope_resolver_factory: Callable[[Path], FilesystemScopeResolver],
        tmp_path: Path,
    ) -> None:
        resolver = scope_resolver_factory(tmp_path)
        assert callable(getattr(resolver, "resolve", None))
        assert callable(getattr(resolver, "resolve_parent", None))
        assert callable(getattr(resolver, "resolve_children", None))


class TestUrnRoundTripInvariants:
    """Hypothesis-based invariants for URN round trips and scope chains."""

    @given(scope=scope_ref_strategy)
    @settings(max_examples=100)
    def test_scope_ref_urn_round_trip(self, scope: ScopeRef) -> None:
        urn = scope.to_urn()
        restored = ScopeRef.from_urn(urn)
        assert restored == scope

    @given(scope=scope_ref_strategy)
    @settings(max_examples=100)
    def test_format_parse_round_trip(self, scope: ScopeRef) -> None:
        urn = scope.to_urn()
        parts = parse_scope_urn(urn)
        assert parts["kind"] == scope.kind
        if scope.kind == "filesystem":
            assert parts["id"] == scope.path
        else:
            assert parts["id"] == scope.id
        assert parts["parent_repo_id"] == scope.parent_repo_id

    @given(scope=scope_ref_strategy)
    @settings(max_examples=100)
    def test_chain_starts_with_self_ends_with_hub(self, scope: ScopeRef) -> None:
        chain = scope_chain(scope)
        assert chain[0] == scope
        assert chain[-1] == ScopeRef(kind="hub")

    @given(scope=scope_ref_strategy)
    @settings(max_examples=100)
    def test_chain_parent_consistency(self, scope: ScopeRef) -> None:
        chain = scope_chain(scope)
        for i in range(len(chain) - 1):
            assert parent_scope(chain[i]) == chain[i + 1]

    @given(scope=scope_ref_strategy)
    @settings(max_examples=50)
    def test_parent_chain_never_loops(self, scope: ScopeRef) -> None:
        seen = set()
        current = scope
        while current is not None:
            assert current not in seen, f"loop detected at {current}"
            seen.add(current)
            current = parent_scope(current)


class TestInvalidUrnContract:
    """Invalid scope URNs must fail before adapter-specific resolution."""

    @pytest.mark.parametrize(
        "urn",
        [
            "",
            "repo:",
            "repo:a/b",
            "worktree:noslash",
            "worktree:/wt1",
            "vault:",
            "vault:a/b",
            "filesystem:",
            "filesystem:%ZZ",
            "planet:earth",
            "hub:extra",
        ],
    )
    def test_invalid_urns_raise_scope_urn_error(self, urn: str) -> None:
        with pytest.raises(ScopeUrnError):
            ScopeRef.from_urn(urn)
