from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, cast

from .flows.models import format_flow_duration
from .managed_thread_status import derive_managed_thread_operator_status
from .pma_context_shared import (
    PMA_MAX_AUTOMATION_ITEMS,
    PMA_MAX_LIFECYCLE_EVENTS,
    PMA_MAX_MESSAGES,
    PMA_MAX_PMA_FILES,
    PMA_MAX_PMA_THREADS,
    PMA_MAX_REPOS,
    PMA_MAX_TEMPLATE_FIELD_CHARS,
    PMA_MAX_TEMPLATE_REPOS,
    PMA_MAX_TEXT,
    _truncate,
)
from .pma_file_inbox import (
    PMA_FILE_NEXT_ACTION_PROCESS,
    PMA_FILE_NEXT_ACTION_REVIEW_STALE,
    _extract_entry_freshness,
)


def _field(item: Mapping[str, Any], key: str, max_chars: int) -> str:
    return _truncate(str(item.get(key) or ""), max_chars)


def _render_freshness_summary(payload: Any, *, max_field_chars: int) -> Optional[str]:
    if not isinstance(payload, Mapping):
        return None
    status = _field(payload, "status", max_field_chars) or _truncate(
        "unknown", max_field_chars
    )
    basis = _field(payload, "recency_basis", max_field_chars)
    basis_at = _field(payload, "basis_at", max_field_chars)
    age_raw = payload.get("age_seconds")
    age_text = _truncate(str(age_raw), max_field_chars) if age_raw is not None else ""
    parts = [f"status={status}"]
    if basis:
        parts.append(f"basis={basis}")
    if basis_at:
        parts.append(f"basis_at={basis_at}")
    if age_text:
        parts.append(f"age_seconds={age_text}")
    return " ".join(parts) if parts else None


def _render_destination_summary(destination: Any, *, max_field_chars: int) -> str:
    destination_payload = destination if isinstance(destination, Mapping) else {}
    destination_kind = _field(
        destination_payload, "kind", max_field_chars
    ) or _truncate("local", max_field_chars)
    destination_text = destination_kind or "local"
    if destination_kind == "docker":
        image = _field(destination_payload, "image", max_field_chars)
        destination_text = f"docker:{image}" if image else "docker:image-missing"
    return destination_text


def _render_resource_owner_summary(
    item: Mapping[str, Any], *, max_field_chars: int
) -> str:
    resource_kind = _field(item, "resource_kind", max_field_chars)
    resource_id = _field(item, "resource_id", max_field_chars)
    repo_id = _field(item, "repo_id", max_field_chars)
    if resource_kind and resource_id:
        if resource_kind == "repo":
            return f"repo_id={resource_id}"
        return f"owner={resource_kind}:{resource_id}"
    if repo_id:
        return f"repo_id={repo_id}"
    workspace_root = _field(item, "workspace_root", max_field_chars)
    if workspace_root:
        return f"workspace_root={workspace_root}"
    return "owner=unowned"


def _render_ticket_flow_summary(summary: Optional[dict[str, Any]]) -> str:
    if not summary:
        return "null"
    status = summary.get("status")
    done_count = summary.get("done_count")
    total_count = summary.get("total_count")
    current_step = summary.get("current_step")
    pr_url = summary.get("pr_url")
    final_review_status = summary.get("final_review_status")
    parts: list[str] = []
    if status is not None:
        parts.append(f"status={status}")
    if done_count is not None and total_count is not None:
        parts.append(f"done={done_count}/{total_count}")
    if current_step is not None:
        parts.append(f"step={current_step}")
    if pr_url:
        parts.append("pr=opened")
    if final_review_status:
        parts.append(f"final_review={final_review_status}")
    if not parts:
        return "null"
    return " ".join(parts)


