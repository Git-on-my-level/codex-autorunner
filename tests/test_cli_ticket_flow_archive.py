from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.cli import app
from codex_autorunner.core.flows.archive_helpers import archive_flow_run_artifacts
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.force_attestation import FORCE_ATTESTATION_REQUIRED_ERROR
from codex_autorunner.core.pma_thread_store import PmaThreadStore

runner = CliRunner()


def _seed_repo_run(
    repo_root: Path,
    run_id: str,
    status: FlowRunStatus,
    *,
    state: dict | None = None,
    error_message: str | None = None,
) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={
                "workspace_root": str(repo_root),
                "runs_dir": ".codex-autorunner/runs",
            },
            state=state or {},
            metadata={},
        )
        store.update_flow_run_status(run_id, status, error_message=error_message)


def _seed_ticket(repo_root: Path) -> None:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        '---\nticket_id: "tkt_archive001"\nagent: user\ndone: false\n---\n\nStatus ticket\n',
        encoding="utf-8",
    )


def _seed_contextspace(repo_root: Path) -> None:
    context_dir = repo_root / ".codex-autorunner" / "contextspace"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "active_context.md").write_text("Active context\n", encoding="utf-8")
    (context_dir / "decisions.md").write_text("Decision log\n", encoding="utf-8")
    (context_dir / "notes.md").write_text("Scratch note\n", encoding="utf-8")


def _setup_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_hub_files(tmp_path, force=True)
    seed_repo_files(repo_root, git_required=False)
    return repo_root


def test_ticket_flow_archive_moves_run_artifacts_and_deletes_run(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_hub_files(tmp_path, force=True)
    seed_repo_files(repo_root, git_required=False)

    run_id = "99999999-9999-9999-9999-999999999999"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.STOPPED)
    _seed_ticket(repo_root)
    _seed_contextspace(repo_root)
    github_context_dir = repo_root / ".codex-autorunner" / "github_context"
    github_context_dir.mkdir(parents=True, exist_ok=True)
    (github_context_dir / "issue-123.md").write_text(
        "Issue context\n", encoding="utf-8"
    )

    run_dir = (
        repo_root / ".codex-autorunner" / "runs" / run_id / "dispatch_history" / "0001"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "DISPATCH.md").write_text(
        "---\nmode: pause\n---\n\nhello\n", encoding="utf-8"
    )
    live_flow_dir = repo_root / ".codex-autorunner" / "flows" / run_id / "chat"
    live_flow_dir.mkdir(parents=True, exist_ok=True)
    (live_flow_dir / "outbound.jsonl").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "archive",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id
    assert payload["archived_runs"] is True
    assert payload["archived_tickets"] == 1
    assert payload["archived_contextspace"] is True
    assert payload["deleted_run"] is True

    archived_root = (
        repo_root / ".codex-autorunner" / "archive" / "runs" / run_id / "archived_runs"
    )
    assert archived_root.exists()
    assert (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / run_id
        / "archived_tickets"
        / "TICKET-001.md"
    ).exists()
    assert (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / run_id
        / "contextspace"
        / "active_context.md"
    ).read_text(encoding="utf-8") == "Active context\n"
    assert (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / run_id
        / "contextspace"
        / "decisions.md"
    ).read_text(encoding="utf-8") == "Decision log\n"
    assert (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / run_id
        / "contextspace"
        / "notes.md"
    ).read_text(encoding="utf-8") == "Scratch note\n"
    assert (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / run_id
        / "flow_state"
        / "chat"
        / "outbound.jsonl"
    ).read_text(encoding="utf-8") == "{}"
    assert (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / run_id
        / "github_context"
        / "issue-123.md"
    ).read_text(encoding="utf-8") == "Issue context\n"
    assert not (repo_root / ".codex-autorunner" / "flows" / run_id).exists()
    assert not (repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md").exists()
    assert (
        repo_root / ".codex-autorunner" / "contextspace" / "active_context.md"
    ).read_text(encoding="utf-8") == ""
    assert (
        repo_root / ".codex-autorunner" / "contextspace" / "decisions.md"
    ).read_text(encoding="utf-8") == ""
    assert (repo_root / ".codex-autorunner" / "contextspace" / "spec.md").read_text(
        encoding="utf-8"
    ) == ""
    assert not (repo_root / ".codex-autorunner" / "contextspace" / "notes.md").exists()

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.initialize()
        assert store.get_flow_run(run_id) is None


def test_ticket_flow_archive_also_archives_ticket_flow_pma_threads(
    tmp_path: Path,
) -> None:
    repo_root = _setup_repo(tmp_path)
    run_id = "12121212-1212-1212-1212-121212121212"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.STOPPED)

    store = PmaThreadStore(tmp_path)
    matching = store.create_thread(
        "codex",
        repo_root.resolve(),
        repo_id="repo",
        name="ticket-flow:codex",
        metadata={
            "thread_kind": "ticket_flow",
            "flow_type": "ticket_flow",
            "run_id": run_id,
        },
    )
    legacy = store.create_thread(
        "codex",
        repo_root.resolve(),
        repo_id="repo",
        name="ticket-flow:codex",
    )
    other_run = store.create_thread(
        "codex",
        repo_root.resolve(),
        repo_id="repo",
        name="ticket-flow:codex",
        metadata={
            "thread_kind": "ticket_flow",
            "flow_type": "ticket_flow",
            "run_id": "34343434-3434-3434-3434-343434343434",
        },
    )
    non_ticket_flow = store.create_thread(
        "codex",
        repo_root.resolve(),
        repo_id="repo",
        name="pma:codex",
    )

    run_dir = repo_root / ".codex-autorunner" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "archive",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["archived_pma_threads"] == 2
    assert sorted(payload["archived_pma_thread_ids"]) == sorted(
        [
            matching["managed_thread_id"],
            legacy["managed_thread_id"],
        ]
    )
    assert store.get_thread(matching["managed_thread_id"])["status"] == "archived"
    assert store.get_thread(legacy["managed_thread_id"])["status"] == "archived"
    assert store.get_thread(other_run["managed_thread_id"])["status"] == "active"
    assert store.get_thread(non_ticket_flow["managed_thread_id"])["status"] == "active"


