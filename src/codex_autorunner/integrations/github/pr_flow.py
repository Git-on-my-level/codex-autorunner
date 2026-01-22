from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ...core.utils import atomic_write, read_json

_logger = logging.getLogger(__name__)
_BULLET_PREFIXES = ("-", "*", "\u2022")


class PrFlowError(RuntimeError):
    pass


@dataclass(frozen=True)
class PrFlowReviewSummary:
    total: int
    major: int
    minor: int
    resolved: int


def _normalize_review_snippet(text: Optional[str], max_len: int) -> str:
    if not text:
        return ""

    lines = []
    bullet_flags = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        bullet = False
        for prefix in _BULLET_PREFIXES:
            if stripped.startswith(prefix):
                bullet = True
                stripped = stripped[len(prefix) :].lstrip()
                break
        stripped = re.sub(r"\s+", " ", stripped).strip()
        if stripped:
            lines.append(stripped)
            bullet_flags.append(bullet)

    if not lines:
        return ""

    normalized_parts = [lines[0]]
    for line, was_bullet in zip(lines[1:], bullet_flags[1:]):
        if was_bullet:
            normalized_parts.append(" - " + line)
        else:
            normalized_parts.append(" " + line)
    normalized = "".join(normalized_parts)

    normalized = re.sub(r"\s+", " ", normalized).strip()
    if max_len > 0 and len(normalized) > max_len:
        if max_len <= 3:
            return normalized[:max_len]
        return normalized[: max_len - 3].rstrip() + "..."
    return normalized


class PrFlowManager:
    def __init__(
        self,
        repo_root: Path,
        config: Optional[dict[str, Any]] = None,
        app_server_supervisor: Optional[Any] = None,
        opencode_supervisor: Optional[Any] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.repo_root = repo_root
        self._config = config or {}
        self._app_server_supervisor = app_server_supervisor
        self._opencode_supervisor = opencode_supervisor
        self._logger = logger or _logger

    def chatops_config(self) -> dict[str, Any]:
        cfg = self._config.get("chatops")
        return cfg if isinstance(cfg, dict) else {}

    def status(self) -> dict[str, Any]:
        raise PrFlowError("PR flow status is not available in refactor mode.")

    def stop(self) -> dict[str, Any]:
        raise PrFlowError("PR flow stop is not available in refactor mode.")

    def resume(self) -> dict[str, Any]:
        raise PrFlowError("PR flow resume is not available in refactor mode.")

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise PrFlowError("PR flow start is not available in refactor mode.")

    def _review_state_path(self, state: dict[str, Any]) -> Path:
        return self._require_worktree_root(state) / ".codex-autorunner" / "review.json"

    def _require_worktree_root(self, state: dict[str, Any]) -> Path:
        worktree_path = state.get("worktree_path")
        if not worktree_path:
            raise PrFlowError("Missing worktree path in state.")
        return Path(worktree_path)

    def _load_engine(self, worktree_root: Path) -> Any:
        raise PrFlowError("Engine loader not configured.")

    def _log_line(self, msg: str) -> None:
        self._logger.info(msg)

    def _apply_review_to_todo(
        self,
        state: dict[str, Any],
        bundle_path: str,
        summary: PrFlowReviewSummary,
        review_data: dict[str, Any],
    ) -> None:
        _ = (bundle_path, summary)
        worktree_root = self._require_worktree_root(state)
        engine = self._load_engine(worktree_root)
        todo_path = engine.config.doc_path("todo")
        existing = read_json(todo_path) if todo_path.suffix == ".json" else None
        if existing is not None:
            raise PrFlowError("JSON TODO format not supported for review injection.")
        todo_content = (
            todo_path.read_text(encoding="utf-8") if todo_path.exists() else "# TODO\n"
        )

        cycle = state.get("cycle") or 1
        tasks: list[str] = []
        for thread in review_data.get("threads") or []:
            if thread.get("isResolved"):
                continue
            for comment in thread.get("comments") or []:
                snippet = _normalize_review_snippet(comment.get("body"), max_len=100)
                if not snippet:
                    continue
                author = comment.get("author") or {}
                author_login = (
                    author.get("login") if isinstance(author, dict) else None
                ) or "unknown"
                path = comment.get("path") or "unknown"
                line = comment.get("line") or comment.get("position") or "?"
                tasks.append(
                    f"- [ ] Address review: {path}:{line} {snippet} ({author_login})"
                )

        if not tasks:
            return

        section = "\n".join([f"## Review Feedback Cycle {cycle}", "", *tasks, ""])
        if not todo_content.endswith("\n"):
            todo_content += "\n"
        updated = f"{todo_content}\n{section}"
        atomic_write(todo_path, updated)
        self._log_line(f"Applied {len(tasks)} review items to TODO.")