def _render_freshness_section(
    snapshot: dict[str, Any], lines: list[str], max_field_chars: int
) -> None:
    generated_at = _field(snapshot, "generated_at", max_field_chars)
    snapshot_freshness = snapshot.get("freshness") or {}
    if not generated_at and not snapshot_freshness:
        return
    lines.append("Snapshot Freshness:")
    threshold = snapshot_freshness.get("stale_threshold_seconds")
    threshold_text = (
        _truncate(str(threshold), max_field_chars) if threshold is not None else ""
    )
    header_parts: list[str] = []
    if generated_at:
        header_parts.append(f"generated_at={generated_at}")
    if threshold_text:
        header_parts.append(f"stale_threshold_seconds={threshold_text}")
    if header_parts:
        lines.append(f"- {' '.join(header_parts)}")
    sections = snapshot_freshness.get("sections") or {}
    stale_section_names: list[str] = []
    if isinstance(sections, Mapping):
        for section_name, payload in sections.items():
            if not isinstance(payload, Mapping):
                continue
            count = _truncate(str(payload.get("entity_count") or 0), max_field_chars)
            stale = _truncate(str(payload.get("stale_count") or 0), max_field_chars)
            newest = _field(payload, "newest_basis_at", max_field_chars)
            is_stale = int(payload.get("stale_count") or 0) > 0
            if is_stale:
                stale_section_names.append(str(section_name))
            lines.append(
                f"- section={_truncate(str(section_name), max_field_chars)} "
                f"count={count} stale={stale}"
                + (f" newest_basis_at={newest}" if newest else "")
                + (" STALE" if is_stale else "")
            )
    if stale_section_names:
        warning_sections = ", ".join(
            _truncate(name, max_field_chars) for name in stale_section_names
        )
        lines.append(
            f"WARNING: stale sections may be outdated; refresh before acting on: {warning_sections}"
        )
    lines.append("")


def _render_availability_section(
    snapshot: dict[str, Any],
    lines: list[str],
    *,
    max_field_chars: int,
    max_text_chars: int,
) -> None:
    availability = snapshot.get("availability") or {}
    if not isinstance(availability, Mapping) or not availability:
        return
    lines.append("Hub Snapshot Availability:")
    status = _field(availability, "status", max_field_chars) or _truncate(
        "unknown", max_field_chars
    )
    detail = _truncate(str(availability.get("detail") or ""), max_text_chars)
    note = _truncate(str(availability.get("note") or ""), max_text_chars)
    line = f"- status={status}"
    if detail:
        line += f" detail={detail}"
    lines.append(line)
    if note:
        lines.append(f"- note: {note}")
    lines.append("")


def _render_process_monitor_section(
    snapshot: dict[str, Any],
    lines: list[str],
    *,
    max_field_chars: int,
) -> None:
    payload = snapshot.get("process_monitor")
    if not isinstance(payload, Mapping):
        return
    status = _field(payload, "status", max_field_chars) or "unknown"
    if status == "ok":
        return
    metrics = payload.get("metrics") or {}
    if not isinstance(metrics, Mapping):
        return
    cadence_seconds = _field(payload, "cadence_seconds", max_field_chars)
    window_seconds = payload.get("window_seconds")
    sample_count = _field(payload, "sample_count", max_field_chars)
    latest_at = _field(payload, "latest_at", max_field_chars)
    lines.append("Process Monitor:")
    header = f"- status={status}"
    if cadence_seconds:
        header += f" cadence_seconds={cadence_seconds}"
    if isinstance(window_seconds, (int, float)) and int(window_seconds) > 0:
        header += f" window_hours={int(window_seconds) // 3600}"
    if sample_count:
        header += f" samples={sample_count}"
    if latest_at:
        header += f" latest_at={latest_at}"
    lines.append(header)
    for label, key in (
        ("opencode", "opencode"),
        ("app_server", "app_server"),
        ("total", "total"),
    ):
        metric = metrics.get(key)
        if not isinstance(metric, Mapping):
            continue
        current = _field(metric, "current", max_field_chars)
        average = metric.get("average")
        tp95 = _field(metric, "p95", max_field_chars)
        peak = _field(metric, "peak", max_field_chars)
        abnormal = bool(metric.get("abnormal"))
        line = f"- {label}={current or '0'}"
        if average is not None:
            line += f" avg={float(average):.1f}"
        if tp95:
            line += f" tp95={tp95}"
        if peak:
            line += f" peak={peak}"
        if abnormal:
            line += " HIGH"
        lines.append(line)
    reasons = payload.get("reasons")
    if isinstance(reasons, Sequence):
        for reason in reasons:
            text = _truncate(str(reason or ""), max_field_chars * 4)
            if text:
                lines.append(f"- reason: {text}")
    lines.append("")