def test_ticket_flow_archive_skips_pma_archival_without_hub_manifest(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)

    run_id = "56565656-5656-5656-5656-565656565656"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.STOPPED)
    run_dir = repo_root / ".codex-autorunner" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    payload = archive_flow_run_artifacts(
        repo_root,
        run_id=run_id,
        force=False,
        delete_run=True,
    )
    assert payload["archived_pma_threads"] == 0
    assert payload["archived_pma_threads_skipped"] == "hub_manifest_missing"
    assert payload["archived_pma_threads_error"] is None
    assert not (repo_root / ".codex-autorunner" / "orchestration.sqlite3").exists()


def test_ticket_flow_archive_tolerates_pma_archive_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _setup_repo(tmp_path)
    run_id = "78787878-7878-7878-7878-787878787878"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.STOPPED)
    run_dir = repo_root / ".codex-autorunner" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    class _BrokenPmaStore(PmaThreadStore):
        def archive_thread(self, managed_thread_id: str) -> None:
            raise RuntimeError(f"boom:{managed_thread_id}")

    store = _BrokenPmaStore(tmp_path)
    thread = store.create_thread(
        "codex",
        repo_root.resolve(),
        repo_id="repo",
        name="ticket-flow:codex",
        metadata={
            "thread_kind": "ticket_flow",
            "flow_type": "ticket_flow",
            "run_id": run_id,
        },
    )

    monkeypatch.setattr(
        "codex_autorunner.core.flows.archive_helpers.PmaThreadStore",
        _BrokenPmaStore,
    )

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "archive",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["archived_runs"] is True
    assert payload["deleted_run"] is True
    assert payload["archived_pma_threads"] == 0
    assert payload["archived_pma_thread_ids"] == []
    assert (
        payload["archived_pma_threads_error"] == f"boom:{thread['managed_thread_id']}"
    )


def test_ticket_flow_archive_searches_full_ancestor_chain_for_hub_manifest(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    repo_root = hub_root / "a" / "b" / "c" / "d" / "e" / "f" / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)

    run_id = "90909090-9090-9090-9090-909090909090"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.STOPPED)
    run_dir = repo_root / ".codex-autorunner" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    store = PmaThreadStore(hub_root)
    thread = store.create_thread(
        "codex",
        repo_root.resolve(),
        name="ticket-flow:codex",
        metadata={
            "thread_kind": "ticket_flow",
            "flow_type": "ticket_flow",
            "run_id": run_id,
        },
    )

    payload = archive_flow_run_artifacts(
        repo_root,
        run_id=run_id,
        force=False,
        delete_run=True,
    )

    assert payload["archived_pma_threads"] == 1
    assert payload["archived_pma_threads_skipped"] is None
    assert payload["archived_pma_thread_ids"] == [thread["managed_thread_id"]]


