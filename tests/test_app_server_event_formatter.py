import pytest

from codex_autorunner.core.app_server_logging import AppServerEventFormatter


@pytest.mark.anyio
async def test_thinking_deltas_buffered_until_part_added() -> None:
    formatter = AppServerEventFormatter()

    item_id = "reasoning-item-123"
    turn_id = "turn-1"

    # Simulate multiple deltas being received
    delta1_msg = {
        "method": "item/reasoning/summaryTextDelta",
        "params": {
            "itemId": item_id,
            "turnId": turn_id,
            "delta": "I am",
        },
    }
    delta2_msg = {
        "method": "item/reasoning/summaryTextDelta",
        "params": {
            "itemId": item_id,
            "turnId": turn_id,
            "delta": " thinking",
        },
    }
    delta3_msg = {
        "method": "item/reasoning/summaryTextDelta",
        "params": {
            "itemId": item_id,
            "turnId": turn_id,
            "delta": " about this",
        },
    }

    # First delta should only emit "thinking" label
    lines = formatter.format_event(delta1_msg)
    assert lines == ["thinking"]

    # Second and third deltas should emit nothing (buffered)
    lines = formatter.format_event(delta2_msg)
    assert lines == []
    lines = formatter.format_event(delta3_msg)
    assert lines == []

    # summaryPartAdded should emit all buffered lines
    part_added_msg = {
        "method": "item/reasoning/summaryPartAdded",
        "params": {
            "itemId": item_id,
        },
    }
    lines = formatter.format_event(part_added_msg)
    assert lines == ["**I am thinking about this**"]


@pytest.mark.anyio
async def test_thinking_deltas_with_multiple_parts() -> None:
    formatter = AppServerEventFormatter()

    item_id = "reasoning-item-456"
    turn_id = "turn-2"

    # First part
    delta1_msg = {
        "method": "item/reasoning/summaryTextDelta",
        "params": {
            "itemId": item_id,
            "turnId": turn_id,
            "delta": "Line one",
        },
    }
    part_added1 = {
        "method": "item/reasoning/summaryPartAdded",
        "params": {"itemId": item_id},
    }

    lines = formatter.format_event(delta1_msg)
    assert lines == ["thinking"]

    lines = formatter.format_event(part_added1)
    assert lines == ["**Line one**"]

    # Second part
    delta2_msg = {
        "method": "item/reasoning/summaryTextDelta",
        "params": {
            "itemId": item_id,
            "turnId": turn_id,
            "delta": "Line two",
        },
    }
    part_added2 = {
        "method": "item/reasoning/summaryPartAdded",
        "params": {"itemId": item_id},
    }

    lines = formatter.format_event(delta2_msg)
    assert lines == []

    lines = formatter.format_event(part_added2)
    assert lines == ["**Line two**"]


@pytest.mark.anyio
async def test_thinking_committed_on_item_completed() -> None:
    formatter = AppServerEventFormatter()

    item_id = "reasoning-item-789"
    turn_id = "turn-3"

    # Simulate deltas that never get a summaryPartAdded
    delta1_msg = {
        "method": "item/reasoning/summaryTextDelta",
        "params": {
            "itemId": item_id,
            "turnId": turn_id,
            "delta": "Final thought",
        },
    }

    lines = formatter.format_event(delta1_msg)
    assert lines == ["thinking"]

    # When reasoning item completes, any remaining buffer should be emitted
    item_completed_msg = {
        "method": "item/completed",
        "params": {
            "itemId": item_id,
            "item": {"id": item_id, "type": "reasoning"},
        },
    }

    lines = formatter.format_event(item_completed_msg)
    assert lines == ["**Final thought**"]


@pytest.mark.anyio
async def test_thinking_reset_clears_buffers() -> None:
    formatter = AppServerEventFormatter()

    item_id = "reasoning-item-999"
    turn_id = "turn-4"

    delta_msg = {
        "method": "item/reasoning/summaryTextDelta",
        "params": {
            "itemId": item_id,
            "turnId": turn_id,
            "delta": "This will be cleared",
        },
    }

    lines = formatter.format_event(delta_msg)
    assert lines == ["thinking"]

    # Reset should clear all buffers
    formatter.reset()

    # After reset, item completion should not emit anything
    item_completed_msg = {
        "method": "item/completed",
        "params": {
            "itemId": item_id,
            "item": {"id": item_id, "type": "reasoning"},
        },
    }

    lines = formatter.format_event(item_completed_msg)
    assert lines == []


@pytest.mark.anyio
async def test_thinking_multiline_deltas() -> None:
    formatter = AppServerEventFormatter()

    item_id = "reasoning-item-multi"
    turn_id = "turn-5"

    # Delta with multiple lines
    delta_msg = {
        "method": "item/reasoning/summaryTextDelta",
        "params": {
            "itemId": item_id,
            "turnId": turn_id,
            "delta": "Line 1\nLine 2\nLine 3",
        },
    }

    lines = formatter.format_event(delta_msg)
    assert lines == ["thinking"]

    # Part added should emit all lines
    part_added_msg = {
        "method": "item/reasoning/summaryPartAdded",
        "params": {"itemId": item_id},
    }

    lines = formatter.format_event(part_added_msg)
    assert lines == ["**Line 1**", "**Line 2**", "**Line 3**"]


@pytest.mark.anyio
async def test_thinking_deltas_without_itemid_emit_immediately() -> None:
    formatter = AppServerEventFormatter()

    # Delta without itemId - should emit immediately
    delta_msg = {
        "method": "item/reasoning/summaryTextDelta",
        "params": {
            "turnId": "turn-6",
            "delta": "This has no itemId",
        },
    }

    lines = formatter.format_event(delta_msg)
    assert lines == ["thinking", "**This has no itemId**"]


@pytest.mark.anyio
async def test_thinking_deltas_with_null_itemid_emit_immediately() -> None:
    formatter = AppServerEventFormatter()

    # Delta with null itemId - should emit immediately
    delta_msg = {
        "method": "item/reasoning/summaryTextDelta",
        "params": {
            "itemId": None,
            "turnId": "turn-7",
            "delta": "This has null itemId",
        },
    }

    lines = formatter.format_event(delta_msg)
    assert lines == ["thinking", "**This has null itemId**"]


@pytest.mark.anyio
async def test_thinking_part_added_without_itemid() -> None:
    formatter = AppServerEventFormatter()

    # summaryPartAdded without itemId should not crash
    part_added_msg = {
        "method": "item/reasoning/summaryPartAdded",
        "params": {"turnId": "turn-8"},
    }

    lines = formatter.format_event(part_added_msg)
    assert lines == []