def _render_action_queue_item(
    item: Mapping[str, Any],
    lines: list[str],
    *,
    max_field_chars: int,
    max_text_chars: int,
) -> None:
    queue_id = _field(item, "action_queue_id", max_field_chars)
    source = _field(item, "queue_source", max_field_chars)
    queue_rank = _field(item, "queue_rank", max_field_chars)
    item_type = _field(item, "item_type", max_field_chars)
    item_name = _field(item, "name", max_field_chars)
    repo_id = _field(item, "repo_id", max_field_chars)
    run_id = _field(item, "run_id", max_field_chars)
    managed_thread_id = _truncate(
        str(item.get("managed_thread_id") or item.get("thread_id") or ""),
        max_field_chars,
    )
    file_name = (
        _field(item, "name", max_field_chars)
        if item.get("item_type") == "pma_file"
        else ""
    )
    recommended_action = _field(item, "recommended_action", max_field_chars)
    followup_state = _field(item, "followup_state", max_field_chars)
    operator_need = _field(item, "operator_need", max_field_chars)
    thread_count = _field(item, "thread_count", max_field_chars)
    file_count = _field(item, "file_count", max_field_chars)
    precedence = item.get("precedence") or {}
    precedence_rank = _field(precedence, "rank", max_field_chars)
    precedence_label = _field(precedence, "label", max_field_chars)
    supersession = item.get("supersession") or {}
    supersession_status = _field(supersession, "status", max_field_chars)
    superseded_by = _field(supersession, "superseded_by", max_field_chars)
    lines.append(
        f"- rank={queue_rank} source={source} precedence={precedence_rank}:{precedence_label} "
        f"status={supersession_status} item_type={item_type} id={queue_id}"
        + (f" repo_id={repo_id}" if repo_id else "")
        + (f" run_id={run_id}" if run_id else "")
        + (f" managed_thread_id={managed_thread_id}" if managed_thread_id else "")
        + (f" name={item_name}" if item_name and item_name != file_name else "")
        + (f" file={file_name}" if file_name else "")
        + (f" followup_state={followup_state}" if followup_state else "")
        + (f" operator_need={operator_need}" if operator_need else "")
        + (f" thread_count={thread_count}" if thread_count else "")
        + (f" file_count={file_count}" if file_count else "")
        + (f" recommended_action={recommended_action}" if recommended_action else "")
    )
    why_selected = item.get("why_selected")
    if why_selected:
        lines.append(f"  why_selected: {_truncate(str(why_selected), max_text_chars)}")
    recommended_detail = item.get("recommended_detail")
    if recommended_detail:
        lines.append(
            f"  recommended_detail: {_truncate(str(recommended_detail), max_text_chars)}"
        )
    drilldown_commands = item.get("drilldown_commands")
    if isinstance(drilldown_commands, Sequence):
        visible_commands = [
            _truncate(str(command), max_text_chars)
            for command in drilldown_commands
            if str(command).strip()
        ]
        if visible_commands:
            lines.append(f"  drilldown: {', '.join(visible_commands)}")
    if superseded_by:
        lines.append(f"  superseded_by: {superseded_by}")
    supersession_reason = supersession.get("reason")
    if supersession_reason:
        lines.append(
            f"  supersession_reason: {_truncate(str(supersession_reason), max_text_chars)}"
        )
    freshness_summary = _render_freshness_summary(
        _extract_entry_freshness(item), max_field_chars=max_field_chars
    )
    if freshness_summary:
        lines.append(f"  freshness: {freshness_summary}")


def _render_action_queue_section(
    snapshot: dict[str, Any],
    lines: list[str],
    *,
    max_messages: int,
    max_field_chars: int,
    max_text_chars: int,
) -> None:
    action_queue = snapshot.get("action_queue") or []
    if not action_queue:
        return
    lines.append("PMA Action Queue:")
    has_primary = any(
        (item.get("supersession") or {}).get("status") == "primary"
        for item in action_queue
        if isinstance(item, Mapping)
    )
    if not has_primary:
        lines.append("- No strong next-action item right now")
    for item in list(action_queue)[: max(0, max_messages)]:
        _render_action_queue_item(
            item, lines, max_field_chars=max_field_chars, max_text_chars=max_text_chars
        )
    lines.append("")


