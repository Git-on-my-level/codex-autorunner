from __future__ import annotations

from codex_autorunner.core.domain.refs import ScopeRef
from codex_autorunner.core.domain.scope_chain import parent_scope, scope_chain


class TestParentScope:
    def test_hub_has_no_parent(self) -> None:
        assert parent_scope(ScopeRef(kind="hub")) is None

    def test_repo_parent_is_hub(self) -> None:
        p = parent_scope(ScopeRef(kind="repo", id="r1"))
        assert p == ScopeRef(kind="hub")

    def test_worktree_parent_is_repo(self) -> None:
        p = parent_scope(ScopeRef(kind="worktree", id="wt1", parent_repo_id="r1"))
        assert p == ScopeRef(kind="repo", id="r1")

    def test_agent_workspace_parent_is_hub(self) -> None:
        p = parent_scope(ScopeRef(kind="agent_workspace", id="ws1"))
        assert p == ScopeRef(kind="hub")

    def test_filesystem_parent_is_hub(self) -> None:
        p = parent_scope(ScopeRef(kind="filesystem", path="/tmp/repo"))
        assert p == ScopeRef(kind="hub")


class TestScopeChain:
    def test_hub_chain_is_just_hub(self) -> None:
        chain = scope_chain(ScopeRef(kind="hub"))
        assert chain == (ScopeRef(kind="hub"),)

    def test_repo_chain(self) -> None:
        chain = scope_chain(ScopeRef(kind="repo", id="r1"))
        assert chain == (
            ScopeRef(kind="repo", id="r1"),
            ScopeRef(kind="hub"),
        )

    def test_worktree_chain(self) -> None:
        chain = scope_chain(ScopeRef(kind="worktree", id="wt1", parent_repo_id="r1"))
        assert chain == (
            ScopeRef(kind="worktree", id="wt1", parent_repo_id="r1"),
            ScopeRef(kind="repo", id="r1"),
            ScopeRef(kind="hub"),
        )

    def test_agent_workspace_chain(self) -> None:
        chain = scope_chain(ScopeRef(kind="agent_workspace", id="ws1"))
        assert chain == (
            ScopeRef(kind="agent_workspace", id="ws1"),
            ScopeRef(kind="hub"),
        )

    def test_filesystem_chain(self) -> None:
        chain = scope_chain(ScopeRef(kind="filesystem", path="/tmp/repo"))
        assert chain == (
            ScopeRef(kind="filesystem", path="/tmp/repo"),
            ScopeRef(kind="hub"),
        )

    def test_chain_starts_with_input_scope(self) -> None:
        scope = ScopeRef(kind="repo", id="abc")
        chain = scope_chain(scope)
        assert chain[0] == scope

    def test_chain_ends_with_hub(self) -> None:
        for kind_args in [
            {"kind": "hub"},
            {"kind": "repo", "id": "r1"},
            {"kind": "worktree", "id": "wt1", "parent_repo_id": "r1"},
            {"kind": "agent_workspace", "id": "ws1"},
            {"kind": "filesystem", "path": "/tmp/repo"},
        ]:
            chain = scope_chain(ScopeRef(**kind_args))
            assert chain[-1] == ScopeRef(kind="hub")
