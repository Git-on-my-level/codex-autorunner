from codex_autorunner.core.redaction import redact_text
from codex_autorunner.integrations.telegram.helpers import format_public_error


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