def _render_inbox_item(
    item: Mapping[str, Any],
    lines: list[str],
    *,
    max_field_chars: int,
    max_text_chars: int,
    max_pma_files: int,
) -> None:
    item_type = _truncate(str(item.get("item_type", "run_dispatch")), max_field_chars)
    next_action = _truncate(
        str(item.get("next_action", "reply_and_resume")), max_field_chars
    )
    repo_id = _truncate(str(item.get("repo_id", "")), max_field_chars)
    run_id = _truncate(str(item.get("run_id", "")), max_field_chars)
    seq = _truncate(str(item.get("seq", "")), max_field_chars)
    dispatch = item.get("dispatch") or {}
    mode = _truncate(str(dispatch.get("mode", "")), max_field_chars)
    handoff = bool(dispatch.get("is_handoff"))
    run_state = item.get("run_state") or {}
    state = _truncate(str(run_state.get("state", "")), max_field_chars)
    current_ticket = _truncate(
        str(run_state.get("current_ticket", "")), max_field_chars
    )
    last_progress_at = _truncate(
        str(run_state.get("last_progress_at", "")), max_field_chars
    )
    duration = _truncate(
        str(
            format_flow_duration(
                cast(Optional[float], run_state.get("duration_seconds"))
            )
            or ""
        ),
        max_field_chars,
    )
    lines.append(
        f"- type={item_type} next_action={next_action} repo_id={repo_id} "
        f"run_id={run_id} seq={seq} mode={mode} handoff={str(handoff).lower()} "
        f"state={state} current_ticket={current_ticket} last_progress_at={last_progress_at}"
        + (f" duration={duration}" if duration else "")
    )
    title = dispatch.get("title")
    if title:
        lines.append(f"  title: {_truncate(str(title), max_text_chars)}")
    body = dispatch.get("body")
    if body:
        lines.append(f"  body: {_truncate(str(body), max_text_chars)}")
    files = item.get("files") or []
    if files:
        display = [
            _truncate(str(name), max_field_chars)
            for name in list(files)[: max(0, max_pma_files)]
        ]
        lines.append(f"  attachments: [{', '.join(display)}]")
    open_url = item.get("open_url")
    if open_url:
        lines.append(f"  open_url: {_truncate(str(open_url), max_field_chars)}")
    blocking_reason = run_state.get("blocking_reason")
    if blocking_reason:
        lines.append(
            f"  blocking_reason: {_truncate(str(blocking_reason), max_text_chars)}"
        )
    run_recommended_action = run_state.get("recommended_action")
    if run_recommended_action:
        lines.append(
            f"  recommended_action: {_truncate(str(run_recommended_action), max_text_chars)}"
        )
    freshness_summary = _render_freshness_summary(
        _extract_entry_freshness(item), max_field_chars=max_field_chars
    )
    if freshness_summary:
        lines.append(f"  freshness: {freshness_summary}")


def _render_inbox_section(
    snapshot: dict[str, Any],
    lines: list[str],
    *,
    max_messages: int,
    max_field_chars: int,
    max_text_chars: int,
    max_pma_files: int,
) -> None:
    inbox = snapshot.get("inbox") or []
    if not inbox:
        return
    lines.append("Run Dispatches (paused runs needing attention):")
    for item in list(inbox)[: max(0, max_messages)]:
        _render_inbox_item(
            item,
            lines,
            max_field_chars=max_field_chars,
            max_text_chars=max_text_chars,
            max_pma_files=max_pma_files,
        )
    lines.append("")


