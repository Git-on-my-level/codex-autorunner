from codex_autorunner.core.orchestration.assistant_output_assembly import (
    AssistantOutputAssembler,
    AssistantOutputEvent,
    assemble_assistant_output,
)


def test_final_message_wins_over_stream_and_commentary() -> None:
    assert (
        assemble_assistant_output(
            [
                AssistantOutputEvent(kind="commentary", text="Let me check."),
                AssistantOutputEvent(kind="delta", text="damaged stream"),
                AssistantOutputEvent(kind="final_message", text="Clean final."),
            ]
        )
        == "Clean final."
    )


def test_scoped_stream_text_does_not_bleed_across_snapshots() -> None:
    assembler = AssistantOutputAssembler()
    assembler.note(
        AssistantOutputEvent(kind="snapshot", text="First message", scope="m1")
    )
    assembler.note(
        AssistantOutputEvent(kind="snapshot", text="Second message", scope="m2")
    )
    assembler.note(AssistantOutputEvent(kind="final_message", text="", scope="m2"))

    assert assembler.stream_text == "Second message"


def test_final_scope_uses_matching_stream_scope_when_final_text_missing() -> None:
    assembler = AssistantOutputAssembler()
    assembler.note(AssistantOutputEvent(kind="delta", text="progress", scope="m1"))
    assembler.note(AssistantOutputEvent(kind="delta", text="final answer", scope="m2"))
    assembler.note(AssistantOutputEvent(kind="final_message", text="", scope="m2"))

    assert assembler.text == "final answer"
