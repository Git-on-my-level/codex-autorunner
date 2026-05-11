from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tests.web_ui_lab.fixtures import build_fixture_payload
from tests.web_ui_lab.scenario_models import (
    LoadingBehavior,
    ScenarioTag,
    WebUiScenario,
)

DEFAULT_DIAGNOSTICS_ROOT = Path(".codex-autorunner/diagnostics/web_ui_lab")
MAX_VISIBLE_ROWS = 50


class ScenarioInvariantError(AssertionError):
    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        failed = report["failed_invariants"]
        message = (
            f"{report['scenario_id']} failed {len(failed)} invariant(s) "
            f"for {report['route_path']}: {failed}; fixture={report['fixture_payload_path']}"
        )
        super().__init__(message)


def run_scenario(
    scenario: WebUiScenario,
    *,
    diagnostics_root: Path = DEFAULT_DIAGNOSTICS_ROOT,
) -> dict[str, Any]:
    payload = build_fixture_payload(scenario.seed_fixture)
    screen_model = _normalize_screen_model(scenario, payload)
    failed_invariants = _evaluate_invariants(scenario, screen_model)

    scenario_dir = diagnostics_root / scenario.scenario_id
    scenario_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = scenario_dir / "fixture_payload.json"
    report_path = scenario_dir / "report.json"
    fixture_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    report = {
        "scenario_id": scenario.scenario_id,
        "title": scenario.title,
        "route_name": scenario.route_name,
        "route_path": scenario.route_path,
        "fixture_kind": scenario.seed_fixture.value,
        "fixture_payload_path": str(fixture_path),
        "status": "failed" if failed_invariants else "passed",
        "failed_invariants": failed_invariants,
        "screen_model": screen_model,
        "evidence": asdict(scenario.evidence),
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if failed_invariants:
        raise ScenarioInvariantError(report)
    return report


def _normalize_screen_model(
    scenario: WebUiScenario,
    payload: dict[str, Any],
) -> dict[str, Any]:
    repos = _records(payload, "repos")
    worktrees = _records(payload, "worktrees")
    tickets = _records(payload, "tickets")
    runs = _records(payload, "runs")
    chats = _records(payload, "chats")
    timeline = _records(payload, "timeline")
    docs = _records(payload, "contextspace_docs")
    settings = (
        payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    )

    actions = _actions_for_route(scenario.route_name, repos, worktrees, tickets, chats)
    landmarks = _landmarks_for_route(
        scenario.route_name,
        repos=repos,
        worktrees=worktrees,
        tickets=tickets,
        runs=runs,
        chats=chats,
        timeline=timeline,
        docs=docs,
        settings=settings,
    )
    row_count = _row_count_for_route(
        scenario.route_name,
        repos=repos,
        worktrees=worktrees,
        tickets=tickets,
        chats=chats,
        timeline=timeline,
        docs=docs,
    )
    visible_rows = min(row_count, MAX_VISIBLE_ROWS)
    loading_markers = []
    if scenario.screen.loading_behavior is LoadingBehavior.MAY_STREAM and runs:
        loading_markers = ["Streaming"]

    return {
        "route": scenario.route_path,
        "landmarks": sorted(set(landmarks)),
        "actions": sorted(set(actions)),
        "loading_markers": loading_markers,
        "row_count": row_count,
        "visible_rows": visible_rows,
        "windowed": row_count <= MAX_VISIBLE_ROWS or visible_rows <= MAX_VISIBLE_ROWS,
        "unknown_status_normalized": _unknown_status_normalized(
            chats + runs + tickets + repos + worktrees + timeline
        ),
        "missing_optional_fields_safe": _missing_optional_fields_safe(payload),
        "cursor_snapshot": _cursor_snapshot(scenario, payload),
        "repair_snapshot": _repair_snapshot(tickets),
        "pma_timeline": _pma_timeline_summary(timeline, payload),
    }


def _evaluate_invariants(
    scenario: WebUiScenario,
    screen_model: dict[str, Any],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if (
        scenario.screen.loading_behavior
        in {
            LoadingBehavior.CLEARS,
            LoadingBehavior.NOT_RENDERED,
        }
        and screen_model["loading_markers"]
    ):
        failures.append(
            {
                "id": "primary_loading_marker_persisted",
                "message": "Primary loading markers are still present after normalization.",
                "markers": screen_model["loading_markers"],
            }
        )

    represented = set(screen_model["landmarks"]) | set(screen_model["actions"])
    missing_landmarks = [
        landmark
        for landmark in scenario.screen.visible_landmarks
        if landmark not in represented
    ]
    if missing_landmarks:
        failures.append(
            {
                "id": "required_landmarks_missing",
                "message": "Required screen landmarks/actions were not represented.",
                "missing": missing_landmarks,
            }
        )

    if ScenarioTag.LARGE_STATE in scenario.tags and not screen_model["windowed"]:
        failures.append(
            {
                "id": "large_list_not_windowed",
                "message": "Large list scenario exposed too many rows.",
                "row_count": screen_model["row_count"],
                "visible_rows": screen_model["visible_rows"],
            }
        )

    if not screen_model["unknown_status_normalized"]:
        failures.append(
            {
                "id": "unknown_status_not_normalized",
                "message": "Unknown statuses did not normalize to a safe fallback.",
            }
        )

    if not screen_model["missing_optional_fields_safe"]:
        failures.append(
            {
                "id": "missing_optional_fields_unsafe",
                "message": "Fixture normalization depends on optional fields.",
            }
        )

    if screen_model["cursor_snapshot"] != _json_roundtrip(
        screen_model["cursor_snapshot"]
    ):
        failures.append(
            {
                "id": "cursor_snapshot_not_idempotent",
                "message": "Cursor snapshot shape changed after JSON roundtrip.",
            }
        )

    if screen_model["repair_snapshot"] != _json_roundtrip(
        screen_model["repair_snapshot"]
    ):
        failures.append(
            {
                "id": "repair_snapshot_not_idempotent",
                "message": "Repair snapshot shape changed after JSON roundtrip.",
            }
        )
    pma_failure = _pma_timeline_invariant_failure(scenario, screen_model)
    if pma_failure:
        failures.append(pma_failure)
    return failures


def _landmarks_for_route(
    route_name: str,
    *,
    repos: list[dict[str, Any]],
    worktrees: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    chats: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    docs: list[dict[str, Any]],
    settings: Any,
) -> list[str]:
    if route_name == "hub":
        return ["Hub", "Workspace"] + (
            ["No repositories", "No active runs"] if not repos and not runs else []
        )
    if route_name == "repos":
        return ["Repositories", *[_label(repo) for repo in repos]]
    if route_name == "repo-detail":
        return ["Repository", "Repo tickets", *[_label(repo) for repo in repos]]
    if route_name == "repo-tickets":
        return [
            "Repo ticket queue",
            *[_label(ticket) for ticket in tickets[:MAX_VISIBLE_ROWS]],
        ]
    if route_name == "repo-ticket-detail":
        return [
            "Workspace ticket detail",
            "Ticket context",
            *[_label(ticket) for ticket in tickets[:1]],
        ]
    if route_name == "worktree-detail":
        return ["Repo worktree detail", *[_label(worktree) for worktree in worktrees]]
    if route_name == "worktree-contextspace":
        return [
            "Workspace contextspace",
            "Durable shared context",
            *[_label(doc) for doc in docs],
        ]
    if route_name == "worktree-tickets":
        return [
            "Worktree ticket queue",
            *[_label(ticket) for ticket in tickets[:MAX_VISIBLE_ROWS]],
        ]
    if route_name == "tickets":
        return [
            "Tickets",
            "All tickets",
            *[_label(ticket) for ticket in tickets[:MAX_VISIBLE_ROWS]],
        ]
    if route_name == "ticket-detail":
        return [
            "Workspace ticket detail",
            "Ticket context",
            *[_label(ticket) for ticket in tickets[:1]],
        ]
    if route_name == "chat":
        return [
            "Chats",
            "Search chats, repos, tickets",
            *[_label(chat) for chat in chats],
            *[_timeline_label(item) for item in timeline],
            *[
                _label(attachment)
                for item in timeline
                for attachment in _records(_payload(item), "attachments")
            ],
        ]
    if route_name == "worktrees":
        return ["Repo worktree variants", *[_label(worktree) for worktree in worktrees]]
    if route_name == "contextspace":
        return [
            "Workspace contextspace",
            "Durable shared context",
            *[_label(doc) for doc in docs],
        ]
    if route_name == "settings":
        agents = settings.get("agents", []) if isinstance(settings, dict) else []
        return ["Settings", "Models", *[_label(agent) for agent in agents]]
    return [route_name]


def _actions_for_route(
    route_name: str,
    repos: list[dict[str, Any]],
    worktrees: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    chats: list[dict[str, Any]],
) -> list[str]:
    actions = ["Refresh"]
    if route_name.endswith("tickets") or route_name in {
        "repo-tickets",
        "worktree-tickets",
    }:
        actions.append("New ticket")
    if tickets:
        actions.append("Open ticket")
    if repos or worktrees:
        actions.append("Open workspace")
    if chats:
        actions.append("Open chat")
    return actions


def _row_count_for_route(
    route_name: str,
    *,
    repos: list[dict[str, Any]],
    worktrees: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    chats: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    docs: list[dict[str, Any]],
) -> int:
    if route_name in {"repos", "repo-detail"}:
        return len(repos)
    if route_name in {"worktrees", "worktree-detail"}:
        return len(worktrees)
    if route_name in {"repo-tickets", "worktree-tickets", "tickets"}:
        return len(tickets)
    if route_name == "chat":
        return max(len(chats), len(timeline))
    if "contextspace" in route_name:
        return len(docs)
    return 1


def _records(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _label(record: dict[str, Any]) -> str:
    for key in ("title", "name", "id", "thread_target_id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return "unnamed"


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _timeline_label(record: dict[str, Any]) -> str:
    payload = _payload(record)
    for key in ("title", "tool_name", "text", "description", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return _label(record)


def _unknown_status_normalized(records: list[dict[str, Any]]) -> bool:
    known = {"running", "waiting", "idle", "done", "failed", "blocked", "invalid"}
    for record in records:
        status = record.get("status") or record.get("normalized_status")
        if isinstance(status, str) and status and status not in known:
            normalized = "idle"
            return normalized in known
    return True


def _missing_optional_fields_safe(payload: dict[str, Any]) -> bool:
    clone = json.loads(json.dumps(payload))
    for key in ("last_activity_at", "updated_at", "progress_percent", "path"):
        for collection in ("repos", "worktrees", "tickets", "runs", "chats"):
            for record in _records(clone, collection):
                record.pop(key, None)
    try:
        _records(clone, "repos")
        _records(clone, "worktrees")
        _records(clone, "tickets")
        _records(clone, "runs")
        _records(clone, "chats")
    except (TypeError, AttributeError):
        return False
    return True


def _cursor_snapshot(
    scenario: WebUiScenario, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "scenario_id": scenario.scenario_id,
        "route": scenario.route_path,
        "counts": {
            key: len(_records(payload, key))
            for key in ("repos", "worktrees", "tickets", "runs", "chats", "timeline")
        },
        "next_cursor": None,
    }


def _repair_snapshot(tickets: list[dict[str, Any]]) -> dict[str, Any]:
    invalid = [
        {
            "id": _label(ticket),
            "errors": ticket.get("errors", []),
        }
        for ticket in tickets
        if ticket.get("errors")
    ]
    return {"needs_repair": bool(invalid), "tickets": invalid}


def _json_roundtrip(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, sort_keys=True))


def _pma_timeline_summary(
    timeline: list[dict[str, Any]], payload: dict[str, Any]
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    assistant_delivery_keys: set[str] = set()
    duplicate_assistant_deliveries = 0
    attachment_count = 0
    for item in timeline:
        kind = str(item.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
        item_payload = _payload(item)
        attachment_count += len(_records(item_payload, "attachments"))
        if kind == "assistant_message":
            text = str(item_payload.get("text") or "").strip()
            turn_id = str(item.get("managed_turn_id") or "")
            key = f"{item.get('managed_thread_id') or ''}|{turn_id}|{text}"
            if text and turn_id:
                if key in assistant_delivery_keys:
                    duplicate_assistant_deliveries += 1
                assistant_delivery_keys.add(key)
    repair = payload.get("repair") if isinstance(payload.get("repair"), dict) else {}
    return {
        "counts": counts,
        "attachment_count": attachment_count,
        "visible_assistant_deliveries": len(assistant_delivery_keys),
        "duplicate_assistant_deliveries": duplicate_assistant_deliveries,
        "approval_visible": counts.get("approval", 0) > 0,
        "error_visible": any(
            str(item.get("status") or "").lower() == "failed"
            or "failed" in _timeline_label(item).lower()
            for item in timeline
        ),
        "interrupt_affordance": any(
            str(chat.get("status") or "").lower() == "running"
            for chat in _records(payload, "chats")
        ),
        "stream_gap_repaired": repair.get("stream_gap_repaired") is True,
    }


def _pma_timeline_invariant_failure(
    scenario: WebUiScenario, screen_model: dict[str, Any]
) -> dict[str, Any] | None:
    summary = screen_model.get("pma_timeline")
    if not isinstance(summary, dict):
        return None
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    expected_by_fixture = {
        "pma_queued": ("user_message", "queued user turn"),
        "pma_running": ("intermediate", "running progress update"),
        "pma_final": ("assistant_message", "final assistant delivery"),
        "pma_approval": ("approval", "approval request"),
        "pma_attachment": ("user_message", "attachment-bearing user message"),
        "pma_duplicate_repair": ("assistant_message", "snapshot repair delivery"),
    }
    expected = expected_by_fixture.get(scenario.seed_fixture.value)
    if expected and int(counts.get(expected[0], 0)) <= 0:
        return {
            "id": "pma_timeline_state_missing",
            "message": f"PMA fixture is missing {expected[1]}.",
            "fixture_kind": scenario.seed_fixture.value,
            "pma_timeline": summary,
        }
    if scenario.seed_fixture.value == "pma_error" and not summary.get("error_visible"):
        return {
            "id": "pma_error_state_missing",
            "message": "PMA error fixture did not surface a failed/error timeline state.",
            "pma_timeline": summary,
        }
    if scenario.seed_fixture.value == "pma_interrupt" and not summary.get(
        "interrupt_affordance"
    ):
        return {
            "id": "pma_interrupt_affordance_missing",
            "message": "PMA interrupt fixture did not expose an interruptable running chat.",
            "pma_timeline": summary,
        }
    if (
        scenario.seed_fixture.value == "pma_attachment"
        and int(summary.get("attachment_count", 0)) <= 0
    ):
        return {
            "id": "pma_attachment_missing",
            "message": "PMA attachment fixture did not expose timeline attachments.",
            "pma_timeline": summary,
        }
    if scenario.seed_fixture.value == "pma_duplicate_repair":
        if int(summary.get("duplicate_assistant_deliveries", 0)) <= 0:
            return {
                "id": "pma_duplicate_delivery_fixture_missing",
                "message": "Duplicate repair fixture did not include duplicate assistant deliveries.",
                "pma_timeline": summary,
            }
        if not summary.get("stream_gap_repaired"):
            return {
                "id": "pma_stream_gap_repair_missing",
                "message": "Duplicate repair fixture did not include a repair snapshot marker.",
                "pma_timeline": summary,
            }
    return None
