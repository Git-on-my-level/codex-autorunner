from codex_autorunner.core.redaction import get_redaction_patterns, redact_text


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


def test_get_redaction_patterns() -> None:
    patterns = get_redaction_patterns()
    assert isinstance(patterns, list)
    assert len(patterns) > 0
    for pattern in patterns:
        assert isinstance(pattern, str)


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
