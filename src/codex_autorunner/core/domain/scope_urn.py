from __future__ import annotations

from typing import Optional
from urllib.parse import quote, unquote


class ScopeUrnError(ValueError):
    """Base error for invalid scope URNs."""


class ScopeUrnParseError(ScopeUrnError):
    """Raised when a scope URN string cannot be parsed."""

    def __init__(self, urn: str, *, reason: str = "") -> None:
        self.urn = urn
        self.reason = reason
        msg = f"Invalid scope URN: {urn}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)


class ScopeUrnKindError(ScopeUrnError):
    """Raised when a scope URN uses an unrecognized kind."""

    def __init__(self, urn: str, *, kind: str) -> None:
        self.urn = urn
        self.kind = kind
        super().__init__(f"Unknown scope kind '{kind}' in URN: {urn}")


VALID_SCOPE_KINDS = frozenset(
    {"hub", "repo", "worktree", "agent_workspace", "filesystem"}
)


def _require_segment(value: Optional[str], *, kind: str, field: str) -> str:
    if not value:
        raise ScopeUrnError(f"{kind} scope requires {field}")
    if "/" in value:
        raise ScopeUrnError(f"{kind} scope {field} must not contain '/'")
    return value


def _validate_percent_escapes(value: str, *, urn: str) -> None:
    percent_pos = value.find("%")
    while percent_pos >= 0:
        escape = value[percent_pos + 1 : percent_pos + 3]
        if len(escape) != 2 or any(c not in "0123456789abcdefABCDEF" for c in escape):
            raise ScopeUrnParseError(urn, reason="filesystem path has invalid escape")
        percent_pos = value.find("%", percent_pos + 1)


def format_scope_urn(
    *,
    kind: str,
    id: Optional[str] = None,
    parent_repo_id: Optional[str] = None,
) -> str:
    if kind not in VALID_SCOPE_KINDS:
        raise ScopeUrnKindError(urn=f"{kind}:{id}", kind=kind)
    if kind == "hub":
        return "hub"
    if kind == "repo":
        id = _require_segment(id, kind=kind, field="an id")
        return f"repo:{id}"
    if kind == "worktree":
        id = _require_segment(id, kind=kind, field="an id")
        parent_repo_id = _require_segment(
            parent_repo_id, kind=kind, field="a parent_repo_id"
        )
        return f"worktree:{parent_repo_id}/{id}"
    if kind == "agent_workspace":
        id = _require_segment(id, kind=kind, field="an id")
        return f"agent_workspace:{id}"
    if kind == "filesystem":
        if not id:
            raise ScopeUrnError("filesystem scope requires a path")
        return f"filesystem:{quote(id, safe='')}"
    raise ScopeUrnKindError(urn=kind, kind=kind)


def parse_scope_urn(urn: str) -> dict[str, Optional[str]]:
    if not isinstance(urn, str) or not urn.strip():
        raise ScopeUrnParseError(repr(urn), reason="URN must be a non-empty string")

    if urn == "hub":
        return {"kind": "hub", "id": None, "parent_repo_id": None}

    colon_pos = urn.find(":")
    if colon_pos < 0:
        raise ScopeUrnParseError(
            urn, reason="URN must contain ':' separator or be 'hub'"
        )

    kind = urn[:colon_pos]
    path = urn[colon_pos + 1 :]

    if kind not in VALID_SCOPE_KINDS:
        raise ScopeUrnKindError(urn, kind=kind)

    if kind == "hub":
        raise ScopeUrnParseError(urn, reason="hub scope must not have a path component")

    if kind == "repo":
        if not path:
            raise ScopeUrnParseError(urn, reason="repo scope requires an id after ':'")
        if "/" in path:
            raise ScopeUrnParseError(urn, reason="repo scope id must not contain '/'")
        return {"kind": "repo", "id": path, "parent_repo_id": None}

    if kind == "worktree":
        slash_pos = path.find("/")
        if slash_pos <= 0 or slash_pos == len(path) - 1:
            raise ScopeUrnParseError(
                urn, reason="worktree scope requires '<repo_id>/<worktree_id>' path"
            )
        return {
            "kind": "worktree",
            "id": path[slash_pos + 1 :],
            "parent_repo_id": path[:slash_pos],
        }

    if kind == "agent_workspace":
        if not path:
            raise ScopeUrnParseError(
                urn, reason="agent_workspace scope requires an id after ':'"
            )
        if "/" in path:
            raise ScopeUrnParseError(
                urn, reason="agent_workspace scope id must not contain '/'"
            )
        return {"kind": "agent_workspace", "id": path, "parent_repo_id": None}

    if kind == "filesystem":
        if not path:
            raise ScopeUrnParseError(urn, reason="filesystem scope requires a path")
        _validate_percent_escapes(path, urn=urn)
        decoded_path = unquote(path)
        if not decoded_path:
            raise ScopeUrnParseError(urn, reason="filesystem scope requires a path")
        return {"kind": "filesystem", "id": decoded_path, "parent_repo_id": None}

    raise ScopeUrnKindError(urn, kind=kind)


__all__ = [
    "VALID_SCOPE_KINDS",
    "ScopeUrnError",
    "ScopeUrnKindError",
    "ScopeUrnParseError",
    "format_scope_urn",
    "parse_scope_urn",
]
