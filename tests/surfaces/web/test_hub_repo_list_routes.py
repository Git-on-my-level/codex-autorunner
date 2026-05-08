from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.support.web_test_helpers import create_test_hub_supervisor
from tests.surfaces.web._hub_test_support import (
    assert_repo_canonical_state_v1,
    seed_flow_run,
)

from codex_autorunner.core.flows import FlowRunStatus
from codex_autorunner.core.state import RunnerState, save_state
from codex_autorunner.server import create_hub_app

pytestmark = [
    pytest.mark.docker_managed_cleanup,
    pytest.mark.slow,
]


def _write_dispatch_artifact(repo_root: Path, run_id: str, seq: str, name: str) -> None:
    entry = repo_root / ".codex-autorunner" / "runs" / run_id / "dispatch_history" / seq
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "DISPATCH.md").write_text("# Dispatch\n", encoding="utf-8")
    (entry / name).write_text("artifact body\n", encoding="utf-8")


def test_hub_repo_list_includes_ticket_flow_summary_and_run_state(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    repo = supervisor.create_repo("base")

    tickets_dir = repo.path / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text(
        "---\ntitle: First\ngoal: ship it\nagent: codex\ndone: true\n---\n\nbody\n",
        encoding="utf-8",
    )
    (tickets_dir / "TICKET-002.md").write_text(
        "---\ntitle: Second\ngoal: verify it\nagent: codex\ndone: false\n---\n\nbody\n",
        encoding="utf-8",
    )
    seed_flow_run(
        repo.path,
        run_id="run-paused",
        status=FlowRunStatus.PAUSED,
        diff_events=[],
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/repos")
    assert response.status_code == 200

    repo_entry = next(item for item in response.json()["repos"] if item["id"] == "base")
    summary = repo_entry["ticket_flow"]
    assert summary["status"] == "paused"
    assert summary["done_count"] == 1
    assert summary["total_count"] == 2
    assert summary["run_id"] == "run-paused"

    display = repo_entry["ticket_flow_display"]
    assert display["status"] == "paused"
    assert display["done_count"] == 1
    assert display["total_count"] == 2
    assert display["run_id"] == "run-paused"
    assert display["is_active"] is True

    run_state = repo_entry["run_state"] or {}
    assert run_state["run_id"] == "run-paused"
    assert run_state["flow_status"] == "paused"
    assert run_state["recommended_action"]
    assert_repo_canonical_state_v1(repo_entry)
    assert repo_entry["canonical_state_v1"]["represented_run_id"] == "run-paused"


def test_hub_scan_reuses_repo_summary_enrichment(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    repo = supervisor.create_repo("base")

    tickets_dir = repo.path / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text(
        "---\ntitle: First\ngoal: ship it\nagent: codex\ndone: false\n---\n\nbody\n",
        encoding="utf-8",
    )
    seed_flow_run(
        repo.path,
        run_id="run-running",
        status=FlowRunStatus.RUNNING,
        diff_events=[],
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.post("/hub/repos/scan")
    assert response.status_code == 200

    payload = response.json()
    assert payload["last_scan_at"]
    assert isinstance(payload["pinned_parent_repo_ids"], list)

    repo_entry = next(item for item in payload["repos"] if item["id"] == "base")
    summary = repo_entry["ticket_flow"]
    assert summary["status"] == "running"
    assert summary["done_count"] == 0
    assert summary["total_count"] == 1
    assert summary["run_id"] == "run-running"

    display = repo_entry["ticket_flow_display"]
    assert display["status"] == "running"
    assert display["is_active"] is True

    run_state = repo_entry["run_state"] or {}
    assert run_state["run_id"] == "run-running"
    assert run_state["flow_status"] == "running"
    assert_repo_canonical_state_v1(repo_entry)
    assert repo_entry["canonical_state_v1"]["represented_run_id"] == "run-running"


def test_hub_repo_list_includes_live_progress_and_current_run_artifacts(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    repo = supervisor.create_repo("base")
    run_id = "11111111-1111-4111-8111-111111111111"

    tickets_dir = repo.path / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text(
        "---\ntitle: First\ngoal: ship it\nagent: codex\ndone: true\n---\n\nbody\n",
        encoding="utf-8",
    )
    (tickets_dir / "TICKET-002.md").write_text(
        "---\ntitle: Second\ngoal: verify it\nagent: codex\ndone: false\n---\n\nbody\n",
        encoding="utf-8",
    )
    seed_flow_run(
        repo.path,
        run_id=run_id,
        status=FlowRunStatus.RUNNING,
        diff_events=[],
    )
    _write_dispatch_artifact(repo.path, run_id, "0001", "preview.txt")

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/repos")
    assert response.status_code == 200

    repo_entry = next(item for item in response.json()["repos"] if item["id"] == "base")
    assert repo_entry["ticket_flow_display"]["progress_percent"] == 50
    artifacts = repo_entry["current_run_artifacts"]
    assert artifacts[0]["name"] == "preview.txt"
    assert artifacts[0]["url"].endswith(
        f"api/flows/{run_id}/dispatch_history/0001/preview.txt"
    )


def test_hub_repo_list_rewrites_stale_runner_last_run_to_authoritative_flow_run(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    repo = supervisor.create_repo("base")

    tickets_dir = repo.path / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text(
        "---\ntitle: First\ngoal: ship it\nagent: codex\ndone: true\n---\n\nbody\n",
        encoding="utf-8",
    )

    seed_flow_run(
        repo.path,
        run_id="older-failed",
        status=FlowRunStatus.FAILED,
        diff_events=[],
    )
    seed_flow_run(
        repo.path,
        run_id="newer-completed",
        status=FlowRunStatus.COMPLETED,
        diff_events=[],
    )
    save_state(
        repo.path / ".codex-autorunner" / "state.sqlite3",
        RunnerState(
            last_run_id="older-failed",
            status="running",
            last_exit_code=137,
            last_run_started_at="2026-03-10T00:00:00+00:00",
            last_run_finished_at=None,
        ),
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/repos")
    assert response.status_code == 200

    repo_entry = next(item for item in response.json()["repos"] if item["id"] == "base")
    assert repo_entry["last_run_id"] == "newer-completed"
    assert repo_entry["last_exit_code"] is None
    assert repo_entry["ticket_flow"]["run_id"] == "newer-completed"
    assert repo_entry["canonical_state_v1"]["latest_run_id"] == "newer-completed"


def test_hub_ui_exposes_destination_and_channel_directory_controls() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    index_html = (
        repo_root / "src" / "codex_autorunner" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="hub-repo-search"' in index_html
    assert 'id="hub-refresh"' in index_html
    assert 'id="hub-new-repo"' in index_html
    assert 'id="hub-scan"' not in index_html
    assert 'id="hub-quick-scan"' not in index_html
    assert 'id="hub-channel-query"' not in index_html
    assert 'id="hub-channel-search"' not in index_html
    assert 'id="hub-channel-refresh"' not in index_html
    assert "Channel Directory" not in index_html
    assert "Copy Ref copies a channel ref" not in index_html

    static_src = repo_root / "src" / "codex_autorunner" / "static_src"
    hub_dom = (static_src / "hubDomBindings.ts").read_text(encoding="utf-8")
    assert 'action === "set_destination"' in hub_dom
    assert "hubRepoSearchInput" in hub_dom
    hub_modals = (static_src / "hubModals.ts").read_text(encoding="utf-8")
    assert "/hub/repos/${encodeURIComponent(repo.id)}/destination" in hub_modals
    assert "await startHubJob(`/hub/jobs/repos/${repoId}/remove`" in hub_modals
    hub_refresh = (static_src / "hubRefresh.ts").read_text(encoding="utf-8")
    assert "/hub/chat/channels?limit=1000" in hub_refresh
    hub_cards = (static_src / "hubRepoCards.ts").read_text(encoding="utf-8")
    assert "hub-chat-binding-row" in hub_cards
    assert "export function renderReposWithScroll" in hub_cards
    assert "container_name" in hub_modals
    assert "profile" in hub_modals
    assert "workdir" in hub_modals
    assert "env_passthrough" in hub_modals
    assert "body.env =" in hub_modals
    assert "mounts" in hub_modals
    assert "read_only" in hub_modals
    assert 'header.textContent = "Channels"' not in hub_dom
    assert "copy_channel_key" not in hub_dom
    assert "Copied channel ref" not in hub_dom


def test_hub_repo_list_includes_last_run_duration_seconds(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    repo = supervisor.create_repo("base")

    tickets_dir = repo.path / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text(
        "---\ntitle: First\ngoal: ship it\nagent: codex\ndone: true\n---\n\nbody\n",
        encoding="utf-8",
    )

    seed_flow_run(
        repo.path,
        run_id="run-completed",
        status=FlowRunStatus.COMPLETED,
        diff_events=[],
        started_at="2026-03-13T08:00:00Z",
        finished_at="2026-03-13T09:45:00Z",
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/repos")
    assert response.status_code == 200

    repo_entry = next(item for item in response.json()["repos"] if item["id"] == "base")
    assert repo_entry["last_run_duration_seconds"] == 6300.0
    run_state = repo_entry["run_state"] or {}
    assert run_state["duration_seconds"] == 6300.0
