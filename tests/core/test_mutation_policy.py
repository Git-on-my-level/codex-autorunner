from __future__ import annotations

from codex_autorunner.core.mutation_policy import evaluate


def test_evaluate_defaults_allow_internal_actions_and_deny_write_mutations() -> None:
    allowed = (
        evaluate("enqueue_managed_turn"),
        evaluate("notify_chat"),
    )
    denied = (
        evaluate("post_pr_comment", provider="github"),
        evaluate("add_labels", provider="github"),
        evaluate("merge_pr", provider="github"),
    )

    assert [decision.decision for decision in allowed] == ["allow", "allow"]
    assert [decision.decision for decision in denied] == ["deny", "deny", "deny"]
    assert all(decision.source == "default" for decision in (*allowed, *denied))


def test_evaluate_honors_nested_config_overrides() -> None:
    config = {
        "github": {
            "automation": {
                "policy": {
                    "post_pr_comment": "allow",
                    "merge_pr": "require_approval",
                }
            }
        }
    }

    post_comment = evaluate(
        "post_pr_comment",
        provider="github",
        repo_id="repo-1",
        binding_id="binding-1",
        config=config,
    )
    merge_pr = evaluate("merge_pr", provider="github", config=config)

    assert post_comment.allowed is True
    assert post_comment.source == "config"
    assert post_comment.repo_id == "repo-1"
    assert post_comment.binding_id == "binding-1"
    assert merge_pr.requires_approval is True
    assert merge_pr.source == "config"


def test_evaluate_accepts_boolean_policy_aliases() -> None:
    config = {
        "post_pr_comment": True,
        "notify_chat": False,
    }

    assert evaluate("post_pr_comment", config=config).decision == "allow"
    assert evaluate("notify_chat", config=config).decision == "deny"


def test_evaluate_denies_unknown_actions_by_default() -> None:
    decision = evaluate("unknown_action", provider="github")

    assert decision.denied is True
    assert decision.source == "default"
    assert (
        decision.reason == "Unknown mutation action 'unknown_action' defaults to deny"
    )
