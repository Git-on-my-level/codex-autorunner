from codex_autorunner.integrations.github.pr_flow import (
    PrFlowReviewSummary,
    _normalize_review_snippet,
)


def test_normalize_review_snippet_single_line() -> None:
    text = "This is a simple review comment"
    result = _normalize_review_snippet(text, 100)
    assert result == "This is a simple review comment"


def test_normalize_review_snippet_multi_line() -> None:
    text = "This is a multi-line\nreview comment\nthat should be normalized"
    result = _normalize_review_snippet(text, 100)
    assert result == "This is a multi-line review comment that should be normalized"


def test_normalize_review_snippet_with_leading_dash_bullet() -> None:
    text = "- This is a bullet point that should be removed"
    result = _normalize_review_snippet(text, 100)
    assert result == "This is a bullet point that should be removed"


def test_normalize_review_snippet_with_leading_asterisk_bullet() -> None:
    text = "* This is an asterisk bullet point"
    result = _normalize_review_snippet(text, 100)
    assert result == "This is an asterisk bullet point"


def test_normalize_review_snippet_with_leading_dot_bullet() -> None:
    text = "• This is a dot bullet point"
    result = _normalize_review_snippet(text, 100)
    assert result == "This is a dot bullet point"


def test_normalize_review_snippet_with_double_space_bullet() -> None:
    text = "-  This has double space after dash"
    result = _normalize_review_snippet(text, 100)
    assert result == "This has double space after dash"


def test_normalize_review_snippet_multi_line_with_bullets() -> None:
    text = """- First point
- Second point
- Third point"""
    result = _normalize_review_snippet(text, 100)
    assert result == "First point - Second point - Third point"


def test_normalize_review_snippet_truncates_long_text() -> None:
    text = (
        "This is a very long comment that should be truncated when it exceeds the limit"
    )
    result = _normalize_review_snippet(text, 30)
    assert result == "This is a very long comment..."
    assert len(result) == 30


def test_normalize_review_snippet_empty_text() -> None:
    result = _normalize_review_snippet("", 100)
    assert result == ""


def test_normalize_review_snippet_none_value() -> None:
    result = _normalize_review_snippet(None, 100)
    assert result == ""


def test_normalize_review_snippet_strips_whitespace() -> None:
    text = "  This has surrounding whitespace  "
    result = _normalize_review_snippet(text, 100)
    assert result == "This has surrounding whitespace"


def test_normalize_review_snippet_mixed_whitespace() -> None:
    text = "This\t has\n mixed  whitespace"
    result = _normalize_review_snippet(text, 100)
    assert result == "This has mixed whitespace"


def test_multi_line_review_comment_produces_valid_todo(tmp_path, monkeypatch) -> None:

    worktree_root = tmp_path / "worktree"
    worktree_root.mkdir()
    doc_path = worktree_root / ".codex-autorunner"
    doc_path.mkdir()

    todo_path = doc_path / "TODO.md"
    todo_path.write_text("# TODO\n\n- [ ] Existing task\n", encoding="utf-8")

    review_data = {
        "threads": [
            {
                "isResolved": False,
                "comments": [
                    {
                        "body": "This is a multi-line review comment\nwith multiple lines\n- bullet point 1\n- bullet point 2\nthat should be normalized",
                        "author": {"login": "reviewer1"},
                        "path": "src/main.py",
                        "line": 42,
                    }
                ],
            },
            {
                "isResolved": False,
                "comments": [
                    {
                        "body": "* Another comment\n  with asterisk bullet",
                        "author": {"login": "reviewer2"},
                        "path": "src/utils.py",
                        "line": 10,
                    }
                ],
            },
        ],
        "checks": [],
        "codex_review": None,
    }

    summary = PrFlowReviewSummary(total=2, major=2, minor=0, resolved=0)
    bundle_path = "/path/to/review_bundle.md"

    from codex_autorunner.integrations.github.pr_flow import PrFlowManager

    manager = PrFlowManager.__new__(PrFlowManager)
    manager._config = {"review": {"severity_threshold": "minor"}}
    manager.repo_root = tmp_path

    state = {
        "cycle": 1,
        "worktree_path": "",
    }

    class MockEngine:
        def __init__(self, root):
            self.root = root
            self.config = type("Config", (), {"raw": {}})()
            self.config.doc_path = lambda key: todo_path
            self.config.raw = {}

    def mock_load_engine(path):
        return MockEngine(path)

    def mock_require_worktree_root(s):
        return worktree_root

    def mock_log_line(msg):
        pass

    monkeypatch.setattr(manager, "_load_engine", mock_load_engine)
    monkeypatch.setattr(manager, "_require_worktree_root", mock_require_worktree_root)
    monkeypatch.setattr(manager, "_log_line", mock_log_line)

    manager._apply_review_to_todo(state, bundle_path, summary, review_data)

    updated_content = todo_path.read_text(encoding="utf-8")

    assert (
        "- [ ] Address review: src/main.py:42 This is a multi-line review comment with multiple lines - bullet point 1 - bullet point 2 that sh... (reviewer1)"
        in updated_content
    )
    assert (
        "- [ ] Address review: src/utils.py:10 Another comment with asterisk bullet (reviewer2)"
        in updated_content
    )
    assert "## Review Feedback Cycle 1" in updated_content

    import re

    lines = updated_content.splitlines()
    review_task_lines = [
        line for line in lines if line.strip().startswith("- [ ] Address review:")
    ]
    for line in review_task_lines:
        after_colon = line.split("Address review:")[-1]
        assert not re.match(
            r"^\s*[-*]\s+\S", after_colon.strip()
        ), f"Line contains non-checkbox bullet pattern: {line}"

    checkbox_lines = [line for line in lines if line.strip().startswith("- [ ]")]
    for line in checkbox_lines:
        if "Address review:" in line:
            snippet = line.split("Address review:")[-1]
            assert not snippet.strip().startswith(
                "- "
            ), f"Snippet contains leading bullet: {line}"
            assert not snippet.strip().startswith(
                "* "
            ), f"Snippet contains leading bullet: {line}"
            assert "\n" not in snippet, f"Snippet contains newline: {line}"
