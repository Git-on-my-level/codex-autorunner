from codex_autorunner.core.orchestration.runtime_threads import (
    RUNTIME_THREAD_INTERRUPTED_ERROR,
    RUNTIME_THREAD_TIMEOUT_ERROR,
)
from codex_autorunner.core.redaction import redact_text
from codex_autorunner.integrations.chat.runtime_thread_errors import (
    format_public_error,
    sanitize_runtime_thread_error,
)


def test_redaction_scrubs_common_tokens() -> None:
    text = "sk-1234567890abcdefghijkl ghp_1234567890abcdefghijkl AKIA1234567890ABCDEF eyJhbGciOiJIUzI1NiJ9.eyJmb28iOiJiYXIifQ.abcDEF123_-"
    out = redact_text(text)
    assert "sk-1234567890" not in out
    assert "ghp_1234567890" not in out
    assert "AKIA1234567890" not in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "sk-[REDACTED]" in out
    assert "gh_[REDACTED]" in out
    assert "AKIA[REDACTED]" in out
    assert "[JWT_REDACTED]" in out


def test_redaction_with_multiple_occurrences() -> None:
    text = "Key1=sk-1234567890abcdefghijkl\nKey2=sk-abcdefghijklmnopqrstuv\nKey3=sk-1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = redact_text(text)
    assert "sk-1234567890abcdefghijkl" not in out
    assert "sk-abcdefghijklmnopqrstuv" not in out
    assert "sk-1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in out
    assert out.count("sk-[REDACTED]") == 3


def test_redaction_preserves_safe_text() -> None:
    text = "This is safe text with no secrets."
    out = redact_text(text)
    assert out == text


def test_redaction_handles_mixed_content() -> None:
    text = "export OPENAI_API_KEY=sk-1234567890abcdefghijkl\nexport SAFE_VAR=some_value"
    out = redact_text(text)
    assert "sk-1234567890" not in out
    assert "sk-[REDACTED]" in out
    assert "SAFE_VAR=some_value" in out


def test_redaction_with_github_tokens() -> None:
    text = "ghp_test1234567890abcdef gho_test9876543210fedcba"
    out = redact_text(text)
    assert "ghp_test1234567890" not in out
    assert "gho_test9876543210" not in out
    assert "gh_[REDACTED]" in out
    assert out.count("gh_[REDACTED]") == 2


def test_redaction_with_short_tokens() -> None:
    text = "sk-short ghp_too_short"
    out = redact_text(text)
    assert out == text


def test_format_public_error_redacts_tokens() -> None:
    detail = (
        "API key: sk-1234567890abcdefghijkl, GitHub token: ghp_test1234567890abcdef"
    )
    out = format_public_error(detail)
    assert "sk-1234567890" not in out
    assert "ghp_test1234567890" not in out
    assert "sk-[REDACTED]" in out
    assert "gh_[REDACTED]" in out


def test_format_public_error_truncates_long_messages() -> None:
    detail = "x" * 300
    out = format_public_error(detail, limit=200)
    assert len(out) == 200
    assert out.endswith("...")


def test_format_public_error_normalizes_whitespace() -> None:
    detail = "Error:  \n  Multiple   \t  spaces  \n\n  here"
    out = format_public_error(detail)
    assert "Multiple spaces here" in out
    assert "\n" not in out
    assert "\t" not in out


def test_format_public_error_with_jwt() -> None:
    detail = "JWT: eyJhbGciOiJIUzI1NiJ9.eyJmb28iOiJiYXIifQ.abcDEF123_-"
    out = format_public_error(detail)
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "[JWT_REDACTED]" in out


def test_format_public_error_with_aws_key() -> None:
    detail = "AWS key: AKIA1234567890ABCDEF"
    out = format_public_error(detail)
    assert "AKIA1234567890ABCDEF" not in out
    assert "AKIA[REDACTED]" in out


