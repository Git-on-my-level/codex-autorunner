import re
from pathlib import Path
from typing import List, Tuple

from .config import Config


def parse_todos(content: str) -> Tuple[List[str], List[str]]:
    outstanding: List[str] = []
    done: List[str] = []
    if not content:
        return outstanding, done
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            outstanding.append(stripped[5:].strip())
        elif stripped.lower().startswith("- [x]"):
            done.append(stripped[5:].strip())
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
    lines = content.splitlines()
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
        path = self.config.doc_path(key)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def todos(self) -> Tuple[List[str], List[str]]:
        todo_path: Path = self.config.doc_path("todo")
        if not todo_path.exists():
            return [], []
        return parse_todos(todo_path.read_text(encoding="utf-8"))

    def todos_done(self) -> bool:
        outstanding, _ = self.todos()
        return len(outstanding) == 0
