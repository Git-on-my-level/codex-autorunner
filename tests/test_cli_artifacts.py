from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.artifact_delivery import ArtifactDeliveryService
from codex_autorunner.core.config import CONFIG_FILENAME
from codex_autorunner.core.orchestration.bindings import OrchestrationBindingStore
from tests.conftest import write_test_config

runner = CliRunner()


def _write_hub_with_repo(
    hub_root: Path, repo_root: Path, repo_id: str, *, kind: str = "base"
) -> None:
    write_test_config(
        hub_root / CONFIG_FILENAME,
        {"mode": "hub", "hub": {"manifest": ".codex-autorunner/manifest.yml"}},
    )
    write_test_config(
        hub_root / ".codex-autorunner" / "manifest.yml",
        {
            "version": 2,
            "repos": [
                {
                    "id": repo_id,
                    "path": repo_root.relative_to(hub_root).as_posix(),
                    "kind": kind,
                    "enabled": True,
                    "auto_run": False,
                }
            ],
        },
    )


def _write_discord_channel_binding(
    hub_root: Path, *, channel_id: str, workspace_path: Path, repo_id: str
) -> None:
    db_path = hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_bindings (
                channel_id TEXT PRIMARY KEY,
                workspace_path TEXT,
                repo_id TEXT,
                resource_kind TEXT,
                resource_id TEXT
            )
            """)
        conn.execute(
            """
            INSERT OR REPLACE INTO channel_bindings (
                channel_id, workspace_path, repo_id, resource_kind, resource_id
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (channel_id, str(workspace_path), repo_id, "repo", repo_id),
        )


def _write_telegram_topic_binding(
    hub_root: Path, *, topic_key: str, workspace_path: Path, repo_id: str
) -> None:
    db_path = hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    chat_id, thread_id = topic_key.split(":", 1)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_topics (
                topic_key TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                scope TEXT,
                workspace_path TEXT,
                repo_id TEXT
            )
            """)
        conn.execute(
            """
            INSERT OR REPLACE INTO telegram_topics (
                topic_key, chat_id, thread_id, scope, workspace_path, repo_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                topic_key,
                int(chat_id),
                int(thread_id),
                None,
                str(workspace_path),
                repo_id,
            ),
        )


def test_artifacts_send_list_inspect_retry_cancel(tmp_path: Path) -> None:
    source = tmp_path / "spec.md"
    source.write_text("# spec\n", encoding="utf-8")

    send_result = runner.invoke(
        app,
        [
            "artifacts",
            "send",
            str(source),
            "--root",
            str(tmp_path),
            "--to",
            "explicit",
            "--surface",
            "discord",
            "--conversation",
            "channel:123",
            "--workspace-scope",
            "repo:/example",
            "--json",
        ],
    )

    assert send_result.exit_code == 0, send_result.output
    payload = json.loads(send_result.output)
    delivery_id = payload["delivery_id"]
    assert payload["state"] == "pending"
    assert payload["artifact"]["filename"] == "spec.md"

    list_result = runner.invoke(
        app,
        ["artifacts", "list", "--root", str(tmp_path), "--json"],
    )
    assert list_result.exit_code == 0, list_result.output
    rows = json.loads(list_result.output)
    assert [row["delivery_id"] for row in rows] == [delivery_id]

    service = ArtifactDeliveryService(tmp_path)
    claimed = service.claim_next(target_surface="discord")
    assert claimed is not None
    service.mark_failed(
        delivery_id,
        error="upload failed",
        claim_token=claimed.claim_token,
    )

    retry_result = runner.invoke(
        app, ["artifacts", "retry", delivery_id, "--root", str(tmp_path)]
    )
    assert retry_result.exit_code == 0, retry_result.output
    assert json.loads(retry_result.output)["state"] == "pending"

    cancel_result = runner.invoke(
        app, ["artifacts", "cancel", delivery_id, "--root", str(tmp_path)]
    )
    assert cancel_result.exit_code == 0, cancel_result.output
    assert json.loads(cancel_result.output)["state"] == "cancelled"