class TestSanitizeRuntimeThreadErrorPolicy:
    def test_preserves_sanitized_detail_for_actionable_errors(self) -> None:
        assert (
            sanitize_runtime_thread_error(
                "Auth required",
                public_error="Turn failed",
                timeout_error="Turn timed out",
                interrupted_error="Turn interrupted",
            )
            == "Auth required"
        )

    def test_preserves_sanitized_detail_with_secrets_redacted(self) -> None:
        result = sanitize_runtime_thread_error(
            "API key sk-1234567890abcdefghijkl rejected",
            public_error="Turn failed",
            timeout_error="Turn timed out",
            interrupted_error="Turn interrupted",
        )
        assert "sk-1234567890abcdefghijkl" not in result
        assert "sk-[REDACTED]" in result
        assert "rejected" in result

    def test_maps_runtime_timeout_to_surface_timeout(self) -> None:
        assert (
            sanitize_runtime_thread_error(
                RUNTIME_THREAD_TIMEOUT_ERROR,
                public_error="Turn failed",
                timeout_error="Surface timed out",
                interrupted_error="Turn interrupted",
            )
            == "Surface timed out"
        )

    def test_maps_surface_timeout_string_to_surface_timeout(self) -> None:
        assert (
            sanitize_runtime_thread_error(
                "Surface timed out",
                public_error="Turn failed",
                timeout_error="Surface timed out",
                interrupted_error="Turn interrupted",
            )
            == "Surface timed out"
        )

    def test_maps_runtime_interrupted_to_surface_interrupted(self) -> None:
        assert (
            sanitize_runtime_thread_error(
                RUNTIME_THREAD_INTERRUPTED_ERROR,
                public_error="Turn failed",
                timeout_error="Turn timed out",
                interrupted_error="Surface interrupted",
            )
            == "Surface interrupted"
        )

    def test_maps_surface_interrupted_string_to_surface_interrupted(self) -> None:
        assert (
            sanitize_runtime_thread_error(
                "Surface interrupted",
                public_error="Turn failed",
                timeout_error="Turn timed out",
                interrupted_error="Surface interrupted",
            )
            == "Surface interrupted"
        )

    def test_returns_public_error_for_empty_detail(self) -> None:
        assert (
            sanitize_runtime_thread_error(
                "",
                public_error="Turn failed",
                timeout_error="Turn timed out",
                interrupted_error="Turn interrupted",
            )
            == "Turn failed"
        )

    def test_returns_public_error_for_none_detail(self) -> None:
        assert (
            sanitize_runtime_thread_error(
                None,
                public_error="Turn failed",
                timeout_error="Turn timed out",
                interrupted_error="Turn interrupted",
            )
            == "Turn failed"
        )

    def test_returns_public_error_for_generic_runtime_failure(self) -> None:
        assert (
            sanitize_runtime_thread_error(
                "Runtime thread failed",
                public_error="Turn failed",
                timeout_error="Turn timed out",
                interrupted_error="Turn interrupted",
            )
            == "Turn failed"
        )

    def test_returns_public_error_when_detail_matches_public_error(self) -> None:
        assert (
            sanitize_runtime_thread_error(
                "Turn failed",
                public_error="Turn failed",
                timeout_error="Turn timed out",
                interrupted_error="Turn interrupted",
            )
            == "Turn failed"
        )

    def test_truncates_long_detail(self) -> None:
        long_detail = "x" * 500
        result = sanitize_runtime_thread_error(
            long_detail,
            public_error="Turn failed",
            timeout_error="Turn timed out",
            interrupted_error="Turn interrupted",
            detail_limit=100,
        )
        assert len(result) == 100
        assert result.endswith("...")

    def test_normalizes_whitespace_in_detail(self) -> None:
        result = sanitize_runtime_thread_error(
            "Error:  \n  Multiple   \t  spaces  \n\n  here",
            public_error="Turn failed",
            timeout_error="Turn timed out",
            interrupted_error="Turn interrupted",
        )
        assert "\n" not in result
        assert "\t" not in result
        assert "Multiple spaces here" in result


class TestSanitizeRuntimeThreadErrorConsistency:
    def test_telegram_and_discord_use_same_policy_for_detail(self) -> None:
        detail = "backend exploded with private detail"
        telegram_result = sanitize_runtime_thread_error(
            detail,
            public_error="Telegram PMA turn failed",
            timeout_error="Telegram PMA turn timed out",
            interrupted_error="Telegram PMA turn interrupted",
        )
        discord_result = sanitize_runtime_thread_error(
            detail,
            public_error="Discord PMA turn failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
        )
        assert telegram_result == detail
        assert discord_result == detail

    def test_telegram_and_discord_both_map_timeout(self) -> None:
        telegram_result = sanitize_runtime_thread_error(
            RUNTIME_THREAD_TIMEOUT_ERROR,
            public_error="Telegram PMA turn failed",
            timeout_error="Telegram PMA turn timed out",
            interrupted_error="Telegram PMA turn interrupted",
        )
        discord_result = sanitize_runtime_thread_error(
            RUNTIME_THREAD_TIMEOUT_ERROR,
            public_error="Discord PMA turn failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
        )
        assert telegram_result == "Telegram PMA turn timed out"
        assert discord_result == "Discord PMA turn timed out"

    def test_telegram_and_discord_both_map_interrupted(self) -> None:
        telegram_result = sanitize_runtime_thread_error(
            RUNTIME_THREAD_INTERRUPTED_ERROR,
            public_error="Telegram PMA turn failed",
            timeout_error="Telegram PMA turn timed out",
            interrupted_error="Telegram PMA turn interrupted",
        )
        discord_result = sanitize_runtime_thread_error(
            RUNTIME_THREAD_INTERRUPTED_ERROR,
            public_error="Discord PMA turn failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
        )
        assert telegram_result == "Telegram PMA turn interrupted"
        assert discord_result == "Discord PMA turn interrupted"

    def test_telegram_and_discord_both_redact_secrets(self) -> None:
        detail = "API key sk-1234567890abcdefghijkl rejected"
        telegram_result = sanitize_runtime_thread_error(
            detail,
            public_error="Telegram PMA turn failed",
            timeout_error="Telegram PMA turn timed out",
            interrupted_error="Telegram PMA turn interrupted",
        )
        discord_result = sanitize_runtime_thread_error(
            detail,
            public_error="Discord PMA turn failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
        )
        assert "sk-1234567890abcdefghijkl" not in telegram_result
        assert "sk-[REDACTED]" in telegram_result
        assert "sk-1234567890abcdefghijkl" not in discord_result
        assert "sk-[REDACTED]" in discord_result
