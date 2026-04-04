from __future__ import annotations

import sqlite3
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, cast

from .orchestration.sqlite import open_orchestration_sqlite
from .text_utils import _normalize_limit, _normalize_text
from .time_utils import now_iso

_PR_STATES = frozenset({"open", "closed", "merged", "draft"})
_ACTIVE_PR_STATES = ("open", "draft")
_TERMINAL_PR_STATES = frozenset({"closed", "merged"})


def _normalize_int(value: Any, *, field_name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if normalized <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return normalized


def _normalize_state(value: Any, *, field_name: str = "pr_state") -> str:
    normalized = _normalize_text(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    if normalized not in _PR_STATES:
        allowed = ", ".join(sorted(_PR_STATES))
        raise ValueError(f"{field_name} must be one of: {allowed}")
    return normalized


@dataclass(frozen=True)
class PrBinding:
    binding_id: str
    provider: str
    repo_slug: str
    pr_number: int
    pr_state: str
    created_at: str
    updated_at: str
    repo_id: Optional[str] = None
    head_branch: Optional[str] = None
    base_branch: Optional[str] = None
    thread_target_id: Optional[str] = None
    closed_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _binding_from_row(row: sqlite3.Row) -> PrBinding:
    return PrBinding(
        binding_id=str(row["binding_id"]),
        provider=str(row["provider"]),
        repo_slug=str(row["repo_slug"]),
        pr_number=_normalize_int(row["pr_number"], field_name="pr_number"),
        pr_state=_normalize_state(row["pr_state"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        repo_id=_normalize_text(row["repo_id"]),
        head_branch=_normalize_text(row["head_branch"]),
        base_branch=_normalize_text(row["base_branch"]),
        thread_target_id=_normalize_text(row["thread_target_id"]),
        closed_at=_normalize_text(row["closed_at"]),
    )


class PrBindingStore:
    """SQLite-backed storage for optional SCM PR bindings."""

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = Path(hub_root)

    def upsert_binding(
        self,
        *,
        provider: str,
        repo_slug: str,
        pr_number: int,
        pr_state: str = "open",
        repo_id: Optional[str] = None,
        head_branch: Optional[str] = None,
        base_branch: Optional[str] = None,
        thread_target_id: Optional[str] = None,
    ) -> PrBinding:
        normalized_provider = _normalize_text(provider)
        normalized_repo_slug = _normalize_text(repo_slug)
        normalized_pr_number = _normalize_int(pr_number, field_name="pr_number")
        normalized_pr_state = _normalize_state(pr_state)
        normalized_repo_id = _normalize_text(repo_id)
        normalized_head_branch = _normalize_text(head_branch)
        normalized_base_branch = _normalize_text(base_branch)
        normalized_thread_target_id = _normalize_text(thread_target_id)
        if normalized_provider is None:
            raise ValueError("provider is required")
        if normalized_repo_slug is None:
            raise ValueError("repo_slug is required")

        timestamp = now_iso()
        closed_at = timestamp if normalized_pr_state in _TERMINAL_PR_STATES else None

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_binding_row(
                conn,
                provider=normalized_provider,
                repo_slug=normalized_repo_slug,
                pr_number=normalized_pr_number,
            )
            if row is None:
                binding_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO orch_pr_bindings (
                        binding_id,
                        provider,
                        repo_slug,
                        repo_id,
                        pr_number,
                        pr_state,
                        head_branch,
                        base_branch,
                        thread_target_id,
                        created_at,
                        updated_at,
                        closed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding_id,
                        normalized_provider,
                        normalized_repo_slug,
                        normalized_repo_id,
                        normalized_pr_number,
                        normalized_pr_state,
                        normalized_head_branch,
                        normalized_base_branch,
                        normalized_thread_target_id,
                        timestamp,
                        timestamp,
                        closed_at,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE orch_pr_bindings
                       SET repo_id = COALESCE(?, repo_id),
                           pr_state = ?,
                           head_branch = CASE
                               WHEN ? IS NULL THEN head_branch
                               ELSE ?
                           END,
                           base_branch = CASE
                               WHEN ? IS NULL THEN base_branch
                               ELSE ?
                           END,
                           thread_target_id = COALESCE(?, thread_target_id),
                           updated_at = ?,
                           closed_at = ?
                     WHERE binding_id = ?
                    """,
                    (
                        normalized_repo_id,
                        normalized_pr_state,
                        normalized_head_branch,
                        normalized_head_branch,
                        normalized_base_branch,
                        normalized_base_branch,
                        normalized_thread_target_id,
                        timestamp,
                        closed_at,
                        str(row["binding_id"]),
                    ),
                )
            refreshed = self._load_binding_row(
                conn,
                provider=normalized_provider,
                repo_slug=normalized_repo_slug,
                pr_number=normalized_pr_number,
            )
        if refreshed is None:
            raise RuntimeError("PR binding row missing after upsert")
        return _binding_from_row(refreshed)

    def get_binding_by_pr(
        self,
        *,
        provider: str,
        repo_slug: str,
        pr_number: int,
    ) -> Optional[PrBinding]:
        normalized_provider = _normalize_text(provider)
        normalized_repo_slug = _normalize_text(repo_slug)
        normalized_pr_number = _normalize_int(pr_number, field_name="pr_number")
        if normalized_provider is None or normalized_repo_slug is None:
            return None
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_binding_row(
                conn,
                provider=normalized_provider,
                repo_slug=normalized_repo_slug,
                pr_number=normalized_pr_number,
            )
        return _binding_from_row(row) if row is not None else None

    def find_active_binding_for_branch(
        self,
        *,
        provider: str,
        repo_slug: str,
        branch_name: str,
    ) -> Optional[PrBinding]:
        normalized_provider = _normalize_text(provider)
        normalized_repo_slug = _normalize_text(repo_slug)
        normalized_branch_name = _normalize_text(branch_name)
        if (
            normalized_provider is None
            or normalized_repo_slug is None
            or normalized_branch_name is None
        ):
            return None
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_pr_bindings
                 WHERE provider = ?
                   AND repo_slug = ?
                   AND head_branch = ?
                   AND pr_state IN (?, ?)
                 ORDER BY updated_at DESC, pr_number DESC
                 LIMIT 1
                """,
                (
                    normalized_provider,
                    normalized_repo_slug,
                    normalized_branch_name,
                    *_ACTIVE_PR_STATES,
                ),
            ).fetchone()
        return _binding_from_row(row) if row is not None else None

    def list_bindings(
        self,
        *,
        provider: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_id: Optional[str] = None,
        pr_state: Optional[str] = None,
        head_branch: Optional[str] = None,
        thread_target_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[PrBinding]:
        resolved_limit = _normalize_limit(limit, default=50)
        if resolved_limit <= 0:
            return []

        where_clauses = ["1 = 1"]
        params: list[Any] = []

        normalized_provider = _normalize_text(provider)
        if normalized_provider is not None:
            where_clauses.append("provider = ?")
            params.append(normalized_provider)

        normalized_repo_slug = _normalize_text(repo_slug)
        if normalized_repo_slug is not None:
            where_clauses.append("repo_slug = ?")
            params.append(normalized_repo_slug)

        normalized_repo_id = _normalize_text(repo_id)
        if normalized_repo_id is not None:
            where_clauses.append("repo_id = ?")
            params.append(normalized_repo_id)

        if pr_state is not None:
            where_clauses.append("pr_state = ?")
            params.append(_normalize_state(pr_state))

        normalized_head_branch = _normalize_text(head_branch)
        if normalized_head_branch is not None:
            where_clauses.append("head_branch = ?")
            params.append(normalized_head_branch)

        normalized_thread_target_id = _normalize_text(thread_target_id)
        if normalized_thread_target_id is not None:
            where_clauses.append("thread_target_id = ?")
            params.append(normalized_thread_target_id)

        query = f"""
            SELECT *
              FROM orch_pr_bindings
             WHERE {" AND ".join(where_clauses)}
             ORDER BY updated_at DESC, created_at DESC, pr_number DESC
             LIMIT ?
        """
        params.append(resolved_limit)

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_binding_from_row(row) for row in rows]

    def attach_thread_target(
        self,
        *,
        provider: str,
        repo_slug: str,
        pr_number: int,
        thread_target_id: str,
    ) -> Optional[PrBinding]:
        normalized_provider = _normalize_text(provider)
        normalized_repo_slug = _normalize_text(repo_slug)
        normalized_pr_number = _normalize_int(pr_number, field_name="pr_number")
        normalized_thread_target_id = _normalize_text(thread_target_id)
        if normalized_provider is None:
            raise ValueError("provider is required")
        if normalized_repo_slug is None:
            raise ValueError("repo_slug is required")
        if normalized_thread_target_id is None:
            raise ValueError("thread_target_id is required")

        timestamp = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            cursor = conn.execute(
                """
                UPDATE orch_pr_bindings
                   SET thread_target_id = ?,
                       updated_at = ?
                 WHERE provider = ?
                   AND repo_slug = ?
                   AND pr_number = ?
                """,
                (
                    normalized_thread_target_id,
                    timestamp,
                    normalized_provider,
                    normalized_repo_slug,
                    normalized_pr_number,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = self._load_binding_row(
                conn,
                provider=normalized_provider,
                repo_slug=normalized_repo_slug,
                pr_number=normalized_pr_number,
            )
        return _binding_from_row(row) if row is not None else None

    def close_binding(
        self,
        *,
        provider: str,
        repo_slug: str,
        pr_number: int,
        pr_state: str = "closed",
    ) -> Optional[PrBinding]:
        normalized_provider = _normalize_text(provider)
        normalized_repo_slug = _normalize_text(repo_slug)
        normalized_pr_number = _normalize_int(pr_number, field_name="pr_number")
        normalized_pr_state = _normalize_state(pr_state)
        if normalized_provider is None:
            raise ValueError("provider is required")
        if normalized_repo_slug is None:
            raise ValueError("repo_slug is required")
        if normalized_pr_state not in _TERMINAL_PR_STATES:
            raise ValueError("pr_state must be 'closed' or 'merged'")

        timestamp = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            cursor = conn.execute(
                """
                UPDATE orch_pr_bindings
                   SET pr_state = ?,
                       updated_at = ?,
                       closed_at = ?
                 WHERE provider = ?
                   AND repo_slug = ?
                   AND pr_number = ?
                """,
                (
                    normalized_pr_state,
                    timestamp,
                    timestamp,
                    normalized_provider,
                    normalized_repo_slug,
                    normalized_pr_number,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = self._load_binding_row(
                conn,
                provider=normalized_provider,
                repo_slug=normalized_repo_slug,
                pr_number=normalized_pr_number,
            )
        return _binding_from_row(row) if row is not None else None

    def _load_binding_row(
        self,
        conn: sqlite3.Connection,
        *,
        provider: str,
        repo_slug: str,
        pr_number: int,
    ) -> Optional[sqlite3.Row]:
        row = conn.execute(
            """
            SELECT *
              FROM orch_pr_bindings
             WHERE provider = ?
               AND repo_slug = ?
               AND pr_number = ?
             LIMIT 1
            """,
            (provider, repo_slug, pr_number),
        ).fetchone()
        return cast(Optional[sqlite3.Row], row)


__all__ = ["PrBinding", "PrBindingStore"]