def test_artifacts_diagnose_flags_legacy_hub_pending_file(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    repo = tmp_path / "repo"
    stranded = hub / ".codex-autorunner" / "filebox" / "outbox" / "pending" / "spec.md"
    stranded.parent.mkdir(parents=True)
    stranded.write_text("# stranded\n", encoding="utf-8")
    repo.mkdir()

    result = runner.invoke(
        app,
        [
            "artifacts",
            "diagnose",
            "--root",
            str(repo),
            "--sibling-root",
            str(hub),
        ],
    )

    assert result.exit_code == 0, result.output
    findings = json.loads(result.output)["findings"]
    assert any(
        finding["kind"] == "legacy_filebox_pending"
        and finding["filename"] == "spec.md"
        and finding["root"] == str(hub.resolve())
        for finding in findings
    )


def test_artifacts_diagnose_flags_stale_unclaimed_pending_delivery(
    tmp_path: Path,
) -> None:
    source = tmp_path / "pending.txt"
    source.write_text("pending\n", encoding="utf-8")
    service = ArtifactDeliveryService(tmp_path)
    intent = service.enqueue_file(
        source,
        target_surface="web",
        target_conversation_key="managed_thread:abc",
    )
    with sqlite3.connect(service.store.db_path) as conn:
        conn.execute(
            """
            UPDATE delivery_intents
               SET created_at = ?, updated_at = ?
             WHERE delivery_id = ?
            """,
            (
                "2000-01-01T00:00:00Z",
                "2000-01-01T00:00:00Z",
                intent.delivery_id,
            ),
        )

    result = runner.invoke(
        app,
        [
            "artifacts",
            "diagnose",
            "--root",
            str(tmp_path),
            "--stale-after-seconds",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    findings = json.loads(result.output)["findings"]
    assert any(
        finding["kind"] == "stale_unclaimed_pending_delivery"
        and finding["delivery_id"] == intent.delivery_id
        for finding in findings
    )


def test_artifacts_diagnose_flags_weak_web_sent_receipt(tmp_path: Path) -> None:
    source = tmp_path / "sent.txt"
    source.write_text("sent\n", encoding="utf-8")
    service = ArtifactDeliveryService(tmp_path)
    intent = service.enqueue_file(
        source,
        target_surface="web",
        target_conversation_key="managed_thread:abc",
    )
    claimed = service.claim_next(
        target_surface="web",
        target_conversation_key="managed_thread:abc",
    )
    assert claimed is not None
    service.mark_sending(intent.delivery_id, claim_token=claimed.claim_token)
    service.mark_sent(
        intent.delivery_id,
        receipt={"surface": "web", "delivery_id": intent.delivery_id},
        claim_token=claimed.claim_token,
    )

    result = runner.invoke(
        app,
        ["artifacts", "diagnose", "--root", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    findings = json.loads(result.output)["findings"]
    assert any(
        finding["kind"] == "weak_web_sent_receipt"
        and finding["delivery_id"] == intent.delivery_id
        for finding in findings
    )


def test_artifacts_import_legacy_queues_scoped_delivery_records(tmp_path: Path) -> None:
    legacy = tmp_path / ".codex-autorunner" / "filebox" / "outbox" / "report.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("report\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "artifacts",
            "import-legacy",
            "--root",
            str(tmp_path),
            "--to",
            "explicit",
            "--surface",
            "discord",
            "--conversation",
            "channel:123",
            "--workspace-scope",
            f"repo:{tmp_path}",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["target_surface"] == "discord"
    assert rows[0]["target_conversation_key"] == "channel:123"
    assert rows[0]["metadata"]["legacy_filebox_source"] == "outbox"


def test_artifacts_send_defaults_to_unique_bound_chat(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "repo"
    repo_root.mkdir(parents=True)
    _write_hub_with_repo(hub_root, repo_root, "repo")
    _write_discord_channel_binding(
        hub_root,
        channel_id="123",
        workspace_path=repo_root,
        repo_id="repo",
    )
    for key in (
        "CAR_ARTIFACT_TARGET_SURFACE",
        "CAR_ARTIFACT_TARGET_CONVERSATION_KEY",
        "CAR_ARTIFACT_WORKSPACE_SCOPE",
    ):
        monkeypatch.delenv(key, raising=False)
    source = repo_root / "report.txt"
    source.write_text("report\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "artifacts",
            "send",
            str(source),
            "--root",
            str(repo_root),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["target_surface"] == "discord"
    assert payload["target_conversation_key"] == "channel:123"
    assert payload["workspace_scope"] == f"repo:{repo_root.resolve()}"


def test_artifacts_send_fails_when_bound_chat_is_ambiguous(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "repo"
    repo_root.mkdir(parents=True)
    _write_hub_with_repo(hub_root, repo_root, "repo")
    _write_discord_channel_binding(
        hub_root,
        channel_id="123",
        workspace_path=repo_root,
        repo_id="repo",
    )
    _write_telegram_topic_binding(
        hub_root,
        topic_key="456:789",
        workspace_path=repo_root,
        repo_id="repo",
    )
    for key in (
        "CAR_ARTIFACT_TARGET_SURFACE",
        "CAR_ARTIFACT_TARGET_CONVERSATION_KEY",
        "CAR_ARTIFACT_WORKSPACE_SCOPE",
    ):
        monkeypatch.delenv(key, raising=False)
    source = repo_root / "report.txt"
    source.write_text("report\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["artifacts", "send", str(source), "--root", str(repo_root)],
    )

    assert result.exit_code != 0
    assert "Current artifact target is ambiguous" in result.output
    assert "discord:channel:123" in result.output
    assert "telegram:chat:456/thread:789" in result.output


def test_artifacts_send_defaults_to_worktree_orchestration_binding(
    tmp_path: Path, monkeypatch
) -> None:
    hub_root = tmp_path / "hub"
    worktree_root = hub_root / "worktrees" / "repo-feature"
    worktree_root.mkdir(parents=True)
    _write_hub_with_repo(hub_root, worktree_root, "repo-feature", kind="worktree")
    OrchestrationBindingStore(hub_root).upsert_binding(
        surface_kind="discord",
        surface_key="worktree-channel",
        thread_target_id="thread-worktree",
        resource_kind="worktree",
        resource_id="repo-feature",
    )
    for key in (
        "CAR_ARTIFACT_TARGET_SURFACE",
        "CAR_ARTIFACT_TARGET_CONVERSATION_KEY",
        "CAR_ARTIFACT_WORKSPACE_SCOPE",
    ):
        monkeypatch.delenv(key, raising=False)
    source = worktree_root / "report.txt"
    source.write_text("report\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "artifacts",
            "send",
            str(source),
            "--root",
            str(worktree_root),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["target_surface"] == "discord"
    assert payload["target_conversation_key"] == "channel:worktree-channel"
    assert payload["workspace_scope"] == f"repo:{worktree_root.resolve()}"
