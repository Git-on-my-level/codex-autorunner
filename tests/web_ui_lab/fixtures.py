from __future__ import annotations

from copy import deepcopy
from typing import Any

from tests.web_ui_lab.scenario_models import SeedFixtureKind

JsonDict = dict[str, Any]


def build_fixture_payload(fixture_kind: SeedFixtureKind) -> JsonDict:
    """Return a deterministic route/read-model payload bundle for one seed state."""

    builders = {
        SeedFixtureKind.EMPTY_HUB: _empty_hub,
        SeedFixtureKind.SEEDED_REPO_WORKTREE_TICKET: _seeded_repo_worktree_ticket,
        SeedFixtureKind.CHAT_LIST_DETAIL: _chat_list_detail,
        SeedFixtureKind.LARGE_LIST_WINDOWING: _large_list_windowing,
        SeedFixtureKind.PMA_PENDING: _pma_pending,
        SeedFixtureKind.PMA_RUNNING: _pma_running,
        SeedFixtureKind.PMA_FINAL: _pma_final,
    }
    return builders[fixture_kind]()


def _empty_hub() -> JsonDict:
    return {
        "repos": [],
        "worktrees": [],
        "tickets": [],
        "runs": [],
        "chats": [],
        "contextspace_docs": [],
        "artifacts": [],
        "settings": {
            "session": {
                "model_overrides": {},
                "effort_override": "",
                "approval_policy": "on-request",
                "sandbox_mode": "workspace-write",
            },
            "agents": [
                {"id": "codex", "name": "Codex", "capabilities": ["chat", "tickets"]}
            ],
            "model_catalogs": {
                "codex": [{"id": "gpt-5.3-codex", "label": "GPT-5.3 Codex"}]
            },
        },
    }


def _seeded_repo_worktree_ticket() -> JsonDict:
    payload = _empty_hub()
    payload.update(
        {
            "repos": [
                {
                    "id": "smoke-repo",
                    "name": "smoke-repo",
                    "path": "/tmp/smoke-repo",
                    "status": "idle",
                    "default_branch": "main",
                    "worktree_count": 1,
                    "active_runs": 0,
                    "open_tickets": 1,
                    "last_activity_at": "2026-05-01T10:00:00Z",
                    "has_car_state": True,
                }
            ],
            "worktrees": [
                {
                    "id": "smoke-repo--review",
                    "repo_id": "smoke-repo",
                    "name": "smoke-repo--review",
                    "path": "/tmp/smoke-repo-review",
                    "branch": "review",
                    "status": "idle",
                    "active_runs": 0,
                    "open_tickets": 1,
                    "last_activity_at": "2026-05-01T10:05:00Z",
                    "has_car_state": True,
                }
            ],
            "tickets": [_ticket_summary()],
            "contextspace_docs": [
                {
                    "id": "active_context",
                    "name": "active_context.md",
                    "kind": "active_context",
                    "content": "# Durable shared context\n\nFixture context.",
                    "updated_at": "2026-05-01T10:02:00Z",
                    "is_pinned": True,
                }
            ],
        }
    )
    return payload


def _chat_list_detail() -> JsonDict:
    payload = _seeded_repo_worktree_ticket()
    payload["chats"] = [
        {
            "thread_target_id": "chat-smoke-1",
            "title": "Smoke fixture chat",
            "status": "running",
            "agent_id": "codex",
            "repo_id": "smoke-repo",
            "worktree_id": "smoke-repo--review",
            "current_ticket_id": "TICKET-350-smoke-fixture",
            "ticket_done": False,
            "flow_type": "ticket_flow",
            "last_activity_at": "2026-05-01T10:10:00Z",
            "progress_percent": 42,
        },
        {
            "thread_target_id": "chat-unknown-status",
            "title": "Unknown status should normalize",
            "status": "mystery-new-state",
            "agent_id": "codex",
            "repo_id": "smoke-repo",
            "last_activity_at": "2026-05-01T10:09:00Z",
        },
    ]
    return payload


