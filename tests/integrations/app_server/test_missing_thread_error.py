"""Tests for the shared missing-thread error classifier."""

from codex_autorunner.integrations.app_server.client import (
    CodexAppServerResponseError,
    is_missing_thread_error,
)


class TestIsMissingThreadError:
    def test_returns_false_for_non_app_server_error(self) -> None:
        assert is_missing_thread_error(ValueError("thread not found")) is False

    def test_returns_false_for_non_missing_app_server_error(self) -> None:
        exc = CodexAppServerResponseError(
            method="thread/resume",
            code=-32000,
            message="permission denied",
        )
        assert is_missing_thread_error(exc) is False

    def test_recognizes_thread_not_found_exact_case(self) -> None:
        exc = CodexAppServerResponseError(
            method="thread/resume",
            code=-32602,
            message="Thread not found",
        )
        assert is_missing_thread_error(exc) is True

    def test_recognizes_thread_not_found_lowercase(self) -> None:
        exc = CodexAppServerResponseError(
            method="thread/resume",
            code=-32602,
            message="thread not found",
        )
        assert is_missing_thread_error(exc) is True

    def test_recognizes_thread_not_found_mixed_case(self) -> None:
        exc = CodexAppServerResponseError(
            method="thread/resume",
            code=-32602,
            message="THREAD Not Found",
        )
        assert is_missing_thread_error(exc) is True

    def test_recognizes_thread_not_found_embedded(self) -> None:
        exc = CodexAppServerResponseError(
            method="thread/resume",
            code=-32602,
            message="Error: Thread not found in backend",
        )
        assert is_missing_thread_error(exc) is True

    def test_recognizes_no_rollout_found_exact_case(self) -> None:
        exc = CodexAppServerResponseError(
            method="thread/resume",
            code=-32600,
            message="no rollout found for thread id thread-123",
        )
        assert is_missing_thread_error(exc) is True

    def test_recognizes_no_rollout_found_mixed_case(self) -> None:
        exc = CodexAppServerResponseError(
            method="thread/resume",
            code=-32600,
            message="No rollout found for thread ID thread-456",
        )
        assert is_missing_thread_error(exc) is True

    def test_recognizes_no_rollout_found_embedded(self) -> None:
        exc = CodexAppServerResponseError(
            method="thread/resume",
            code=-32600,
            message="Failed: no rollout found for thread id abc-123 in storage",
        )
        assert is_missing_thread_error(exc) is True

    def test_does_not_match_similar_but_different_messages(self) -> None:
        exc = CodexAppServerResponseError(
            method="thread/resume",
            code=-32000,
            message="Thread not ready",
        )
        assert is_missing_thread_error(exc) is False

    def test_does_not_match_similar_but_different_rollout_messages(self) -> None:
        exc = CodexAppServerResponseError(
            method="thread/resume",
            code=-32000,
            message="Rollout found for thread",
        )
        assert is_missing_thread_error(exc) is False