def test_ticket_flow_archive_cleans_related_terminal_runs(
    tmp_path: Path,
) -> None:
    repo_root = _setup_repo(tmp_path)
    archived_run_id = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
    stale_run_id = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"
    _seed_repo_run(repo_root, archived_run_id, FlowRunStatus.STOPPED)
    _seed_repo_run(repo_root, stale_run_id, FlowRunStatus.SUPERSEDED)
    _seed_ticket(repo_root)
    _seed_contextspace(repo_root)

    archived_run_dir = repo_root / ".codex-autorunner" / "runs" / archived_run_id
    archived_run_dir.mkdir(parents=True, exist_ok=True)
    stale_run_dir = repo_root / ".codex-autorunner" / "runs" / stale_run_id
    stale_run_dir.mkdir(parents=True, exist_ok=True)
    (stale_run_dir / "reply.txt").write_text("stale payload\n", encoding="utf-8")

    stale_flow_dir = repo_root / ".codex-autorunner" / "flows" / stale_run_id / "chat"
    stale_flow_dir.mkdir(parents=True, exist_ok=True)
    (stale_flow_dir / "events.jsonl").write_text("{}", encoding="utf-8")

    payload = archive_flow_run_artifacts(
        repo_root,
        run_id=archived_run_id,
        force=False,
        delete_run=True,
    )

    related = payload["related_terminal_cleanup"]
    assert related["archived_run_ids"] == [stale_run_id]
    assert related["deleted_run_ids"] == [stale_run_id]
    assert (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / stale_run_id
        / "archived_runs"
        / "reply.txt"
    ).read_text(encoding="utf-8") == "stale payload\n"
    assert (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / stale_run_id
        / "flow_state"
        / "chat"
        / "events.jsonl"
    ).read_text(encoding="utf-8") == "{}"

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.initialize()
        assert store.get_flow_run(archived_run_id) is None
        assert store.get_flow_run(stale_run_id) is None


def test_ticket_flow_archive_tolerates_sibling_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = _setup_repo(tmp_path)
    archived_run_id = "c3c3c3c3-c3c3-c3c3-c3c3-c3c3c3c3c3c3"
    failing_run_id = "d4d4d4d4-d4d4-d4d4-d4d4-d4d4d4d4d4d4"
    _seed_repo_run(repo_root, archived_run_id, FlowRunStatus.STOPPED)
    _seed_repo_run(repo_root, failing_run_id, FlowRunStatus.SUPERSEDED)

    archived_run_dir = repo_root / ".codex-autorunner" / "runs" / archived_run_id
    archived_run_dir.mkdir(parents=True, exist_ok=True)
    failing_run_dir = repo_root / ".codex-autorunner" / "runs" / failing_run_id
    failing_run_dir.mkdir(parents=True, exist_ok=True)

    original_archive_run_scoped_artifacts = archive_flow_run_artifacts.__globals__[
        "_archive_run_scoped_artifacts"
    ]

    def fake_archive_run_scoped_artifacts(
        repo_root_arg: Path,
        *,
        record: object,
    ) -> dict[str, object]:
        if getattr(record, "id", None) == failing_run_id:
            raise PermissionError("permission denied")
        return original_archive_run_scoped_artifacts(repo_root_arg, record=record)

    monkeypatch.setitem(
        archive_flow_run_artifacts.__globals__,
        "_archive_run_scoped_artifacts",
        fake_archive_run_scoped_artifacts,
    )

    payload = archive_flow_run_artifacts(
        repo_root,
        run_id=archived_run_id,
        force=False,
        delete_run=True,
    )

    related = payload["related_terminal_cleanup"]
    assert related["archived_run_ids"] == []
    assert related["deleted_run_ids"] == []
    assert related["failed_run_count"] == 1
    assert related["failed_runs"] == [
        {"run_id": failing_run_id, "error": "permission denied"}
    ]

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.initialize()
        assert store.get_flow_run(archived_run_id) is None
        assert store.get_flow_run(failing_run_id) is not None


