from codex_autorunner.integrations.github.service import (
    find_github_links,
    parse_github_url,
)


def test_parse_github_issue_url_with_query_string() -> None:
    parsed = parse_github_url(
        "https://github.com/Git-on-my-level/codex-autorunner/issues/577?notification_referrer_id=NT_kwDO"
    )
    assert parsed == ("Git-on-my-level/codex-autorunner", "issue", 577)


def test_parse_github_pr_url_with_www_host_and_query_string() -> None:
    parsed = parse_github_url(
        "https://www.github.com/Git-on-my-level/codex-autorunner/pull/123?expand=1"
    )
    assert parsed == ("Git-on-my-level/codex-autorunner", "pr", 123)


def test_find_github_links_matches_issue_urls_with_query_strings() -> None:
    text = "Context: https://github.com/Git-on-my-level/codex-autorunner/issues/577?notification_referrer_id=NT_kwDO"
    links = find_github_links(text)
    assert links == [
        "https://github.com/Git-on-my-level/codex-autorunner/issues/577?notification_referrer_id=NT_kwDO"
    ]
