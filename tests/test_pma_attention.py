from pathlib import Path

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.pma_attention import (
    build_pma_attention_state,
    render_pma_attention_state,
)
from codex_autorunner.core.pma_context import format_pma_prompt


def test_attention_state_renders_idle_overview_without_queue_prose() -> None:
    snapshot = {
        "generated_at": "2026-05-31T10:00:00Z",
        "repos": [{"id": "repo-1", "status": "clean"}],
        "managed_threads": [],
        "pma_files_detail": {"inbox": [], "outbox": []},
        "action_queue": [
            {
                "item_type": "managed_thread_followup_summary",
                "queue_source": "managed_thread_followup",
                "action_queue_id": "managed_thread_followup_summary:protected_chat_bound",
                "name": "Protected chat-bound threads (8)",
                "thread_count": 8,
                "followup_state": "protected_chat_bound",
                "operator_need": "protected",
                "why_selected": "Collapsed protected threads into inventory.",
                "recommended_detail": "Show protected threads.",
                "supersession": {"status": "non_primary", "superseded": False},
            },
            {
                "item_type": "pma_file_summary",
                "queue_source": "pma_file_inbox",
                "action_queue_id": "pma_file_inbox_summary:stale_uploaded_files",
                "name": "Stale uploaded files (3)",
                "file_count": 3,
                "next_action": "review_stale_uploaded_file",
                "why_selected": "Collapsed stale files into inventory.",
                "supersession": {"status": "non_primary", "superseded": False},
                "likely_false_positive": True,
            },
        ],
        "freshness": {"stale_sections": ["managed_threads", "pma_file_inbox"]},
    }

    rendered = render_pma_attention_state(
        build_pma_attention_state(snapshot),
        mode="overview",
    )

    assert "<pma v=2 mode=overview state=idle fresh=idle actions=0>" in rendered
    assert "q:\n  none" in rendered
    assert "threads protected=8" in rendered
    assert "files fresh=0 stale=3" in rendered
    assert "car hub snapshot --section managed_threads" in rendered
    assert "why_selected" not in rendered
    assert "recommended_detail" not in rendered
    assert "precedence" not in rendered


def test_attention_state_combines_recommendation_and_drilldown_command() -> None:
    snapshot = {
        "repos": [],
        "managed_threads": [],
        "pma_files_detail": {"inbox": [], "outbox": []},
        "action_queue": [
            {
                "item_type": "run_dispatch",
                "queue_source": "ticket_flow_inbox",
                "action_queue_id": "ticket_flow_inbox:discord-1:run-123:3",
                "repo_id": "discord-1",
                "run_id": "run-123",
                "recommended_action": "reply_and_resume",
                "supersession": {"status": "primary", "superseded": False},
                "freshness": {"is_stale": False},
                "scope": {"kind": "run", "key": "run:run-123"},
            }
        ],
    }

    rendered = render_pma_attention_state(
        build_pma_attention_state(snapshot),
        mode="overview",
    )

    assert "<pma v=2 mode=overview state=action fresh=ok actions=1>" in rendered
    assert "src=dispatch" in rendered
    assert "repo=discord-1" in rendered
    assert "run=run-123" in rendered
    assert 'cmd="car hub snapshot --section action_queue"' in rendered
    assert "recommended_action" not in rendered


def test_attention_state_uses_required_thread_info_id_option() -> None:
    snapshot = {
        "repos": [],
        "managed_threads": [],
        "pma_files_detail": {"inbox": [], "outbox": []},
        "action_queue": [
            {
                "item_type": "managed_thread_followup",
                "queue_source": "managed_thread_followup",
                "action_queue_id": "managed_thread_followup:thread-123",
                "managed_thread_id": "thread-123",
                "followup_state": "attention_required",
                "supersession": {"status": "primary", "superseded": False},
                "freshness": {"is_stale": False},
                "scope": {"kind": "thread", "key": "thread:thread-123"},
            }
        ],
    }

    rendered = render_pma_attention_state(
        build_pma_attention_state(snapshot),
        mode="overview",
    )

    assert 'cmd="car pma thread info --id thread-123"' in rendered
    assert "car pma thread info thread-123" not in rendered


