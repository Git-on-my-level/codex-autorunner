from codex_autorunner.integrations.github.chatops import _extract_issue_number


def test_extract_issue_number_from_full_url():
    assert _extract_issue_number("https://github.com/owner/repo/issues/123") == 123


def test_extract_issue_number_from_api_url():
    assert (
        _extract_issue_number("https://api.github.com/repos/owner/repo/issues/456")
        == 456
    )


def test_extract_issue_number_from_url_with_query():
    assert (
        _extract_issue_number("https://github.com/owner/repo/issues/789?foo=bar") == 789
    )


def test_extract_issue_number_from_short_url():
    assert _extract_issue_number("/issues/42") == 42


def test_extract_issue_number_none_for_invalid_url():
    assert _extract_issue_number("https://github.com/owner/repo/pull/123") is None


def test_extract_issue_number_none_for_empty_string():
    assert _extract_issue_number("") is None


def test_extract_issue_number_none_for_none():
    assert _extract_issue_number(None) is None


def test_extract_issue_number_none_for_url_without_number():
    assert _extract_issue_number("https://github.com/owner/repo/issues/abc") is None
