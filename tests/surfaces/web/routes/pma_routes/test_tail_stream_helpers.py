from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from codex_autorunner.surfaces.web.routes.pma_routes.tail_stream import (
    _managed_thread_harness,
    normalize_tail_level,
    parse_tail_duration_seconds,
    resolve_resume_after,
    since_ms_from_duration,
)


class TestParseTailDurationSeconds:
    def test_none_returns_none(self) -> None:
        assert parse_tail_duration_seconds(None) is None

    @pytest.mark.parametrize(
        "input_val,expected",
        [
            ("30s", 30),
            ("5m", 300),
            ("2h", 7200),
            ("1d", 86400),
            ("1w", 604800),
            ("1h30m", 5400),
            ("2d12h", 216000),
            ("1w2d", 777600),
        ],
    )
    def test_valid_durations(self, input_val: str, expected: int) -> None:
        assert parse_tail_duration_seconds(input_val) == expected

    def test_empty_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            parse_tail_duration_seconds("")
        assert exc_info.value.status_code == 400

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            parse_tail_duration_seconds("   ")
        assert exc_info.value.status_code == 400

    def test_missing_unit_suffix_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            parse_tail_duration_seconds("30")
        assert exc_info.value.status_code == 400

    def test_unknown_unit_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            parse_tail_duration_seconds("30x")
        assert exc_info.value.status_code == 400

    def test_zero_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            parse_tail_duration_seconds("0s")
        assert exc_info.value.status_code == 400
        assert "must be > 0" in exc_info.value.detail

    def test_case_insensitive(self) -> None:
        assert parse_tail_duration_seconds("5M") == 300
        assert parse_tail_duration_seconds("2H") == 7200

    def test_overflow_component_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            parse_tail_duration_seconds("12345678901s")
        assert exc_info.value.status_code == 400
        assert "too large" in exc_info.value.detail


class TestSinceMsFromDuration:
    def test_none_returns_none(self) -> None:
        assert since_ms_from_duration(None) is None

    def test_returns_past_timestamp(self) -> None:
        result = since_ms_from_duration("1h")
        assert result is not None
        import time

        now_ms = int(time.time() * 1000)
        assert now_ms - 4000 * 1000 < result < now_ms - 3000 * 1000


class TestNormalizeTailLevel:
    def test_none_defaults_to_info(self) -> None:
        assert normalize_tail_level(None) == "info"

    def test_empty_defaults_to_info(self) -> None:
        assert normalize_tail_level("") == "info"

    def test_info_accepted(self) -> None:
        assert normalize_tail_level("info") == "info"

    def test_debug_accepted(self) -> None:
        assert normalize_tail_level("debug") == "debug"

    def test_case_insensitive(self) -> None:
        assert normalize_tail_level("DEBUG") == "debug"
        assert normalize_tail_level("Info") == "info"

    def test_verbose_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            normalize_tail_level("verbose")
        assert exc_info.value.status_code == 400
        assert "level must be info or debug" in exc_info.value.detail


class TestResolveResumeAfter:
    def test_explicit_since_event_id(self) -> None:
        request = SimpleNamespace(headers={})
        assert resolve_resume_after(request, since_event_id=42) == 42

    def test_explicit_id_wins_over_header(self) -> None:
        request = SimpleNamespace(headers={"Last-Event-ID": "10"})
        assert resolve_resume_after(request, since_event_id=42) == 42

    def test_negative_since_event_id_rejected(self) -> None:
        request = SimpleNamespace(headers={})
        with pytest.raises(HTTPException) as exc_info:
            resolve_resume_after(request, since_event_id=-1)
        assert exc_info.value.status_code == 400
        assert "since_event_id must be >= 0" in exc_info.value.detail

    def test_header_fallback(self) -> None:
        request = SimpleNamespace(headers={"Last-Event-ID": "99"})
        assert resolve_resume_after(request, since_event_id=None) == 99

    def test_no_header_no_id_returns_none(self) -> None:
        request = SimpleNamespace(headers={})
        assert resolve_resume_after(request, since_event_id=None) is None

    def test_empty_header_returns_none(self) -> None:
        request = SimpleNamespace(headers={"Last-Event-ID": ""})
        assert resolve_resume_after(request, since_event_id=None) is None

    def test_non_integer_header_rejected(self) -> None:
        request = SimpleNamespace(headers={"Last-Event-ID": "abc"})
        with pytest.raises(HTTPException) as exc_info:
            resolve_resume_after(request, since_event_id=None)
        assert exc_info.value.status_code == 400
        assert "Invalid Last-Event-ID" in exc_info.value.detail

    def test_negative_header_rejected(self) -> None:
        request = SimpleNamespace(headers={"Last-Event-ID": "-5"})
        with pytest.raises(HTTPException) as exc_info:
            resolve_resume_after(request, since_event_id=None)
        assert exc_info.value.status_code == 400
        assert "Last-Event-ID must be >= 0" in exc_info.value.detail


class TestManagedThreadHarness:
    def test_returns_none_when_no_factory(self) -> None:
        service = SimpleNamespace()
        assert _managed_thread_harness(service, "codex") is None

    def test_returns_none_when_factory_raises(self) -> None:
        service = SimpleNamespace(
            harness_factory=lambda _aid: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        assert _managed_thread_harness(service, "codex") is None

    def test_returns_harness_from_factory(self) -> None:
        harness = SimpleNamespace(supports=lambda c: True)
        service = SimpleNamespace(harness_factory=lambda _aid: harness)
        assert _managed_thread_harness(service, "codex") is harness
