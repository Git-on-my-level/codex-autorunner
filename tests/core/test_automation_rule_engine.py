from __future__ import annotations

from codex_autorunner.core.automation import (
    AutomationEvent,
    AutomationRule,
    AutomationRuleEngine,
    AutomationStore,
    render_template,
)
from codex_autorunner.core.automation.models import (
    EXECUTOR_MANAGED_THREAD_TURN,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
    TRIGGER_KIND_MANUAL,
)


def _rule(**overrides):
    kwargs = {
        "rule_id": "rule-1",
        "name": "PR review",
        "trigger_kind": TRIGGER_KIND_EVENT,
        "trigger": {"event_types": ["scm.github.pull_request_review.submitted"]},
        "filters": {"repo_id": "repo-1", "pr.number": {"eq": 42}},
        "target_policy": TARGET_POLICY_HUB,
        "target": {"repo_id": "{{ event.repo_id }}", "pr": "{{ pr.number }}"},
        "executor_kind": EXECUTOR_MANAGED_THREAD_TURN,
        "executor": {
            "lane_id": "pma:default",
            "message": "Review PR {{ pr.number }} in {{ event.repo_id }}",
        },
        "policy": {
            "dedupe_key": "review:{{ event.event_id }}",
            "batch_key": "repo:{{ event.repo_id }}",
            "batch_window_seconds": 30,
            "cooldown_seconds": 0,
        },
    }
    kwargs.update(overrides)
    return AutomationRule.create(**kwargs)


def _event(event_id="event-1"):
    return AutomationEvent.create(
        event_id=event_id,
        event_type="scm.github.pull_request_review.submitted",
        observed_at="2026-01-01T00:00:00Z",
        source="github",
        repo_id="repo-1",
        target={"repo_id": "repo-1"},
        payload={"pr": {"number": 42}, "author": "dev"},
    )


def test_template_renderer_only_expands_allowlisted_dot_paths() -> None:
    rendered = render_template(
        {"message": "PR {{ pr.number }} for {{ event.repo_id }}"},
        {"event": {"repo_id": "repo-1"}, "pr": {"number": 42}},
    )

    assert rendered == {"message": "PR 42 for repo-1"}


def test_rule_engine_matches_filters_and_enqueues_templated_job(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule())

    result = AutomationRuleEngine(store).record_event_and_enqueue_jobs(_event())

    assert result.matched_rules == 1
    assert result.jobs_created == 1
    job = store.list_jobs()[0]
    assert job.dedupe_key == "review:event-1"
    assert job.batch_key == "repo:repo-1"
    assert job.available_at == "2026-01-01T00:00:30Z"
    assert job.executor["message"] == "Review PR 42 in repo-1"


def test_rule_engine_dedupe_batch_cooldown_and_max_runs_policy(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule(policy={"dedupe_key": "stable", "cooldown_seconds": 60}))
    engine = AutomationRuleEngine(store)

    first = engine.record_event_and_enqueue_jobs(_event("event-1"))
    duplicate = engine.record_event_and_enqueue_jobs(_event("event-2"))

    assert first.jobs_created == 1
    assert duplicate.jobs_skipped == 1
    assert len(store.list_jobs()) == 1

    store.upsert_rule(
        _rule(
            rule_id="rule-2",
            policy={"dedupe_key": "{{ event.event_id }}", "max_runs_per_hour": 1},
        )
    )
    result = engine.record_event_and_enqueue_jobs(_event("event-3"))
    assert result.jobs_skipped >= 1


def test_rule_engine_dedupe_survives_store_restart(tmp_path) -> None:
    first_store = AutomationStore(tmp_path)
    first_store.upsert_rule(_rule(policy={"dedupe_key": "stable"}))
    first = AutomationRuleEngine(first_store).record_event_and_enqueue_jobs(
        _event("event-1")
    )

    restarted_store = AutomationStore(tmp_path)
    duplicate = AutomationRuleEngine(restarted_store).record_event_and_enqueue_jobs(
        _event("event-2")
    )

    assert first.jobs_created == 1
    assert duplicate.jobs_deduped == 1
    assert [job.dedupe_key for job in restarted_store.list_jobs()] == ["stable"]


def test_rule_engine_records_event_but_does_not_enqueue_disabled_rule(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule(enabled=False))

    result = AutomationRuleEngine(store).record_event_and_enqueue_jobs(_event())

    assert result.matched_rules == 0
    assert result.jobs_created == 0
    assert store.get_event("event-1") is not None
    assert store.list_jobs() == []


def test_rule_engine_selected_manual_rule_uses_templating_and_policy(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    rule = AutomationRule.create(
        rule_id="manual-rule",
        name="Manual wakeup",
        trigger_kind=TRIGGER_KIND_MANUAL,
        target_policy=TARGET_POLICY_HUB,
        target={"repo_id": "{{ event.target.repo_id }}"},
        executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
        executor={"message": "Wake {{ event.payload.prompt }}"},
        policy={"dedupe_key": "{{ metadata.manual_dedupe_key }}"},
    )
    store.upsert_rule(rule)
    event = store.record_event(
        AutomationEvent.create(
            event_id="manual-event",
            event_type="manual.run",
            target={"repo_id": "repo-1"},
            payload={"prompt": "now"},
            metadata={"manual_dedupe_key": "manual-key"},
        )
    )

    result = AutomationRuleEngine(store).enqueue_job_for_rule(rule, event)

    assert result.jobs_created == 1
    job = store.list_jobs()[0]
    assert job.dedupe_key == "manual-key"
    assert job.target["repo_id"] == "repo-1"
    assert job.executor["message"] == "Wake now"


def test_rule_engine_rejects_non_matching_event_type_and_filter(tmp_path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert_rule(_rule(filters={"repo_id": "other"}))

    result = AutomationRuleEngine(store).record_event_and_enqueue_jobs(_event())

    assert result.matched_rules == 0
    assert store.list_jobs() == []
