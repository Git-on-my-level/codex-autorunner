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