def test_attention_state_falls_back_to_raw_inbox_when_queue_is_absent() -> None:
    snapshot = {
        "inbox": [
            {
                "item_type": "run_dispatch",
                "repo_id": "discord-2",
                "run_id": "run-raw",
                "next_action": "reply_and_resume",
                "canonical_state_v1": {"freshness": {"is_stale": False}},
            }
        ],
        "repos": [],
        "pma_files_detail": {"inbox": [], "outbox": []},
    }

    rendered = render_pma_attention_state(
        build_pma_attention_state(snapshot),
        mode="overview",
    )

    assert "state=action" in rendered
    assert "src=dispatch" in rendered
    assert 'cmd="car hub snapshot --section action_queue"' in rendered


def test_attention_state_exposes_action_overflow() -> None:
    action_queue = []
    for index in range(4):
        action_queue.append(
            {
                "item_type": "run_dispatch",
                "queue_source": "ticket_flow_inbox",
                "action_queue_id": f"ticket_flow_inbox:repo-{index}:run-{index}:1",
                "repo_id": f"repo-{index}",
                "run_id": f"run-{index}",
                "supersession": {"status": "non_primary", "superseded": False},
                "freshness": {"is_stale": False},
            }
        )
    snapshot = {
        "repos": [],
        "managed_threads": [],
        "pma_files_detail": {"inbox": [], "outbox": []},
        "action_queue": action_queue,
    }

    rendered = render_pma_attention_state(
        build_pma_attention_state(snapshot),
        mode="overview",
    )

    assert "actions=4" in rendered
    assert '+1 more cmd="car hub snapshot --section action_queue"' in rendered
    assert "car hub snapshot --section action_queue" in rendered


def test_prompt_delta_does_not_collapse_when_action_semantics_change(
    tmp_path: Path,
) -> None:
    seed_hub_files(tmp_path, force=True)
    base_queue_item = {
        "item_type": "run_dispatch",
        "queue_source": "ticket_flow_inbox",
        "action_queue_id": "ticket_flow_inbox:discord-1:run-123:3",
        "repo_id": "discord-1",
        "run_id": "run-123",
        "recommended_action": "reply_and_resume",
        "recommended_detail": "Answer the dispatch.",
        "supersession": {"status": "primary", "superseded": False},
        "freshness": {"is_stale": False},
    }
    snapshot1 = {
        "generated_at": "2026-05-31T10:00:00Z",
        "repos": [],
        "managed_threads": [],
        "pma_files_detail": {"inbox": [], "outbox": []},
        "action_queue": [base_queue_item],
    }
    changed_queue_item = dict(base_queue_item)
    changed_queue_item["recommended_action"] = "inspect_and_resume"
    changed_queue_item["recommended_detail"] = "Inspect the new blocker."
    snapshot2 = dict(snapshot1)
    snapshot2["generated_at"] = "2026-05-31T10:05:00Z"
    snapshot2["action_queue"] = [changed_queue_item]

    _ = format_pma_prompt(
        "Base prompt",
        snapshot1,
        "Turn 1",
        hub_root=tmp_path,
        prompt_state_key="pma.attention.changed-semantics",
    )
    result = format_pma_prompt(
        "Base prompt",
        snapshot2,
        "Turn 2",
        hub_root=tmp_path,
        prompt_state_key="pma.attention.changed-semantics",
    )

    assert "<context_unchanged />" not in result
    assert "<current_actionable_state>" in result
    assert "semantic_ref=" in result


def test_prompt_delta_collapses_when_attention_is_unchanged(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    snapshot1 = {
        "generated_at": "2026-05-31T10:00:00Z",
        "repos": [],
        "managed_threads": [],
        "pma_files_detail": {"inbox": [], "outbox": []},
        "action_queue": [],
    }
    snapshot2 = dict(snapshot1)
    snapshot2["generated_at"] = "2026-05-31T10:05:00Z"

    _ = format_pma_prompt(
        "Base prompt",
        snapshot1,
        "Turn 1",
        hub_root=tmp_path,
        prompt_state_key="pma.attention.same",
    )
    result = format_pma_prompt(
        "Base prompt",
        snapshot2,
        "Turn 2",
        hub_root=tmp_path,
        prompt_state_key="pma.attention.same",
    )

    assert "<context_unchanged />" in result
    assert "<current_actionable_state>" not in result
    assert "<hub_snapshot_ref " not in result
    assert "<user_message>\nTurn 2\n</user_message>" in result
