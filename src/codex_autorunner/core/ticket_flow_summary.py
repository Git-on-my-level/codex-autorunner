from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from ..tickets.files import list_ticket_paths
from ..tickets.frontmatter import parse_markdown_frontmatter
from ..tickets.lint import parse_ticket_index
from .config import load_repo_config
from .flows import FlowStore
from .flows.failure_diagnostics import format_failure_summary, get_failure_payload
from .flows.models import FlowRunRecord

_PR_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)


def _extract_pr_url_from_ticket(path: Path) -> Optional[str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    data, body = parse_markdown_frontmatter(raw)
    if isinstance(data, dict):
        frontmatter_pr = data.get("pr_url")
        if isinstance(frontmatter_pr, str) and frontmatter_pr.strip():
            return frontmatter_pr.strip()
    match = _PR_URL_RE.search(body or "")
    if match:
        return match.group(0)
    return None


def get_latest_ticket_flow_run(store: FlowStore) -> Optional[FlowRunRecord]:
    runs = store.list_flow_runs(flow_type="ticket_flow")
    return runs[0] if runs else None


def build_ticket_flow_summary(
    repo_path: Path,
    *,
    include_failure: bool,
) -> Optional[dict[str, Any]]:
    db_path = repo_path / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None
    try:
        config = load_repo_config(repo_path)
        with FlowStore(db_path, durable=config.durable_writes) as store:
            latest = get_latest_ticket_flow_run(store)
            if not latest:
                return None
    except Exception:
        return None

    ticket_dir = repo_path / ".codex-autorunner" / "tickets"
    ticket_paths = list_ticket_paths(ticket_dir)
    if not ticket_paths:
        return None

    total_count = len(ticket_paths)
    done_count = 0
    open_pr_ticket_url: Optional[str] = None
    final_review_status: Optional[str] = None
    for path in ticket_paths:
        idx = parse_ticket_index(path.name)
        if idx is None:
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        data, _body = parse_markdown_frontmatter(raw)
        if not isinstance(data, dict):
            continue
        done = data.get("done")
        done_flag = bool(done) if isinstance(done, bool) else False
        if done_flag:
            done_count += 1

        title = str(data.get("title") or "").strip().lower()
        ticket_kind = str(data.get("ticket_kind") or "").strip().lower()
        is_final_review = ticket_kind == "final_review" or "final review" in title
        if is_final_review:
            final_review_status = "done" if done_flag else "pending"

        is_open_pr = (
            ticket_kind == "open_pr" or "open pr" in title or "pull request" in title
        )
        if is_open_pr:
            open_pr_ticket_url = _extract_pr_url_from_ticket(path)

    pr_url = open_pr_ticket_url

    state = latest.state if isinstance(latest.state, dict) else {}
    engine = state.get("ticket_engine") if isinstance(state, dict) else {}
    engine = engine if isinstance(engine, dict) else {}
    current_step = engine.get("total_turns")

    summary: dict[str, Any] = {
        "status": latest.status.value,
        "done_count": done_count,
        "total_count": total_count,
        "current_step": current_step,
        "pr_url": pr_url,
        "pr_opened": bool(pr_url),
        "final_review_status": final_review_status,
    }
    if include_failure:
        failure_payload = get_failure_payload(latest)
        summary["failure"] = failure_payload
        summary["failure_summary"] = (
            format_failure_summary(failure_payload) if failure_payload else None
        )
    return summary
