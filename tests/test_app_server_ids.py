from codex_autorunner.core.app_server_ids import (
    extract_thread_id,
    extract_thread_id_for_turn,
    extract_turn_id,
)


def test_extract_turn_id_from_top_level_keys():
    assert extract_turn_id({"turnId": "turn-123"}) == "turn-123"
    assert extract_turn_id({"turn_id": "turn-456"}) == "turn-456"
    assert extract_turn_id({"id": "turn-789"}) == "turn-789"
    assert extract_turn_id({"turnId": "turn-1", "turn_id": "turn-2"}) == "turn-1"


def test_extract_turn_id_from_nested_turn():
    assert extract_turn_id({"turn": {"id": "turn-123"}}) == "turn-123"
    assert extract_turn_id({"turn": {"turnId": "turn-456"}}) == "turn-456"
    assert extract_turn_id({"turn": {"turn_id": "turn-789"}}) == "turn-789"


def test_extract_turn_id_from_nested_item():
    assert extract_turn_id({"item": {"turnId": "turn-123"}}) == "turn-123"
    assert extract_turn_id({"item": {"turn_id": "turn-456"}}) == "turn-456"
    assert extract_turn_id({"item": {"id": "turn-789"}}) == "turn-789"


def test_extract_turn_id_from_precedence():
    top_level = {"turnId": "top", "turn": {"id": "nested"}, "item": {"id": "item"}}
    assert extract_turn_id(top_level) == "top"

    turn_only = {"turn": {"id": "turn-123"}, "item": {"id": "item-456"}}
    assert extract_turn_id(turn_only) == "turn-123"


def test_extract_turn_id_no_match():
    assert extract_turn_id({}) is None
    assert extract_turn_id({"foo": "bar"}) is None
    assert extract_turn_id({"turn": {"foo": "bar"}}) is None
    assert extract_turn_id({"item": {"foo": "bar"}}) is None
    assert extract_turn_id(None) is None
    assert extract_turn_id("not a dict") is None
    assert extract_turn_id([1, 2, 3]) is None


def test_extract_turn_id_non_string_values():
    assert extract_turn_id({"turnId": 123}) is None
    assert extract_turn_id({"turn_id": None}) is None
    assert extract_turn_id({"id": []}) is None
    assert extract_turn_id({"turn": {"id": 456}}) is None


def test_extract_thread_id_from_top_level_keys():
    assert extract_thread_id({"threadId": "thread-123"}) == "thread-123"
    assert extract_thread_id({"thread_id": "thread-456"}) == "thread-456"
    assert extract_thread_id({"id": "thread-789"}) == "thread-789"


def test_extract_thread_id_from_nested_thread():
    assert extract_thread_id({"thread": {"id": "thread-123"}}) == "thread-123"
    assert extract_thread_id({"thread": {"threadId": "thread-456"}}) == "thread-456"
    assert extract_thread_id({"thread": {"thread_id": "thread-789"}}) == "thread-789"


def test_extract_thread_id_no_match():
    assert extract_thread_id({}) is None
    assert extract_thread_id({"foo": "bar"}) is None
    assert extract_thread_id({"thread": {"foo": "bar"}}) is None
    assert extract_thread_id(None) is None
    assert extract_thread_id("not a dict") is None


def test_extract_thread_id_for_turn():
    assert extract_thread_id_for_turn({"threadId": "thread-1"}) == "thread-1"
    assert extract_thread_id_for_turn({"turn": {"threadId": "thread-2"}}) == "thread-2"
    assert extract_thread_id_for_turn({"item": {"threadId": "thread-3"}}) == "thread-3"
    assert (
        extract_thread_id_for_turn(
            {"threadId": "thread-1", "turn": {"threadId": "thread-2"}}
        )
        == "thread-1"
    )
    assert extract_thread_id_for_turn({}) is None
    assert extract_thread_id_for_turn({"turn": {"foo": "bar"}}) is None
