from codex_autorunner.core.orchestration.chat_architecture_goal import (
    CHAT_ARCHITECTURE_GOAL_CRITERIA,
    CURRENT_CHAT_ARCHITECTURE_SIGNALS,
    NEGATIVE_SIGNALS,
    POSITIVE_SIGNALS,
    ChatArchitectureSignal,
    current_chat_architecture_goal_evaluation,
    evaluate_chat_architecture_goal,
)


def test_chat_architecture_goal_is_satisfied_by_platonic_evidence() -> None:
    evaluation = evaluate_chat_architecture_goal(POSITIVE_SIGNALS)

    assert evaluation.satisfied
    assert evaluation.score == evaluation.max_score
    assert evaluation.normalized_score == 1.0
    assert evaluation.gaps == ()


def test_chat_architecture_goal_penalizes_surface_owned_state() -> None:
    signals = set(POSITIVE_SIGNALS)
    signals.add(ChatArchitectureSignal.SURFACE_LOCAL_LIFECYCLE_MACHINE)

    evaluation = evaluate_chat_architecture_goal(signals)

    assert not evaluation.satisfied
    assert evaluation.score < evaluation.max_score
    top_gap = evaluation.top_gaps(limit=1)[0]
    assert top_gap.criterion_id == "one_control_plane_state_machine"
    assert (
        ChatArchitectureSignal.SURFACE_LOCAL_LIFECYCLE_MACHINE
        in top_gap.blocking_signals
    )


def test_chat_architecture_goal_current_state_names_known_gaps() -> None:
    evaluation = current_chat_architecture_goal_evaluation()

    assert evaluation.score < evaluation.max_score
    blocking = {signal for gap in evaluation.gaps for signal in gap.blocking_signals}
    assert ChatArchitectureSignal.SURFACE_LOCAL_LIFECYCLE_MACHINE in blocking
    assert ChatArchitectureSignal.OPAQUE_STATUS_STRING_MAPPING in blocking


def test_chat_architecture_goal_has_no_unclassified_signals() -> None:
    classified = POSITIVE_SIGNALS | NEGATIVE_SIGNALS

    assert set(ChatArchitectureSignal) == classified


def test_chat_architecture_goal_criteria_have_stable_machine_readable_ids() -> None:
    ids = [criterion.criterion_id for criterion in CHAT_ARCHITECTURE_GOAL_CRITERIA]

    assert ids == sorted(set(ids), key=ids.index)
    assert all(criterion.weight > 0 for criterion in CHAT_ARCHITECTURE_GOAL_CRITERIA)
    assert set(CURRENT_CHAT_ARCHITECTURE_SIGNALS) <= set(ChatArchitectureSignal)
