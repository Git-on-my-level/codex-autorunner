"""Shared helpers for hub web route tests.

Consolidates database seeding and assertion utilities used across
the split hub test modules (repo list, destination, channel directory).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from codex_autorunner.core.flows import FlowEventType, FlowRunStatus, FlowStore
from codex_autorunner.core.git_utils import run_git


def init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(["add", "README.md"], path, check=True)
    run_git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        path,
        check=True,
    )


def write_discord_binding_rows(db_path: Path, rows: list[dict]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    channel_id TEXT PRIMARY KEY,
                    guild_id TEXT,
                    workspace_path TEXT,
                    repo_id TEXT,
                    resource_kind TEXT,
                    resource_id TEXT,
                    pma_enabled INTEGER,
                    agent TEXT,
                    agent_profile TEXT,
                    updated_at TEXT
                )
                """
            )
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO channel_bindings (
                        channel_id,
                        guild_id,
                        workspace_path,
                        repo_id,
                        resource_kind,
                        resource_id,
                        pma_enabled,
                        agent,
                        agent_profile,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id) DO UPDATE SET
                        guild_id=excluded.guild_id,
                        workspace_path=excluded.workspace_path,
                        repo_id=excluded.repo_id,
                        resource_kind=excluded.resource_kind,
                        resource_id=excluded.resource_id,
                        pma_enabled=excluded.pma_enabled,
                        agent=excluded.agent,
                        agent_profile=excluded.agent_profile,
                        updated_at=excluded.updated_at
                    """,
                    (
                        row.get("channel_id"),
                        row.get("guild_id"),
                        row.get("workspace_path"),
                        row.get("repo_id"),
                        row.get("resource_kind"),
                        row.get("resource_id"),
                        row.get("pma_enabled"),
                        row.get("agent"),
                        row.get("agent_profile"),
                        row.get("updated_at"),
                    ),
                )
    finally:
        conn.close()


def write_telegram_topic_rows(
    db_path: Path,
    *,
    topics: list[dict],
    scopes: list[dict],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topics (
                    topic_key TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    workspace_path TEXT,
                    repo_id TEXT,
                    active_thread_id TEXT,
                    payload_json TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topic_scopes (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (chat_id, thread_id)
                )
                """
            )
            for row in topics:
                payload_json = row.get("payload_json")
                payload_text = (
                    json.dumps(payload_json) if isinstance(payload_json, dict) else "{}"
                )
                conn.execute(
                    """
                    INSERT INTO telegram_topics (
                        topic_key,
                        chat_id,
                        thread_id,
                        scope,
                        workspace_path,
                        repo_id,
                        active_thread_id,
                        payload_json,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(topic_key) DO UPDATE SET
                        chat_id=excluded.chat_id,
                        thread_id=excluded.thread_id,
                        scope=excluded.scope,
                        workspace_path=excluded.workspace_path,
                        repo_id=excluded.repo_id,
                        active_thread_id=excluded.active_thread_id,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        row.get("topic_key"),
                        row.get("chat_id"),
                        row.get("thread_id"),
                        row.get("scope"),
                        row.get("workspace_path"),
                        row.get("repo_id"),
                        row.get("active_thread_id"),
                        payload_text,
                        row.get("updated_at"),
                    ),
                )
            for scope in scopes:
                conn.execute(
                    """
                    INSERT INTO telegram_topic_scopes (
                        chat_id,
                        thread_id,
                        scope,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                        scope=excluded.scope,
                        updated_at=excluded.updated_at
                    """,
                    (
                        scope.get("chat_id"),
                        scope.get("thread_id"),
                        scope.get("scope"),
                        scope.get("updated_at"),
                    ),
                )
    finally:
        conn.close()


def write_app_server_threads(path: Path, threads: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "threads": threads}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_usage_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def seed_flow_run(
    repo_root: Path,
    *,
    run_id: str,
    status: FlowRunStatus,
    diff_events: list[dict],
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.create_flow_run(run_id, "ticket_flow", input_data={})
        store.update_flow_run_status(
            run_id,
            status,
            state={},
            started_at=started_at,
            finished_at=finished_at,
        )
        for index, payload in enumerate(diff_events, start=1):
            store.create_event(
                event_id=f"{run_id}-diff-{index}",
                run_id=run_id,
                event_type=FlowEventType.DIFF_UPDATED,
                data=payload,
            )


def assert_repo_canonical_state_v1(repo_entry: dict) -> None:
    canonical = repo_entry.get("canonical_state_v1") or {}
    assert canonical.get("schema_version") == 1
    assert canonical.get("repo_id") == repo_entry["id"]
    assert Path(str(canonical.get("repo_root") or "")).name == repo_entry["id"]
    assert canonical.get("ingest_source") == "ticket_files"
    assert isinstance(canonical.get("recommended_actions"), list)
    assert canonical.get("recommendation_confidence") in {"high", "medium", "low"}
    assert canonical.get("observed_at")
    assert canonical.get("recommendation_generated_at")
    freshness = canonical.get("freshness") or {}
    assert freshness.get("generated_at")
    assert freshness.get("recency_basis")
    assert freshness.get("basis_at")
