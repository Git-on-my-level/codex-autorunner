from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from ..text_utils import _json_dumps
from ..time_utils import now_iso
from .models import (
    TRIGGER_KIND_EVENT,
    AutomationEvent,
    AutomationJob,
    AutomationRule,
    default_dedupe_key,
    normalize_timestamp,
)
from .store import AutomationStore

_TEMPLATE_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*}}")
_ALLOWED_TEMPLATE_ROOTS = frozenset(
    {"event", "repo", "target", "pr", "schedule", "job", "metadata"}
)


@dataclass(frozen=True)
class RuleEvaluationResult:
    event: AutomationEvent
    matched_rules: int
    jobs_created: int
    jobs_deduped: int
    jobs_skipped: int = 0


def _dig(context: Mapping[str, Any], path: str) -> Any:
    current: Any = context
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def render_template(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, dict):
        return {str(key): render_template(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_template(item, context) for item in value]
    if not isinstance(value, str):
        return value

    def _replace(match: re.Match[str]) -> str:
        path = match.group(1)
        root = path.split(".", 1)[0]
        if root not in _ALLOWED_TEMPLATE_ROOTS:
            raise ValueError(f"template root is not allowed: {root}")
        resolved = _dig(context, path)
        if resolved is None:
            return ""
        if isinstance(resolved, (dict, list)):
            return _json_dumps(resolved)
        return str(resolved)

    return _TEMPLATE_RE.sub(_replace, value)


class AutomationRuleEngine:
    def __init__(self, store: AutomationStore) -> None:
        self._store = store

    def record_event_and_enqueue_jobs(
        self, event: AutomationEvent
    ) -> RuleEvaluationResult:
        saved = self._store.record_event(event)
        return self.enqueue_jobs_for_event(saved)

    def enqueue_jobs_for_event(self, event: AutomationEvent) -> RuleEvaluationResult:
        matched = 0
        created = 0
        deduped = 0
        skipped = 0
        for rule in self._store.list_rules(
            enabled=True, trigger_kind=TRIGGER_KIND_EVENT
        ):
            if not self.matches_event(rule, event):
                continue
            matched += 1
            job = self._job_for_rule(rule, event)
            if self._blocked_by_policy(rule, event, job):
                skipped += 1
                continue
            _, was_deduped = self._store.enqueue_job(job)
            if was_deduped:
                deduped += 1
            else:
                created += 1
        return RuleEvaluationResult(
            event=event,
            matched_rules=matched,
            jobs_created=created,
            jobs_deduped=deduped,
            jobs_skipped=skipped,
        )

    def matches_event(self, rule: AutomationRule, event: AutomationEvent) -> bool:
        if rule.trigger_kind != TRIGGER_KIND_EVENT:
            return False
        event_types = rule.trigger.get("event_types")
        if isinstance(event_types, str):
            event_types = [event_types]
        if event_types and event.event_type not in {str(item) for item in event_types}:
            return False
        return self._filters_match(rule.filters, self._match_context(event, rule))

    def _job_for_rule(
        self, rule: AutomationRule, event: AutomationEvent
    ) -> AutomationJob:
        context = self._template_context(rule=rule, event=event, job={})
        target = render_template(rule.target, context)
        executor = render_template(
            {"kind": rule.executor_kind, **rule.executor}, context
        )
        policy = render_template(rule.policy, context)
        payload = render_template(
            {
                "event": event.to_dict(),
                "metadata": rule.metadata,
                "rule": {"rule_id": rule.rule_id, "name": rule.name},
            },
            context,
        )
        dedupe_key = self._render_policy_key(
            policy.get("dedupe_key"), context
        ) or default_dedupe_key(
            rule_id=rule.rule_id,
            event_id=event.event_id,
            target=target if isinstance(target, dict) else {},
        )
        batch_key = self._render_policy_key(policy.get("batch_key"), context)
        available_at = event.observed_at or now_iso()
        batch_window = _non_negative_int(policy.get("batch_window_seconds"))
        if batch_key and batch_window > 0:
            available_at = _add_seconds(available_at, batch_window)
        return AutomationJob.create(
            rule_id=rule.rule_id,
            event_id=event.event_id,
            target=target if isinstance(target, dict) else {},
            executor=executor if isinstance(executor, dict) else {},
            policy=policy if isinstance(policy, dict) else {},
            payload=payload if isinstance(payload, dict) else {},
            dedupe_key=dedupe_key,
            batch_key=batch_key,
            available_at=available_at,
        )

    def _blocked_by_policy(
        self, rule: AutomationRule, event: AutomationEvent, job: AutomationJob
    ) -> bool:
        policy = job.policy
        cooldown = _non_negative_int(policy.get("cooldown_seconds"))
        if cooldown and self._store.has_recent_job_for_rule(
            rule.rule_id, since=_add_seconds(event.observed_at, -cooldown)
        ):
            return True
        max_runs = _non_negative_int(policy.get("max_runs_per_hour"))
        if (
            max_runs
            and self._store.count_jobs_for_rule_since(
                rule.rule_id, since=_add_seconds(event.observed_at, -3600)
            )
            >= max_runs
        ):
            return True
        return False

    def _filters_match(
        self, filters: Mapping[str, Any], context: Mapping[str, Any]
    ) -> bool:
        for key, expected in filters.items():
            path = self._filter_path(str(key))
            actual = _dig(context, path)
            if not self._match_filter(actual, expected, context):
                return False
        return True

    def _match_filter(
        self, actual: Any, expected: Any, context: Mapping[str, Any]
    ) -> bool:
        if isinstance(expected, Mapping):
            if "path" in expected:
                actual = _dig(context, str(expected["path"]))
            if "exists" in expected:
                return (actual is not None) is bool(expected["exists"])
            if "eq" in expected:
                return bool(actual == expected["eq"])
            if "ne" in expected:
                return bool(actual != expected["ne"])
            if "in" in expected:
                values = expected["in"]
                return isinstance(values, list) and actual in values
            if "not_in" in expected:
                values = expected["not_in"]
                return isinstance(values, list) and actual not in values
            if "contains" in expected:
                needle = expected["contains"]
                return isinstance(actual, (list, str)) and needle in actual
            return all(
                self._match_filter(
                    actual.get(str(key)) if isinstance(actual, Mapping) else None,
                    value,
                    context,
                )
                for key, value in expected.items()
            )
        if isinstance(expected, list):
            return actual in expected
        return bool(actual == expected)

    def _filter_path(self, key: str) -> str:
        if "." in key:
            return key
        if key in {"repo_id", "event_type", "source", "observed_at"}:
            return f"event.{key}"
        return f"event.payload.{key}"

    def _match_context(
        self, event: AutomationEvent, rule: Optional[AutomationRule] = None
    ) -> dict[str, Any]:
        return self._template_context(rule=rule, event=event, job={})

    def _template_context(
        self, *, rule: Optional[AutomationRule], event: AutomationEvent, job: Any
    ) -> dict[str, Any]:
        payload = event.payload if isinstance(event.payload, dict) else {}
        target = event.target if isinstance(event.target, dict) else {}
        schedule = dict(
            payload.get("schedule", {})
            if isinstance(payload.get("schedule"), dict)
            else {}
        )
        schedule_config_raw = schedule.get("schedule")
        schedule_config = (
            dict(schedule_config_raw) if isinstance(schedule_config_raw, dict) else {}
        )
        target_repo_raw = target.get("repo")
        return {
            "event": event.to_dict(),
            "repo": {
                "repo_id": event.repo_id,
                **(dict(target_repo_raw) if isinstance(target_repo_raw, dict) else {}),
            },
            "target": dict(target),
            "pr": dict(
                payload.get("pr", {}) if isinstance(payload.get("pr"), dict) else {}
            ),
            "schedule": {**schedule, **schedule_config},
            "job": dict(job if isinstance(job, dict) else {}),
            "metadata": {
                **dict(event.metadata),
                **dict(rule.metadata if rule is not None else {}),
            },
        }

    def _render_policy_key(
        self, value: Any, context: Mapping[str, Any]
    ) -> Optional[str]:
        if value is None:
            return None
        rendered = render_template(value, context)
        if isinstance(rendered, str):
            text = rendered.strip()
            return text or None
        return _json_dumps(rendered)


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _add_seconds(value: str, seconds: int) -> str:
    parsed = datetime.fromisoformat(normalize_timestamp(value).replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["AutomationRuleEngine", "RuleEvaluationResult", "render_template"]
