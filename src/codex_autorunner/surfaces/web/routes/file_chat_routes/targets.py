from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException, Request

from .....contextspace.paths import (
    contextspace_doc_path,
    normalize_contextspace_doc_kind,
)
from .....core import drafts as draft_utils
from .....core.context_awareness import CAR_AWARENESS_BLOCK, format_file_role_addendum
from .....core.file_chat_keys import ticket_chat_scope, ticket_state_key
from .....core.utils import find_repo_root

if TYPE_CHECKING:
    from typing import Any, Dict


FILE_CHAT_STATE_NAME = draft_utils.FILE_CHAT_STATE_NAME


def _state_path(repo_root: Path) -> Path:
    return draft_utils.state_path(repo_root)


def _load_state(repo_root: Path) -> Dict[str, Any]:
    return draft_utils.load_state(repo_root)


def _save_state(repo_root: Path, state: Dict[str, Any]) -> None:
    draft_utils.save_state(repo_root, state)


def _hash_content(content: str) -> str:
    return draft_utils.hash_content(content)


def _resolve_repo_root(request: Optional[Request] = None) -> Path:
    if request is not None:
        engine = getattr(request.app.state, "engine", None)
        repo_root = getattr(engine, "repo_root", None)
        if isinstance(repo_root, Path):
            return repo_root
        if isinstance(repo_root, str):
            try:
                return Path(repo_root)
            except Exception:
                pass
    return find_repo_root()


def resolve_repo_root(request: Optional[Request] = None) -> Path:
    return _resolve_repo_root(request)


def _ticket_path(repo_root: Path, index: int) -> Path:
    return repo_root / ".codex-autorunner" / "tickets" / f"TICKET-{index:03d}.md"


def _parse_contextspace_kind(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value.endswith(".md"):
        value = value[:-3]
    return normalize_contextspace_doc_kind(value)


@dataclass(frozen=True)
class _Target:
    target: str
    kind: str
    id: str
    chat_scope: str
    path: Path
    rel_path: str
    state_key: str


def parse_target(repo_root: Path, raw: str) -> _Target:
    target = (raw or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="target is required")

    if target.lower().startswith("ticket:"):
        suffix = target.split(":", 1)[1].strip()
        if not suffix.isdigit():
            raise HTTPException(status_code=400, detail="invalid ticket target")
        idx = int(suffix)
        if idx <= 0:
            raise HTTPException(status_code=400, detail="invalid ticket target")
        path = _ticket_path(repo_root, idx)
        rel = (
            str(path.relative_to(repo_root))
            if path.is_relative_to(repo_root)
            else str(path)
        )
        return _Target(
            target=f"ticket:{idx}",
            kind="ticket",
            id=f"{idx:03d}",
            chat_scope=ticket_chat_scope(idx, path),
            path=path,
            rel_path=rel,
            state_key=ticket_state_key(idx, path),
        )

    if target.lower().startswith("contextspace:"):
        suffix_raw = target.split(":", 1)[1].strip()
        if not suffix_raw:
            raise HTTPException(status_code=400, detail="invalid contextspace target")
        try:
            kind = _parse_contextspace_kind(suffix_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        path = contextspace_doc_path(repo_root, kind)
        rel_suffix = f"{kind}.md"

        rel = (
            str(path.relative_to(repo_root))
            if path.is_relative_to(repo_root)
            else str(path)
        )
        return _Target(
            target=f"contextspace:{rel_suffix}",
            kind="contextspace",
            id=rel_suffix,
            chat_scope=f"contextspace:{rel_suffix}",
            path=path,
            rel_path=rel,
            state_key=f"contextspace_{rel_suffix.replace('/', '_')}",
        )

    raise HTTPException(status_code=400, detail=f"invalid target: {target}")


def build_file_chat_prompt(
    *,
    target: _Target,
    message: str,
    before: str,
    editable_rel_path: Optional[str] = None,
) -> str:
    prompt_path = editable_rel_path or target.rel_path
    draft_path_block = ""
    instruction_lines = [
        "- This is a single-turn edit request. Don't ask the user questions.",
        "- You may read other files for context, but only modify the editable path.",
        "- Do not modify the live target file directly; prepare the deferred-apply draft instead.",
        "- If no changes are needed, explain why without editing the file.",
        "- Respond with a short summary of what you did.",
    ]
    instructions = "\n".join(instruction_lines)
    if target.kind == "ticket":
        file_role_context = (
            f"{format_file_role_addendum('ticket', target.rel_path)}\n"
            "Edits here change what ticket flow agent will do; keep YAML "
            "frontmatter valid."
        )
    elif target.kind == "contextspace":
        file_role_context = (
            f"{format_file_role_addendum('contextspace', target.rel_path)}\n"
            "These docs act as shared memory across ticket turns."
        )
    else:
        file_role_context = format_file_role_addendum("other", target.rel_path)

    if editable_rel_path and editable_rel_path != target.rel_path:
        draft_path_block = (
            "<live_target_path>\n"
            f"{target.rel_path}\n"
            "</live_target_path>\n\n"
            "<editable_path>\n"
            f"{editable_rel_path}\n"
            "</editable_path>\n\n"
        )

    return (
        f"{CAR_AWARENESS_BLOCK}\n\n"
        "<file_role_context>\n"
        f"{file_role_context}\n"
        "</file_role_context>\n\n"
        "You are editing a single file in Codex Autorunner.\n\n"
        "<target>\n"
        f"{target.target}\n"
        "</target>\n\n"
        f"{draft_path_block}"
        "<path>\n"
        f"{prompt_path}\n"
        "</path>\n\n"
        "<instructions>\n"
        f"{instructions}\n"
        "</instructions>\n\n"
        "<user_request>\n"
        f"{message}\n"
        "</user_request>\n\n"
        "<FILE_CONTENT>\n"
        f"{before[:12000]}\n"
        "</FILE_CONTENT>\n"
    )


def read_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def build_patch(rel_path: str, before: str, after: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        lineterm="",
    )
    return "\n".join(diff)