def _render_repos_section(
    snapshot: dict[str, Any],
    lines: list[str],
    *,
    max_repos: int,
    max_field_chars: int,
    max_text_chars: int,
) -> None:
    repos = snapshot.get("repos") or []
    if not repos:
        return
    lines.append("Repos:")
    for repo in list(repos)[: max(0, max_repos)]:
        repo_id = _truncate(str(repo.get("id", "")), max_field_chars)
        display_name = _truncate(str(repo.get("display_name", "")), max_field_chars)
        status = _truncate(str(repo.get("status", "")), max_field_chars)
        last_run_id = _truncate(str(repo.get("last_run_id", "")), max_field_chars)
        last_exit = _truncate(str(repo.get("last_exit_code", "")), max_field_chars)
        destination_text = _render_destination_summary(
            repo.get("effective_destination"), max_field_chars=max_field_chars
        )
        ticket_flow = _render_ticket_flow_summary(repo.get("ticket_flow"))
        run_state = repo.get("run_state") or {}
        state = _truncate(str(run_state.get("state", "")), max_field_chars)
        blocking_reason = _truncate(
            str(run_state.get("blocking_reason", "")), max_text_chars
        )
        recommended_action = _truncate(
            str(run_state.get("recommended_action", "")), max_text_chars
        )
        duration = _truncate(
            str(
                format_flow_duration(
                    cast(Optional[float], run_state.get("duration_seconds"))
                )
                or ""
            ),
            max_field_chars,
        )
        lines.append(
            f"- {repo_id} ({display_name}): status={status} "
            f"destination={destination_text} "
            f"last_run_id={last_run_id} last_exit_code={last_exit} "
            f"ticket_flow={ticket_flow} state={state}"
            + (f" duration={duration}" if duration else "")
        )
        if blocking_reason:
            lines.append(f"  blocking_reason: {blocking_reason}")
        if recommended_action:
            lines.append(f"  recommended_action: {recommended_action}")
        freshness_summary = _render_freshness_summary(
            _extract_entry_freshness(repo), max_field_chars=max_field_chars
        )
        if freshness_summary:
            lines.append(f"  freshness: {freshness_summary}")
    lines.append("")


def _render_agent_workspaces_section(
    snapshot: dict[str, Any],
    lines: list[str],
    *,
    max_repos: int,
    max_field_chars: int,
) -> None:
    agent_workspaces = snapshot.get("agent_workspaces") or []
    if not agent_workspaces:
        return
    lines.append("Agent Workspaces:")
    for workspace in list(agent_workspaces)[: max(0, max_repos)]:
        workspace_id = _truncate(str(workspace.get("id", "")), max_field_chars)
        display_name = _truncate(
            str(workspace.get("display_name", "")), max_field_chars
        )
        runtime = _truncate(str(workspace.get("runtime", "")), max_field_chars)
        enabled = str(bool(workspace.get("enabled"))).lower()
        exists_on_disk = str(bool(workspace.get("exists_on_disk"))).lower()
        destination_text = _render_destination_summary(
            workspace.get("effective_destination"), max_field_chars=max_field_chars
        )
        lines.append(
            f"- {workspace_id} ({display_name}): runtime={runtime} "
            f"destination={destination_text} enabled={enabled} "
            f"exists_on_disk={exists_on_disk}"
        )
        freshness_summary = _render_freshness_summary(
            workspace.get("freshness"), max_field_chars=max_field_chars
        )
        if freshness_summary:
            lines.append(f"  freshness: {freshness_summary}")
    lines.append("")


def _render_templates_section(
    snapshot: dict[str, Any],
    lines: list[str],
    *,
    max_template_repos: int,
    max_field_chars: int,
) -> None:
    templates = snapshot.get("templates") or {}
    template_repos = templates.get("repos") or []
    template_scan = templates.get("last_scan")
    if not templates.get("enabled") and not template_repos and not template_scan:
        return
    templates_enabled = bool(templates.get("enabled"))
    lines.append("Templates:")
    lines.append(f"- enabled={str(templates_enabled).lower()}")
    if template_repos:
        items: list[str] = []
        for repo in list(template_repos)[: max(0, max_template_repos)]:
            repo_id = _truncate(str(repo.get("id", "")), max_field_chars)
            default_ref = _truncate(str(repo.get("default_ref", "")), max_field_chars)
            trusted = bool(repo.get("trusted"))
            items.append(f"{repo_id}@{default_ref} trusted={str(trusted).lower()}")
        lines.append(f"- repos: [{', '.join(items)}]")
    if template_scan:
        repo_id = _truncate(str(template_scan.get("repo_id", "")), max_field_chars)
        decision = _truncate(str(template_scan.get("decision", "")), max_field_chars)
        severity = _truncate(str(template_scan.get("severity", "")), max_field_chars)
        scanned_at = _truncate(
            str(template_scan.get("scanned_at", "")), max_field_chars
        )
        lines.append(
            f"- last_scan: {repo_id} {decision} {severity} {scanned_at}".strip()
        )
    lines.append("")