def test_ticket_flow_archive_scans_all_active_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = _setup_repo(tmp_path)
    run_id = "91919191-9191-9191-9191-919191919191"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.STOPPED)
    run_dir = repo_root / ".codex-autorunner" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    observed: dict[str, object] = {}
    archived_thread_ids: list[str] = []
    matching_thread_id = "matching-thread"

    def fake_list_threads(
        self: PmaThreadStore,
        *,
        agent: str | None = None,
        status: str | None = None,
        normalized_status: str | None = None,
        repo_id: str | None = None,
        limit: int | None = 200,
    ) -> list[dict[str, object]]:
        observed["agent"] = agent
        observed["status"] = status
        observed["normalized_status"] = normalized_status
        observed["repo_id"] = repo_id
        observed["limit"] = limit
        return [
            {
                "managed_thread_id": "non-ticket-flow-thread",
                "workspace_root": str(repo_root.resolve()),
                "repo_id": None,
                "name": "pma:codex:0",
                "metadata": {},
            },
            {
                "managed_thread_id": matching_thread_id,
                "workspace_root": str(repo_root.resolve()),
                "repo_id": None,
                "name": "ticket-flow:codex",
                "metadata": {
                    "thread_kind": "ticket_flow",
                    "flow_type": "ticket_flow",
                    "run_id": run_id,
                },
            },
        ]

    def fake_archive_thread(
        self: PmaThreadStore,
        managed_thread_id: str,
    ) -> None:
        archived_thread_ids.append(managed_thread_id)

    monkeypatch.setattr(PmaThreadStore, "list_threads", fake_list_threads)
    monkeypatch.setattr(PmaThreadStore, "archive_thread", fake_archive_thread)

    payload = archive_flow_run_artifacts(
        repo_root,
        run_id=run_id,
        force=False,
        delete_run=True,
    )

    assert observed["status"] == "active"
    assert observed["limit"] is None
    assert payload["archived_pma_threads"] == 1
    assert payload["archived_pma_thread_ids"] == [matching_thread_id]
    assert archived_thread_ids == [matching_thread_id]


def test_ticket_flow_archive_dry_run_does_not_modify(tmp_path: Path) -> None:
    repo_root = _setup_repo(tmp_path)

    run_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.FAILED)
    _seed_ticket(repo_root)

    run_dir = repo_root / ".codex-autorunner" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "archive",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["archived_runs"] is False
    assert payload["deleted_run"] is False
    assert run_dir.exists()
    assert (repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md").exists()


def test_ticket_flow_archive_force_requires_attestation(tmp_path: Path) -> None:
    repo_root = _setup_repo(tmp_path)

    run_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.PAUSED)

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "archive",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--force",
        ],
    )

    assert result.exit_code == 1
    assert FORCE_ATTESTATION_REQUIRED_ERROR in result.output


def test_ticket_flow_archive_force_with_attestation_succeeds(tmp_path: Path) -> None:
    repo_root = _setup_repo(tmp_path)

    run_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.PAUSED)

    run_dir = repo_root / ".codex-autorunner" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "archive",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--force",
            "--force-attestation",
            "archive paused run",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id
    assert payload["archived_runs"] is True
    assert payload["deleted_run"] is True


def test_ticket_flow_archive_missing_repo_shows_repo_hint(tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "flow",
                "ticket_flow",
                "archive",
                "--run-id",
                "ffffffff-ffff-ffff-ffff-ffffffffffff",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert (
            "No .git directory found. Specify --repo <worktree-path> to resolve."
            in result.output
        )


def test_ticket_flow_archive_alias_inherits_force_attestation(tmp_path: Path) -> None:
    repo_root = _setup_repo(tmp_path)

    run_id = "abababab-abab-abab-abab-abababababab"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.PAUSED)

    run_dir = repo_root / ".codex-autorunner" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    result = runner.invoke(
        app,
        [
            "ticket-flow",
            "archive",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--force",
            "--force-attestation",
            "archive paused run via alias",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id


def test_ticket_flow_status_outputs_human_readable_status(tmp_path: Path) -> None:
    repo_root = _setup_repo(tmp_path)
    _seed_ticket(repo_root)

    run_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.RUNNING)

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "status",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip()
    assert f"Run id: {run_id}" in result.output
    assert "Status: running" in result.output


def test_ticket_flow_status_outputs_json_payload(tmp_path: Path) -> None:
    repo_root = _setup_repo(tmp_path)
    _seed_ticket(repo_root)

    run_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.PAUSED)

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "status",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip()
    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id
    assert payload["status"] == "paused"
    assert payload["flow_type"] == "ticket_flow"
    assert "worker" in payload
    assert "ticket_progress" in payload
    assert "error_message" in payload
    assert "reason_summary" in payload
    assert "error" in payload
    assert "failure_reason" in payload


def test_ticket_flow_status_outputs_failure_details_in_json(tmp_path: Path) -> None:
    repo_root = _setup_repo(tmp_path)
    _seed_ticket(repo_root)

    run_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    _seed_repo_run(
        repo_root,
        run_id,
        FlowRunStatus.FAILED,
        state={"reason_summary": "docker preflight failed"},
        error_message="Docker preflight failed: missing required binaries: opencode",
    )

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "status",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["reason_summary"] == "docker preflight failed"
    assert (
        payload["error_message"]
        == "Docker preflight failed: missing required binaries: opencode"
    )
    assert payload["failure_reason"] == "docker preflight failed"
    assert (
        payload["error"]
        == "Docker preflight failed: missing required binaries: opencode"
    )
