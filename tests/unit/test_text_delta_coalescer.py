from __future__ import annotations

from codex_autorunner.core.text_delta_coalescer import (
    StreamingTextCoalescer,
    TextDeltaCoalescer,
)


class TestTextDeltaCoalescer:
    def test_initial_buffer_is_empty(self):
        coalescer = TextDeltaCoalescer()
        assert coalescer.get_buffer() == ""

    def test_add_appends_to_buffer(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("hello")
        assert coalescer.get_buffer() == "hello"

    def test_add_multiple_deltas(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("hello")
        coalescer.add(" ")
        coalescer.add("world")
        assert coalescer.get_buffer() == "hello world"

    def test_add_ignores_none(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("hello")
        coalescer.add(None)
        assert coalescer.get_buffer() == "hello"

    def test_add_ignores_empty_string(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("hello")
        coalescer.add("")
        assert coalescer.get_buffer() == "hello"

    def test_add_ignores_non_string_types(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("hello")
        coalescer.add(123)
        coalescer.add(["list"])
        coalescer.add({"dict": "value"})
        assert coalescer.get_buffer() == "hello"

    def test_flush_lines_returns_empty_list_when_no_newlines(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("hello world")
        assert coalescer.flush_lines() == []
        assert coalescer.get_buffer() == "hello world"

    def test_flush_lines_returns_empty_list_when_buffer_empty(self):
        coalescer = TextDeltaCoalescer()
        assert coalescer.flush_lines() == []

    def test_flush_lines_extracts_single_line(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("line1\nline2")
        lines = coalescer.flush_lines()
        assert lines == ["line1"]
        assert coalescer.get_buffer() == "line2"

    def test_flush_lines_extracts_multiple_lines(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("line1\nline2\nline3\npartial")
        lines = coalescer.flush_lines()
        assert lines == ["line1", "line2", "line3"]
        assert coalescer.get_buffer() == "partial"

    def test_flush_lines_handles_trailing_newline(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("line1\nline2\n")
        lines = coalescer.flush_lines()
        assert lines == ["line1", "line2"]
        assert coalescer.get_buffer() == ""

    def test_flush_lines_handles_consecutive_newlines(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("line1\n\nline3\n")
        lines = coalescer.flush_lines()
        assert lines == ["line1", "", "line3"]
        assert coalescer.get_buffer() == ""

    def test_flush_all_returns_empty_list_when_buffer_empty(self):
        coalescer = TextDeltaCoalescer()
        assert coalescer.flush_all() == []

    def test_flush_all_returns_single_line_without_newline(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("hello world")
        lines = coalescer.flush_all()
        assert lines == ["hello world"]
        assert coalescer.get_buffer() == ""

    def test_flush_all_returns_multiple_lines(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("line1\nline2\nline3")
        lines = coalescer.flush_all()
        assert lines == ["line1", "line2", "line3"]
        assert coalescer.get_buffer() == ""

    def test_flush_all_clears_buffer(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("some content")
        coalescer.flush_all()
        assert coalescer.get_buffer() == ""

    def test_clear_empties_buffer(self):
        coalescer = TextDeltaCoalescer()
        coalescer.add("hello world")
        coalescer.clear()
        assert coalescer.get_buffer() == ""

    def test_clear_on_empty_buffer_is_safe(self):
        coalescer = TextDeltaCoalescer()
        coalescer.clear()
        assert coalescer.get_buffer() == ""

    def test_flush_on_newline_parameter_accepted(self):
        coalescer = TextDeltaCoalescer(flush_on_newline=True)
        coalescer.add("test")
        assert coalescer.get_buffer() == "test"


class TestStreamingTextCoalescer:
    def test_initial_buffer_is_empty(self):
        coalescer = StreamingTextCoalescer()
        chunks = coalescer.flush()
        assert chunks == []

    def test_default_min_flush_chars(self):
        coalescer = StreamingTextCoalescer()
        assert coalescer._min_flush_chars == 32

    def test_default_max_buffer_chars(self):
        coalescer = StreamingTextCoalescer()
        assert coalescer._max_buffer_chars == 2048

    def test_custom_min_flush_chars(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=64)
        assert coalescer._min_flush_chars == 64

    def test_custom_max_buffer_chars(self):
        coalescer = StreamingTextCoalescer(max_buffer_chars=4096)
        assert coalescer._max_buffer_chars == 4096

    def test_min_flush_chars_minimum_is_one(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=0)
        assert coalescer._min_flush_chars == 1

    def test_min_flush_chars_negative_becomes_one(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=-10)
        assert coalescer._min_flush_chars == 1

    def test_max_buffer_chars_respects_min(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=100, max_buffer_chars=50)
        assert coalescer._max_buffer_chars == 100

    def test_add_returns_empty_list_for_none(self):
        coalescer = StreamingTextCoalescer()
        assert coalescer.add(None) == []

    def test_add_returns_empty_list_for_empty_string(self):
        coalescer = StreamingTextCoalescer()
        assert coalescer.add("") == []

    def test_add_returns_empty_list_for_non_string(self):
        coalescer = StreamingTextCoalescer()
        assert coalescer.add(123) == []
        assert coalescer.add(["list"]) == []

    def test_add_returns_empty_list_when_below_threshold(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=10)
        chunks = coalescer.add("short")
        assert chunks == []

    def test_add_flushes_on_newline_immediately(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=100)
        chunks = coalescer.add("hello\n")
        assert chunks == ["hello\n"]
        assert coalescer.flush() == []

    def test_add_flushes_multiple_newlines(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=100)
        chunks = coalescer.add("line1\nline2\nremaining")
        assert chunks == ["line1\n", "line2\n"]
        assert coalescer.flush() == ["remaining"]

    def test_add_flushes_at_boundary_space(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("hello world ")
        assert chunks == ["hello world "]
        assert coalescer.flush() == []

    def test_add_flushes_at_boundary_period(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("hello.")
        assert chunks == ["hello."]
        assert coalescer.flush() == []

    def test_add_flushes_at_boundary_exclamation(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("hello!")
        assert chunks == ["hello!"]

    def test_add_flushes_at_boundary_question(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("hello?")
        assert chunks == ["hello?"]

    def test_add_flushes_at_boundary_semicolon(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("hello;")
        assert chunks == ["hello;"]

    def test_add_flushes_at_boundary_colon(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("hello:")
        assert chunks == ["hello:"]

    def test_add_does_not_flush_below_min_chars_at_boundary(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=10)
        chunks = coalescer.add("hi.")
        assert chunks == []
        assert coalescer.flush() == ["hi."]

    def test_add_flushes_when_oversized(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5, max_buffer_chars=10)
        chunks = coalescer.add("this is a very long string")
        assert len(chunks) >= 2
        for chunk in chunks[:-1]:
            assert len(chunk) == 10

    def test_add_flushes_multiple_chunks_when_oversized(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5, max_buffer_chars=10)
        chunks = coalescer.add("a" * 35)
        assert len(chunks) == 3
        assert chunks[0] == "a" * 10
        assert chunks[1] == "a" * 10
        assert chunks[2] == "a" * 10
        assert coalescer.flush() == ["a" * 5]

    def test_flush_returns_remaining_buffer(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=100)
        coalescer.add("some buffered text")
        chunks = coalescer.flush()
        assert chunks == ["some buffered text"]

    def test_flush_clears_buffer(self):
        coalescer = StreamingTextCoalescer()
        coalescer.add("text")
        coalescer.flush()
        assert coalescer.flush() == []

    def test_flush_returns_empty_list_when_buffer_empty(self):
        coalescer = StreamingTextCoalescer()
        assert coalescer.flush() == []

    def test_combined_newline_and_boundary(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("hi\nhello. world ")
        assert chunks == ["hi\n", "hello. world "]

    def test_newline_takes_precedence_over_oversized(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5, max_buffer_chars=10)
        chunks = coalescer.add("aaaa\n")
        assert chunks == ["aaaa\n"]

    def test_boundary_chars_preserved(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=3)
        chunks = coalescer.add("Hi. How are you? ")
        assert chunks == ["Hi. How are you? "]

    def test_incremental_adding_accumulates_buffer(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=20)
        coalescer.add("Hello")
        coalescer.add(" ")
        coalescer.add("World")
        assert coalescer.flush() == ["Hello World"]

    def test_incremental_adding_flushes_when_boundary_reached(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=10)
        all_chunks = []
        all_chunks.extend(coalescer.add("Hello "))
        all_chunks.extend(coalescer.add("World!"))
        assert all_chunks == ["Hello World!"]

    def test_empty_boundary_chars(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("hello ")
        assert chunks == ["hello "]

    def test_tab_as_boundary(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("hello\t")
        assert chunks == ["hello\t"]

    def test_newline_as_boundary(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("hello\n")
        assert chunks == ["hello\n"]

    def test_exact_min_flush_chars_at_boundary(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("12345 ")
        assert chunks == ["12345 "]

    def test_one_below_min_flush_chars_at_boundary(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("123 ")
        assert chunks == []
        assert coalescer.flush() == ["123 "]

    def test_float_min_flush_chars_converted_to_int(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5.9)
        assert coalescer._min_flush_chars == 5

    def test_float_max_buffer_chars_converted_to_int(self):
        coalescer = StreamingTextCoalescer(max_buffer_chars=100.5)
        assert coalescer._max_buffer_chars == 100

    def test_exact_max_buffer_chars_does_not_flush(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5, max_buffer_chars=10)
        chunks = coalescer.add("a" * 10)
        assert chunks == []

    def test_over_max_buffer_chars_flushes(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5, max_buffer_chars=10)
        chunks = coalescer.add("a" * 11)
        assert len(chunks) == 1
        assert len(chunks[0]) == 10

    def test_unicode_handling(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("hello 世界! ")
        assert chunks == ["hello 世界! "]

    def test_multibyte_unicode_newline(self):
        coalescer = StreamingTextCoalescer(min_flush_chars=5)
        chunks = coalescer.add("hello\n世界")
        assert chunks == ["hello\n"]
