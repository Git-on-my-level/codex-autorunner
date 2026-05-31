from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Mapping, Sequence

from .pma_file_inbox import (
    PMA_FILE_NEXT_ACTION_PROCESS,
    PMA_FILE_NEXT_ACTION_REVIEW_STALE,
    _extract_entry_freshness,
)

PMA_ATTENTION_SCHEMA_VERSION = 2
PMA_ATTENTION_MAX_ACTIONS = 3


def _text(value: Any) -> str:
    return str(value or "").strip()


def _quote(value: Any) -> str:
    text = _text(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _freshness_status(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return "unknown"
    if payload.get("is_stale") is True:
        return "stale"
    if payload.get("is_stale") is False:
        return "ok"
    status = _text(payload.get("status")).lower()
    return status or "unknown"


def _stable_digest_payload(value: Any) -> str:
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        payload = str(value)
    return sha256(payload.encode("utf-8")).hexdigest()[:16]


def _item_semantic_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    freshness = _extract_entry_freshness(item)
    supersession = item.get("supersession") or {}
    return {
        "id": _text(item.get("action_queue_id")),
        "src": _action_source(item),
        "scope": _scope_key(item),
        "status": _text(supersession.get("status")),
        "repo": _text(item.get("repo_id")),
        "run": _text(item.get("run_id")),
        "thread": _text(item.get("managed_thread_id") or item.get("thread_id")),
        "file": _text(item.get("name")) if item.get("item_type") == "pma_file" else "",
        "next_action": _text(item.get("next_action")),
        "recommended_action": _text(item.get("recommended_action")),
        "recommended_detail": _text(item.get("recommended_detail")),
        "open_url": _text(item.get("open_url")),
        "cmd": _build_command(item),
        "fresh": _freshness_status(freshness),
    }


def _attention_freshness(snapshot: Mapping[str, Any], actions: Sequence[dict]) -> str:
    if any(action.get("fresh") == "stale" for action in actions):
        return "risk"
    if actions:
        return "ok"

    freshness = snapshot.get("freshness") or {}
    stale_sections = (
        freshness.get("stale_sections") if isinstance(freshness, Mapping) else []
    )
    if stale_sections:
        return "idle"
    return "ok"


def _scope_key(item: Mapping[str, Any]) -> str:
    scope = item.get("scope") or {}
    if isinstance(scope, Mapping):
        value = _text(scope.get("key"))
        if value:
            return value
    for key in ("run_id", "managed_thread_id", "thread_id", "name", "repo_id"):
        value = _text(item.get(key))
        if value:
            return value
    return _text(item.get("action_queue_id")) or "unknown"


def _is_low_signal_inventory(item: Mapping[str, Any]) -> bool:
    if bool(item.get("likely_false_positive")):
        return True
    item_type = _text(item.get("item_type")).lower()
    if item_type in {"managed_thread_followup_summary", "pma_file_summary"}:
        return True
    operator_need = _text(item.get("operator_need")).lower()
    return operator_need in {"optional", "cleanup", "protected", "review"}


def _is_hard_attention_item(item: Mapping[str, Any]) -> bool:
    if _is_low_signal_inventory(item):
        return False
    item_type = _text(item.get("item_type")).lower()
    queue_source = _text(item.get("queue_source")).lower()
    followup_state = _text(item.get("followup_state")).lower()
    operator_need = _text(item.get("operator_need")).lower()

    if queue_source == "ticket_flow_inbox":
        return True
    if item_type == "automation_wakeup":
        return True
    if item_type == "pma_file":
        return _text(item.get("next_action")) == PMA_FILE_NEXT_ACTION_PROCESS
    if item_type == "managed_thread_followup":
        return followup_state in {"attention_required", "awaiting_followup"}
    return operator_need in {"urgent", "normal"}


def _build_command(item: Mapping[str, Any]) -> str:
    item_type = _text(item.get("item_type")).lower()
    queue_source = _text(item.get("queue_source")).lower()
    repo_id = _text(item.get("repo_id"))
    run_id = _text(item.get("run_id"))
    thread_id = _text(item.get("managed_thread_id") or item.get("thread_id"))
    file_name = _text(item.get("name"))
    action_queue_id = _text(item.get("action_queue_id"))

    if queue_source == "ticket_flow_inbox" and repo_id and run_id:
        return "car hub snapshot --section action_queue"
    if item_type == "managed_thread_followup" and thread_id:
        return f"car pma thread info --id {thread_id}"
    if item_type == "pma_file" and file_name:
        return "car pma files"
    if item_type == "automation_wakeup":
        return "car pma automation list"
    if action_queue_id:
        return "car hub snapshot --section action_queue"
    return "car hub snapshot --section action_queue"


def _action_source(item: Mapping[str, Any]) -> str:
    queue_source = _text(item.get("queue_source"))
    return {
        "ticket_flow_inbox": "dispatch",
        "managed_thread_followup": "thread",
        "pma_file_inbox": "file",
        "automation_wakeup": "automation",
    }.get(queue_source, queue_source or "unknown")


def _project_action(item: Mapping[str, Any]) -> dict[str, Any]:
    semantic = _item_semantic_payload(item)
    return {
        "id": semantic["id"],
        "src": semantic["src"],
        "scope": semantic["scope"],
        "status": semantic["status"] or "non_primary",
        "repo": semantic["repo"],
        "run": semantic["run"],
        "thread": semantic["thread"],
        "file": semantic["file"],
        "cmd": semantic["cmd"],
        "fresh": semantic["fresh"],
    }


def _surface_counts_from_threads(
    threads: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    counts = {
        "protected": 0,
        "reusable": 0,
        "cleanup": 0,
        "running": 0,
        "failed": 0,
        "hung": 0,
    }
    for thread in threads:
        status = _text(
            thread.get("operator_status")
            or thread.get("normalized_status")
            or thread.get("status")
        ).lower()
        reason = _text(
            thread.get("status_reason_code") or thread.get("status_reason")
        ).lower()
        if bool(thread.get("chat_bound")) or bool(thread.get("cleanup_protected")):
            counts["protected"] += 1
        if status in {"completed", "reusable", "idle"} and not bool(
            thread.get("chat_bound") or thread.get("cleanup_protected")
        ):
            counts["reusable"] += 1
        if status == "running":
            counts["running"] += 1
        if status in {"failed", "attention_required"}:
            counts["failed"] += 1
        if "hung" in reason:
            counts["hung"] += 1
    return counts


def _overlay_thread_summary_counts(
    counts: dict[str, int], action_queue: Sequence[Mapping[str, Any]]
) -> dict[str, int]:
    projected = dict(counts)
    for item in action_queue:
        if _text(item.get("item_type")) != "managed_thread_followup_summary":
            continue
        followup_state = _text(item.get("followup_state"))
        thread_count = int(item.get("thread_count") or 0)
        if followup_state == "protected_chat_bound":
            projected["protected"] = max(projected["protected"], thread_count)
        elif followup_state == "reusable":
            projected["reusable"] = max(projected["reusable"], thread_count)
        elif followup_state == "idle_archive_candidate":
            projected["cleanup"] = max(projected["cleanup"], thread_count)
    return projected


def _surface_counts_from_files(
    pma_files_detail: Mapping[str, Sequence[Mapping[str, Any]]],
    action_queue: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    fresh = 0
    stale = 0
    for entry in pma_files_detail.get("inbox") or []:
        next_action = _text(entry.get("next_action"))
        freshness = _extract_entry_freshness(entry)
        if next_action == PMA_FILE_NEXT_ACTION_REVIEW_STALE or (
            isinstance(freshness, Mapping) and freshness.get("is_stale") is True
        ):
            stale += 1
        else:
            fresh += 1

    for item in action_queue:
        item_type = _text(item.get("item_type"))
        if item_type == "pma_file_summary":
            stale = max(stale, int(item.get("file_count") or 0))
        elif (
            item_type == "pma_file"
            and _text(item.get("next_action")) == PMA_FILE_NEXT_ACTION_PROCESS
        ):
            fresh = max(fresh, 1)
    return {"fresh": fresh, "stale": stale}


def _surface_counts_from_repos(repos: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    dirty = 0
    for repo in repos:
        if bool(repo.get("dirty")) or bool(repo.get("has_unpushed_commits")):
            dirty += 1
            continue
        status = _text(repo.get("status")).lower()
        if status in {"dirty", "needs_push", "unpushed"}:
            dirty += 1
    return {"tracked": len(repos), "dirty": dirty}


def _surface_counts_from_automation(automation: Mapping[str, Any]) -> dict[str, int]:
    wakeups = automation.get("wakeups") if isinstance(automation, Mapping) else {}
    pending = (
        int((wakeups or {}).get("pending_count") or 0)
        if isinstance(wakeups, Mapping)
        else 0
    )
    return {"pending": pending}


def _fallback_action_queue(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items: list[Mapping[str, Any]] = []
    for entry in snapshot.get("inbox") or []:
        if not isinstance(entry, Mapping):
            continue
        copied = dict(entry)
        repo_id = _text(copied.get("repo_id"))
        run_id = _text(copied.get("run_id"))
        copied.setdefault("queue_source", "ticket_flow_inbox")
        copied.setdefault("item_type", copied.get("item_type") or "run_dispatch")
        copied.setdefault(
            "action_queue_id",
            f"ticket_flow_inbox:{repo_id or '-'}:{run_id or '-'}",
        )
        copied.setdefault("supersession", {"status": "primary", "superseded": False})
        copied.setdefault(
            "scope",
            {"kind": "run", "key": f"run:{run_id}" if run_id else f"repo:{repo_id}"},
        )
        items.append(copied)

    pma_files_detail = snapshot.get("pma_files_detail") or {}
    if isinstance(pma_files_detail, Mapping):
        for entry in pma_files_detail.get("inbox") or []:
            if not isinstance(entry, Mapping):
                continue
            if _text(entry.get("next_action")) != PMA_FILE_NEXT_ACTION_PROCESS:
                continue
            copied = dict(entry)
            name = _text(copied.get("name"))
            copied.setdefault("queue_source", "pma_file_inbox")
            copied.setdefault("item_type", "pma_file")
            copied.setdefault("action_queue_id", f"pma_file_inbox:{name or '-'}")
            copied.setdefault(
                "supersession", {"status": "non_primary", "superseded": False}
            )
            copied.setdefault(
                "scope", {"kind": "filebox", "key": f"filebox:inbox:{name or '-'}"}
            )
            items.append(copied)
    return items


def build_pma_attention_state(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    action_queue = [
        item for item in snapshot.get("action_queue") or [] if isinstance(item, Mapping)
    ]
    if not action_queue:
        action_queue = list(_fallback_action_queue(snapshot))
    hard_items = [
        item
        for item in action_queue
        if _is_hard_attention_item(item)
        and not bool((item.get("supersession") or {}).get("superseded"))
    ]
    visible_hard_items = hard_items[:PMA_ATTENTION_MAX_ACTIONS]
    actions = [_project_action(item) for item in visible_hard_items]
    thread_counts = _overlay_thread_summary_counts(
        _surface_counts_from_threads(
            [
                item
                for item in snapshot.get("managed_threads") or []
                if isinstance(item, Mapping)
            ]
        ),
        action_queue,
    )
    pma_files_detail = snapshot.get("pma_files_detail") or {}
    if not isinstance(pma_files_detail, Mapping):
        pma_files_detail = {}
    repos = [item for item in snapshot.get("repos") or [] if isinstance(item, Mapping)]
    automation = snapshot.get("automation") or {}
    if not isinstance(automation, Mapping):
        automation = {}

    state = "action" if actions else "idle"
    if isinstance(snapshot.get("availability"), Mapping):
        state = "degraded"
    drilldowns = [
        "car hub snapshot --section managed_threads",
        "car pma files",
        "car hub snapshot --section repos",
        "car pma automation list",
    ]
    if len(hard_items) > PMA_ATTENTION_MAX_ACTIONS:
        drilldowns.insert(0, "car hub snapshot --section action_queue")

    return {
        "version": PMA_ATTENTION_SCHEMA_VERSION,
        "state": state,
        "fresh": _attention_freshness(snapshot, actions),
        "action_count": len(hard_items),
        "omitted_action_count": max(0, len(hard_items) - len(actions)),
        "semantic_ref": _stable_digest_payload(
            [_item_semantic_payload(item) for item in hard_items]
        ),
        "actions": actions,
        "background": {
            "threads": thread_counts,
            "files": _surface_counts_from_files(pma_files_detail, action_queue),
            "repos": _surface_counts_from_repos(repos),
            "automation": _surface_counts_from_automation(automation),
        },
        "drilldowns": drilldowns,
    }


def render_pma_attention_state(attention: Mapping[str, Any], *, mode: str) -> str:
    version = int(attention.get("version") or PMA_ATTENTION_SCHEMA_VERSION)
    state = _text(attention.get("state")) or "idle"
    fresh = _text(attention.get("fresh")) or "unknown"
    actions = [
        item for item in attention.get("actions") or [] if isinstance(item, Mapping)
    ]
    action_count = int(attention.get("action_count") or len(actions))
    omitted_action_count = int(attention.get("omitted_action_count") or 0)
    lines = [
        f"<pma v={version} mode={mode} state={state} fresh={fresh} actions={action_count}>"
    ]
    lines.append("q:")
    if actions:
        for index, action in enumerate(actions, start=1):
            parts = [
                str(index),
                f"src={_text(action.get('src')) or 'unknown'}",
                f"scope={_text(action.get('scope')) or 'unknown'}",
            ]
            for key in ("repo", "run", "thread", "file"):
                value = _text(action.get(key))
                if value:
                    parts.append(f"{key}={value}")
            parts.append(f"fresh={_text(action.get('fresh')) or 'unknown'}")
            parts.append(f"cmd={_quote(action.get('cmd'))}")
            lines.append("  " + " ".join(parts))
        if omitted_action_count:
            lines.append(
                f"  +{omitted_action_count} more cmd="
                f"{_quote('car hub snapshot --section action_queue')}"
            )
    else:
        lines.append("  none")

    background = attention.get("background") or {}
    lines.append("bg:")
    for surface in ("threads", "files", "repos", "automation"):
        counts = background.get(surface) if isinstance(background, Mapping) else {}
        if not isinstance(counts, Mapping):
            counts = {}
        parts = [f"{key}={int(value or 0)}" for key, value in counts.items()]
        if parts:
            lines.append(f"  {surface} " + " ".join(parts))

    drilldowns = [
        _text(command)
        for command in attention.get("drilldowns") or []
        if _text(command)
    ]
    if drilldowns:
        lines.append("drill:")
        for command in drilldowns:
            lines.append(f"  {command}")
    semantic_ref = _text(attention.get("semantic_ref"))
    if semantic_ref:
        lines.append(f"semantic_ref={semantic_ref}")
    lines.append("</pma>")
    return "\n".join(lines)