def _render_pma_files_section(
    snapshot: dict[str, Any],
    lines: list[str],
    *,
    max_pma_files: int,
    max_field_chars: int,
) -> None:
    pma_files = snapshot.get("pma_files") or {}
    inbox_files = pma_files.get("inbox") or []
    outbox_files = pma_files.get("outbox") or []
    pma_files_detail = snapshot.get("pma_files_detail") or {}
    if not inbox_files and not outbox_files:
        return
    if inbox_files:
        lines.append("PMA File Inbox:")
        files = [
            _truncate(str(name), max_field_chars)
            for name in list(inbox_files)[: max(0, max_pma_files)]
        ]
        lines.append(f"- inbox: [{', '.join(files)}]")
        inbox_detail = [
            entry
            for entry in (pma_files_detail.get("inbox") or [])
            if isinstance(entry, Mapping)
        ]
        visible_inbox_detail = inbox_detail[: max(0, max_pma_files)]
        fresh_files = [
            _truncate(str(entry.get("name") or ""), max_field_chars)
            for entry in visible_inbox_detail
            if str(entry.get("next_action") or "") == PMA_FILE_NEXT_ACTION_PROCESS
        ]
        stale_files = [
            _truncate(str(entry.get("name") or ""), max_field_chars)
            for entry in visible_inbox_detail
            if str(entry.get("next_action") or "") == PMA_FILE_NEXT_ACTION_REVIEW_STALE
        ]
        if fresh_files:
            lines.append(
                f"- next_action: {PMA_FILE_NEXT_ACTION_PROCESS} "
                f"(fresh_uploads=[{', '.join(fresh_files)}])"
            )
        if stale_files:
            lines.append(
                f"- next_action: {PMA_FILE_NEXT_ACTION_REVIEW_STALE} "
                f"(likely_leftovers=[{', '.join(stale_files)}])"
            )
            lines.append(
                "- note: stale inbox files are usually false positives from prior "
                "work that was never cleared"
            )
    if outbox_files:
        lines.append("PMA File Outbox:")
        files = [
            _truncate(str(name), max_field_chars)
            for name in list(outbox_files)[: max(0, max_pma_files)]
        ]
        lines.append(f"- outbox: [{', '.join(files)}]")
    lines.append("")


def _render_pma_threads_section(
    snapshot: dict[str, Any],
    lines: list[str],
    *,
    max_pma_threads: int,
    max_field_chars: int,
) -> None:
    pma_threads = snapshot.get("pma_threads") or []
    if not pma_threads:
        return
    lines.append("PMA Managed Threads:")
    for thread in list(pma_threads)[: max(0, max_pma_threads)]:
        managed_thread_id = _truncate(
            str(thread.get("managed_thread_id", "")), max_field_chars
        )
        owner_summary = _render_resource_owner_summary(
            thread, max_field_chars=max_field_chars
        )
        agent = _field(thread, "agent", max_field_chars)
        raw_status = str(thread.get("status") or "")
        lifecycle_status = str(thread.get("lifecycle_status") or "-")
        operator_status = derive_managed_thread_operator_status(
            normalized_status=raw_status, lifecycle_status=lifecycle_status
        )
        status_display = _truncate(operator_status, max_field_chars)
        last_turn_outcome = _truncate(
            (
                raw_status
                if raw_status in {"completed", "interrupted", "failed"}
                else "-"
            ),
            max_field_chars,
        )
        status_reason = _truncate(
            str(thread.get("status_reason") or "-"), max_field_chars
        )
        name = _field(thread, "name", max_field_chars) or "-"
        preview = _field(thread, "last_message_preview", max_field_chars) or "-"
        lines.append(
            f"- {managed_thread_id} {owner_summary} agent={agent} "
            f"status={status_display} last_turn={last_turn_outcome} "
            f"reason={status_reason} name={name} last={preview}"
            + (" chat_bound=true" if bool(thread.get("chat_bound")) else "")
            + (
                f" binding_kind={_field(thread, 'binding_kind', max_field_chars)}"
                if thread.get("binding_kind")
                else ""
            )
            + (
                f" binding_id={_field(thread, 'binding_id', max_field_chars)}"
                if thread.get("binding_id")
                else ""
            )
            + (
                " cleanup_protected=true"
                if bool(thread.get("cleanup_protected"))
                else ""
            )
        )
        freshness_summary = _render_freshness_summary(
            thread.get("freshness"), max_field_chars=max_field_chars
        )
        if freshness_summary:
            lines.append(f"  freshness: {freshness_summary}")
    lines.append("")


