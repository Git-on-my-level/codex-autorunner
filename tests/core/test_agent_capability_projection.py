from codex_autorunner.core.agent_capability_projection import (
    project_agent_capabilities,
    project_thread_capabilities,
)


def test_agent_projection_allows_durable_message_turn_model_listing_and_controls() -> (
    None
):
    projection = project_agent_capabilities(
        "codex",
        [
            "durable_threads",
            "message_turns",
            "model_listing",
            "interrupt",
            "approvals",
        ],
    )

    assert projection.gate("list_models").allowed is True
    assert projection.gate("use_durable_threads").allowed is True
    assert projection.gate("use_message_turns").allowed is True
    assert projection.gate("use_file_mentions").allowed is True
    assert projection.gate("use_approvals").allowed is True
    assert projection.gate("use_sandbox_policy").allowed is True
    assert projection.gate("use_model_overrides").allowed is True


def test_agent_projection_reports_missing_model_listing_and_approvals() -> None:
    projection = project_agent_capabilities(
        "hermes",
        ["durable_threads", "message_turns", "interrupt"],
    )

    list_models = projection.gate("list_models")
    approvals = projection.gate("use_approvals")

    assert list_models.allowed is False
    assert list_models.missing_capabilities == ("model_listing",)
    assert "model_listing" in (list_models.reason or "")
    assert approvals.allowed is False
    assert approvals.missing_capabilities == ("approvals",)


def test_thread_projection_gates_interrupt_by_capability_and_running_turn() -> None:
    missing_capability = project_thread_capabilities(
        thread_id="thread-1",
        agent_id="basic",
        capabilities=["durable_threads", "message_turns"],
        has_running_turn=True,
    ).gate("interrupt_thread")
    idle_thread = project_thread_capabilities(
        thread_id="thread-2",
        agent_id="codex",
        capabilities=["durable_threads", "message_turns", "interrupt"],
        has_running_turn=False,
    ).gate("interrupt_thread")
    running_thread = project_thread_capabilities(
        thread_id="thread-3",
        agent_id="codex",
        capabilities=["durable_threads", "message_turns", "interrupt"],
        has_running_turn=True,
    ).gate("interrupt_thread")

    assert missing_capability.allowed is False
    assert missing_capability.missing_capabilities == ("interrupt",)
    assert idle_thread.allowed is False
    assert idle_thread.missing_capabilities == ()
    assert idle_thread.reason == "Managed thread has no active turn"
    assert running_thread.allowed is True
