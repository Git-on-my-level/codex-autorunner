from codex_autorunner.core.docs import parse_todos


def test_parse_todos_collects_outstanding_and_done() -> None:
    content = """# TODO

- [ ] First task
- [x] Finished task
- [X] Also finished
- [ ]    Spaced task
"""
    outstanding, done = parse_todos(content)

    assert outstanding == ["First task", "Spaced task"]
    assert done == ["Finished task", "Also finished"]


def test_parse_todos_ignores_code_fences() -> None:
    content = """# TODO

- [ ] Real task 1
- [x] Completed task

```python
# This is a code block with example TODOs
- [ ] This should be ignored
- [x] This should also be ignored
```

- [ ] Real task 2
"""
    outstanding, done = parse_todos(content)

    assert outstanding == ["Real task 1", "Real task 2"]
    assert done == ["Completed task"]


def test_parse_todos_ignores_html_comments() -> None:
    content = """# TODO

- [ ] Real task 1

<!--
- [ ] This TODO in HTML comment should be ignored
- [x] This too
-->

- [ ] Real task 2
"""
    outstanding, done = parse_todos(content)

    assert outstanding == ["Real task 1", "Real task 2"]
    assert done == []


def test_parse_todos_handles_multiline_html_comment() -> None:
    content = """# TODO

- [ ] Real task

<!--
This is a multiline HTML comment.
- [ ] This should be ignored
- [x] This too should be ignored
Even this line is part of the comment
-->

- [ ] Another real task
"""
    outstanding, done = parse_todos(content)

    assert outstanding == ["Real task", "Another real task"]
    assert done == []


def test_parse_todos_supports_asterisk_bullets() -> None:
    content = """# TODO

* [ ] Task with asterisk
* [x] Completed with asterisk
- [ ] Task with dash
"""
    outstanding, done = parse_todos(content)

    assert outstanding == ["Task with asterisk", "Task with dash"]
    assert done == ["Completed with asterisk"]


def test_parse_todos_handles_empty_checkboxes() -> None:
    content = """# TODO

- [ ]
- [x]
- [ ] Real task
- [x] Completed task
"""
    outstanding, done = parse_todos(content)

    assert outstanding == ["", "Real task"]
    assert done == ["", "Completed task"]


def test_parse_todos_handles_inline_html_comments() -> None:
    content = """# TODO

- [ ] Real task with inline comment <!-- note to self
- [x] Another completed task <!-- internal note
- [ ] Task without inline comment
"""
    outstanding, done = parse_todos(content)

    assert outstanding == [
        "Real task with inline comment <!-- note to self",
        "Task without inline comment",
    ]
    assert done == ["Another completed task <!-- internal note"]


def test_parse_todos_complex_markdown() -> None:
    content = """# TODO

- [ ] Implement feature A
- [x] Fix bug B

## Code Examples

```python
# Example usage:
# - [ ] This is just a comment, not a real TODO
def example():
    pass
```

<!--
Internal notes:
- [ ] This is a comment TODO
- [x] This is also a comment
-->

## Remaining Work

* [ ] Implement feature C
* [ ] Refactor D
"""
    outstanding, done = parse_todos(content)

    assert outstanding == ["Implement feature A", "Implement feature C", "Refactor D"]
    assert done == ["Fix bug B"]
