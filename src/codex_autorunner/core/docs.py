import re
from pathlib import Path
from typing import List, Tuple

from .config import Config

_TODO_LINE_RE = re.compile(r"^\s*[-*]\s*\[(?P<state>[ xX])\]\s*(?P<text>.*)$")


def _iter_meaningful_lines(content: str):
    in_code_fence = False
    in_html_comment = False
    html_comment_pattern = re.compile(r"<!--.*?-->", re.DOTALL)

    for line in content.splitlines():
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            continue

        if in_code_fence:
            continue

        if line.lstrip().startswith("<!--"):
            if "-->" in line:
                if html_comment_pattern.search(line):
                    continue
            else:
                in_html_comment = True
                continue

        if in_html_comment:
            if "-->" in line:
                in_html_comment = False
            continue

        yield line


def parse_todos(content: str) -> Tuple[List[str], List[str]]:
    outstanding: List[str] = []
    done: List[str] = []
    if not content:
        return outstanding, done

    for line in _iter_meaningful_lines(content):
        match = _TODO_LINE_RE.match(line)
        if match:
            state = match.group("state")
            text = match.group("text").strip()
            if state in (" ",):
                outstanding.append(text)
            elif state in ("x", "X"):
                done.append(text)
    return outstanding, done


_TODO_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[(?P<state>[ xX])\]\s+\S")
_TODO_BULLET_RE = re.compile(r"^\s*[-*]\s+")


def validate_todo_markdown(content: str) -> List[str]:
    """
    Validate that TODO content contains tasks as markdown checkboxes.

    Rules:
    - If the file has any non-heading, non-empty content, it must include at least one checkbox line.
    - Any bullet line must be a checkbox bullet (no plain '-' bullets for tasks).
    """
    errors: List[str] = []
    if content is None:
        return ["TODO is missing"]
    lines = list(_iter_meaningful_lines(content))
    meaningful = [
        line for line in lines if line.strip() and not line.lstrip().startswith("#")
    ]
    if not meaningful:
        return []
    checkbox_lines = [line for line in meaningful if _TODO_CHECKBOX_RE.match(line)]
    if not checkbox_lines:
        errors.append(
            "TODO must contain at least one markdown checkbox task line like `- [ ] ...`."
        )
    bullet_lines = [line for line in meaningful if _TODO_BULLET_RE.match(line)]
    non_checkbox_bullets = [
        line for line in bullet_lines if not _TODO_CHECKBOX_RE.match(line)
    ]
    if non_checkbox_bullets:
        sample = non_checkbox_bullets[0].strip()
        errors.append(
            "TODO contains non-checkbox bullet(s); use `- [ ] ...` instead. "
            f"Example: `{sample}`"
        )
    return errors


class DocsManager:
    def __init__(self, config: Config):
        self.config = config

    def read_doc(self, key: str) -> str:
        try:
            path = self.config.doc_path(key)
        except KeyError:
            return ""
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def todos(self) -> Tuple[List[str], List[str]]:
        # Legacy helper retained for backward compatibility; newer configs may not
        # have a TODO doc at all.
        try:
            todo_path: Path = self.config.doc_path("todo")
        except KeyError:
            return [], []
        if not todo_path.exists():
            return [], []
        return parse_todos(todo_path.read_text(encoding="utf-8"))

    def todos_done(self) -> bool:
        outstanding, _ = self.todos()
        return len(outstanding) == 0