def _render_subscription_sample(
    subscriptions: Mapping[str, Any],
    lines: list[str],
    *,
    max_automation_items: int,
    max_field_chars: int,
) -> None:
    sample = subscriptions.get("sample") or []
    if not sample:
        return
    lines.append("- subscriptions_sample:")
    for item in list(sample)[: max(0, max_automation_items)]:
        if not isinstance(item, dict):
            continue
        sub_id = _truncate(str(item.get("subscription_id", "")), max_field_chars)
        event_types = item.get("event_types")
        event_types_text = (
            ", ".join(
                _truncate(str(entry), max_field_chars)
                for entry in event_types
                if entry is not None
            )
            if isinstance(event_types, list)
            else _truncate(str(event_types or ""), max_field_chars)
        )
        lane_id = _field(item, "lane_id", max_field_chars)
        repo_id = _truncate(str(item.get("repo_id") or "-"), max_field_chars)
        run_id = _truncate(str(item.get("run_id") or "-"), max_field_chars)
        thread_id = _truncate(str(item.get("thread_id") or "-"), max_field_chars)
        from_state = _truncate(str(item.get("from_state") or "-"), max_field_chars)
        to_state = _truncate(str(item.get("to_state") or "-"), max_field_chars)
        lines.append(
            f"  - id={sub_id} events=[{event_types_text}] repo_id={repo_id} "
            f"run_id={run_id} thread_id={thread_id} lane_id={lane_id} "
            f"from={from_state} to={to_state}"
        )


def _render_timer_sample(
    timers: Mapping[str, Any],
    lines: list[str],
    *,
    max_automation_items: int,
    max_field_chars: int,
) -> None:
    sample = timers.get("sample") or []
    if not sample:
        return
    lines.append("- timers_sample:")
    for item in list(sample)[: max(0, max_automation_items)]:
        if not isinstance(item, dict):
            continue
        timer_id = _field(item, "timer_id", max_field_chars)
        timer_type = _field(item, "timer_type", max_field_chars)
        due_at = _field(item, "due_at", max_field_chars)
        lane_id = _field(item, "lane_id", max_field_chars)
        repo_id = _truncate(str(item.get("repo_id") or "-"), max_field_chars)
        run_id = _truncate(str(item.get("run_id") or "-"), max_field_chars)
        thread_id = _truncate(str(item.get("thread_id") or "-"), max_field_chars)
        lines.append(
            f"  - id={timer_id} type={timer_type} due_at={due_at} "
            f"repo_id={repo_id} run_id={run_id} thread_id={thread_id} lane_id={lane_id}"
        )


def _render_wakeup_sample(
    wakeups: Mapping[str, Any],
    lines: list[str],
    *,
    max_automation_items: int,
    max_field_chars: int,
) -> None:
    sample = wakeups.get("pending_sample") or []
    if not sample:
        return
    lines.append("- pending_wakeups_sample:")
    for item in list(sample)[: max(0, max_automation_items)]:
        if not isinstance(item, dict):
            continue
        wakeup_id = _field(item, "wakeup_id", max_field_chars)
        source = _field(item, "source", max_field_chars)
        event_type = _field(item, "event_type", max_field_chars)
        timer_id = _truncate(str(item.get("timer_id") or "-"), max_field_chars)
        subscription_id = _truncate(
            str(item.get("subscription_id") or "-"), max_field_chars
        )
        lane_id = _field(item, "lane_id", max_field_chars)
        to_state = _truncate(str(item.get("to_state") or "-"), max_field_chars)
        reason = _truncate(str(item.get("reason") or "-"), max_field_chars)
        lines.append(
            f"  - id={wakeup_id} source={source} event_type={event_type} "
            f"subscription_id={subscription_id} timer_id={timer_id} "
            f"lane_id={lane_id} to={to_state} reason={reason}"
        )


