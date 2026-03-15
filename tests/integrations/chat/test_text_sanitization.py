from codex_autorunner.integrations.chat.text_sanitization import (
    collapse_local_markdown_links,
)


def test_collapse_local_markdown_links_preserves_site_relative_routes() -> None:
    text = "Download [artifact](/car/hub/filebox/foo.zip) from the hub."

    result = collapse_local_markdown_links(text)

    assert result == text


def test_collapse_local_markdown_links_preserves_inline_code_examples() -> None:
    text = "Use `[file](/Users/dazheng/worktree/src/app.py)` literally."

    result = collapse_local_markdown_links(text)

    assert result == text


def test_collapse_local_markdown_links_preserves_fenced_code_examples() -> None:
    text = "```md\n" "[file](/Users/dazheng/worktree/src/app.py)\n" "```\n"

    result = collapse_local_markdown_links(text)

    assert result == text


def test_collapse_local_markdown_links_hides_workspace_paths() -> None:
    text = "Open [repo](/workspace/codex-autorunner/README.md) next."

    result = collapse_local_markdown_links(text)

    assert result == "Open repo next."


def test_collapse_local_markdown_links_handles_parentheses_in_paths() -> None:
    text = "Check [f](/Users/me/My Folder (old)/file.py) for details."

    result = collapse_local_markdown_links(text)

    assert result == "Check f for details."
