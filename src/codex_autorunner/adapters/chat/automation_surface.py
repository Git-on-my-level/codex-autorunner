from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.automation.product import (
    automation_overview,
    automation_row,
    automation_store,
    format_automation_list,
    format_automation_status,
    run_automation_now,
    set_automation_enabled,
)

USAGE = (
    "Usage: automation list | automation status <id> | automation run <id> | "
    "automation pause <id> | automation resume <id>"
)


def list_automations_for_chat(hub_root: Path, *, limit: int = 10) -> str:
    store = automation_store(hub_root)
    return format_automation_list(automation_overview(store, limit=limit), limit=limit)


def automation_status_for_chat(hub_root: Path, rule_id: str) -> str:
    store = automation_store(hub_root)
    rule = _resolve_rule(store, rule_id)
    return format_automation_status(automation_row(store, rule))


def run_automation_for_chat(
    hub_root: Path,
    rule_id: str,
    *,
    source: str,
    supervisor: Any = None,
) -> str:
    store = automation_store(hub_root)
    rule = _resolve_rule(store, rule_id)
    result = run_automation_now(
        store,
        rule.rule_id,
        source=source,
        supervisor=supervisor,
    )
    return (
        f"Queued automation: {rule.name}\n"
        f"Jobs created: {result.get('jobs_created', 0)} "
        f"(deduped: {result.get('jobs_deduped', 0)})"
    )


def set_automation_enabled_for_chat(
    hub_root: Path,
    rule_id: str,
    *,
    enabled: bool,
) -> str:
    store = automation_store(hub_root)
    rule = _resolve_rule(store, rule_id)
    row = set_automation_enabled(store, rule.rule_id, enabled)
    state = "resumed" if enabled else "paused"
    return f"Automation {state}: {row.get('name') or row.get('id')}"


def _resolve_rule(store: Any, rule_id: str) -> Any:
    query = str(rule_id or "").strip()
    if not query:
        raise ValueError("automation id is required")
    exact = store.get_rule(query)
    if exact is not None:
        return exact
    matches = [
        rule
        for rule in store.list_rules()
        if rule.rule_id.startswith(query) or query in rule.rule_id
    ]
    if not matches:
        raise KeyError(query)
    if len(matches) > 1:
        choices = ", ".join(rule.rule_id for rule in matches[:5])
        raise ValueError(f"automation id is ambiguous: {choices}")
    return matches[0]


__all__ = [
    "USAGE",
    "automation_status_for_chat",
    "list_automations_for_chat",
    "run_automation_for_chat",
    "set_automation_enabled_for_chat",
]