def _large_list_windowing() -> JsonDict:
    payload = _seeded_repo_worktree_ticket()
    payload["tickets"] = [
        {
            **_ticket_summary(
                number=number, status="done" if number % 5 == 0 else "idle"
            ),
            "id": f"TICKET-{number:03d}-smoke-fixture",
            "path": f".codex-autorunner/tickets/TICKET-{number:03d}-smoke-fixture.md",
        }
        for number in range(350, 470)
    ]
    return payload


def _pma_pending() -> JsonDict:
    payload = _seeded_repo_worktree_ticket()
    payload["runs"] = [
        _run_progress(
            status="waiting", phase="queued", queue_depth=1, progress_percent=0
        )
    ]
    return payload


def _pma_running() -> JsonDict:
    payload = _chat_list_detail()
    payload["runs"] = [
        _run_progress(
            status="running", phase="implementation", queue_depth=0, progress_percent=46
        )
    ]
    return payload


def _pma_final() -> JsonDict:
    payload = _chat_list_detail()
    payload["tickets"] = [_ticket_summary(status="done", done=True)]
    payload["runs"] = [
        _run_progress(
            status="done",
            phase="final",
            queue_depth=0,
            progress_percent=100,
            terminal=True,
        )
    ]
    payload["artifacts"] = [
        {
            "id": "final-report",
            "kind": "final_report",
            "title": "Final report",
            "summary": "Scenario completed.",
            "url": "/artifacts/final-report.md",
            "created_at": "2026-05-01T10:30:00Z",
        }
    ]
    return payload


def _ticket_summary(
    *,
    number: int = 350,
    status: str = "idle",
    done: bool = False,
) -> JsonDict:
    return {
        "id": f"TICKET-{number:03d}-smoke-fixture",
        "number": number,
        "title": "TICKET-350-smoke-fixture",
        "status": status,
        "done": done,
        "workspace_kind": "worktree",
        "workspace_id": "smoke-repo--review",
        "workspace_path": "/tmp/smoke-repo-review",
        "repo_id": "smoke-repo",
        "worktree_id": "smoke-repo--review",
        "path": f".codex-autorunner/tickets/TICKET-{number:03d}-smoke-fixture.md",
        "ticket_path": f".codex-autorunner/tickets/TICKET-{number:03d}-smoke-fixture.md",
        "agent_id": "codex",
        "chat_key": "chat-smoke-1",
        "run_id": "run-smoke-ticket-flow",
        "updated_at": "2026-05-01T10:08:00Z",
        "duration_seconds": 120,
        "frontmatter": {
            "title": "TICKET-350-smoke-fixture",
            "agent": "codex",
            "done": done,
        },
        "body": "## Goal\nExercise the Web Hub UI scenario harness.",
        "errors": [],
    }


def _run_progress(
    *,
    status: str,
    phase: str,
    queue_depth: int,
    progress_percent: int,
    terminal: bool = False,
) -> JsonDict:
    return {
        "id": "run-smoke-ticket-flow",
        "chat_id": "chat-smoke-1",
        "status": status,
        "work_status": status,
        "terminal": terminal,
        "stream_should_close": terminal,
        "phase": phase,
        "queue_depth": queue_depth,
        "progress_percent": progress_percent,
        "started_at": "2026-05-01T10:00:00Z",
        "last_event_id": 3,
        "last_event_at": "2026-05-01T10:15:00Z",
        "ticket_id": "TICKET-350-smoke-fixture",
        "resource_kind": "worktree",
        "resource_id": "smoke-repo--review",
        "repo_id": "smoke-repo",
        "worktree_id": "smoke-repo--review",
    }


def clone_fixture_payload(fixture_kind: SeedFixtureKind) -> JsonDict:
    return deepcopy(build_fixture_payload(fixture_kind))