def _render_automation_section(
    snapshot: dict[str, Any],
    lines: list[str],
    *,
    max_automation_items: int,
    max_field_chars: int,
) -> None:
    automation = snapshot.get("automation") or {}
    if not automation:
        return
    subscriptions = automation.get("subscriptions") or {}
    timers = automation.get("timers") or {}
    wakeups = automation.get("wakeups") or {}
    subscriptions_active = int(subscriptions.get("active_count") or 0)
    timers_pending = int(timers.get("pending_count") or 0)
    wakeups_pending = int(wakeups.get("pending_count") or 0)
    wakeups_dispatched = int(wakeups.get("dispatched_recent_count") or 0)
    lines.append("PMA Automation:")
    lines.append(
        f"- subscriptions_active={subscriptions_active} timers_pending={timers_pending} "
        f"wakeups_pending={wakeups_pending} wakeups_dispatched_recent={wakeups_dispatched}"
    )
    has_samples = (
        bool(subscriptions.get("sample"))
        or bool(timers.get("sample"))
        or bool(wakeups.get("pending_sample"))
    )
    _render_subscription_sample(
        subscriptions,
        lines,
        max_automation_items=max_automation_items,
        max_field_chars=max_field_chars,
    )
    _render_timer_sample(
        timers,
        lines,
        max_automation_items=max_automation_items,
        max_field_chars=max_field_chars,
    )
    _render_wakeup_sample(
        wakeups,
        lines,
        max_automation_items=max_automation_items,
        max_field_chars=max_field_chars,
    )
    if not has_samples:
        lines.append(
            "- no automation configured; create rules via "
            "/hub/pma/subscriptions and /hub/pma/timers"
        )
    lines.append("")


def _render_lifecycle_events_section(
    snapshot: dict[str, Any],
    lines: list[str],
    *,
    max_lifecycle_events: int,
    max_field_chars: int,
) -> None:
    lifecycle_events = snapshot.get("lifecycle_events") or []
    if not lifecycle_events:
        return
    lines.append("Lifecycle events (recent):")
    for event in list(lifecycle_events)[: max(0, max_lifecycle_events)]:
        timestamp = _truncate(str(event.get("timestamp", "")), max_field_chars)
        event_type = _truncate(str(event.get("event_type", "")), max_field_chars)
        repo_id = _truncate(str(event.get("repo_id", "")), max_field_chars)
        run_id = _truncate(str(event.get("run_id", "")), max_field_chars)
        lines.append(f"- {timestamp} {event_type} repo_id={repo_id} run_id={run_id}")
    lines.append("")


def _render_hub_snapshot(
    snapshot: dict[str, Any],
    *,
    max_repos: int = PMA_MAX_REPOS,
    max_messages: int = PMA_MAX_MESSAGES,
    max_text_chars: int = PMA_MAX_TEXT,
    max_template_repos: int = PMA_MAX_TEMPLATE_REPOS,
    max_field_chars: int = PMA_MAX_TEMPLATE_FIELD_CHARS,
    max_pma_files: int = PMA_MAX_PMA_FILES,
    max_lifecycle_events: int = PMA_MAX_LIFECYCLE_EVENTS,
    max_pma_threads: int = PMA_MAX_PMA_THREADS,
    max_automation_items: int = PMA_MAX_AUTOMATION_ITEMS,
) -> str:
    lines: list[str] = []
    fc = max_field_chars
    _render_availability_section(
        snapshot,
        lines,
        max_field_chars=fc,
        max_text_chars=max_text_chars,
    )
    _render_freshness_section(snapshot, lines, fc)
    _render_process_monitor_section(snapshot, lines, max_field_chars=fc)
    _render_action_queue_section(
        snapshot,
        lines,
        max_messages=max_messages,
        max_field_chars=fc,
        max_text_chars=max_text_chars,
    )
    _render_inbox_section(
        snapshot,
        lines,
        max_messages=max_messages,
        max_field_chars=fc,
        max_text_chars=max_text_chars,
        max_pma_files=max_pma_files,
    )
    _render_repos_section(
        snapshot,
        lines,
        max_repos=max_repos,
        max_field_chars=fc,
        max_text_chars=max_text_chars,
    )
    _render_agent_workspaces_section(
        snapshot, lines, max_repos=max_repos, max_field_chars=fc
    )
    _render_templates_section(
        snapshot, lines, max_template_repos=max_template_repos, max_field_chars=fc
    )
    _render_pma_files_section(
        snapshot, lines, max_pma_files=max_pma_files, max_field_chars=fc
    )
    _render_pma_threads_section(
        snapshot, lines, max_pma_threads=max_pma_threads, max_field_chars=fc
    )
    _render_automation_section(
        snapshot, lines, max_automation_items=max_automation_items, max_field_chars=fc
    )
    _render_lifecycle_events_section(
        snapshot, lines, max_lifecycle_events=max_lifecycle_events, max_field_chars=fc
    )
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)
