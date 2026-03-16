from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException, Request

from .....contextspace.paths import (
    CONTEXTSPACE_DOC_KINDS,
    contextspace_doc_path,
    normalize_contextspace_rel_path,
)
from .....core import drafts as draft_utils
from .....core.car_context import DEFAULT_REPO_THREAD_CONTEXT_PROFILE
from .....core.context_awareness import (
    format_file_role_addendum,
    maybe_inject_car_awareness,
)
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


def _ticket_path(repo_root: Path, index: int) -> Path:
    return repo_root / ".codex-autorunner" / "tickets" / f"TICKET-{index:03d}.md"


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

        if suffix_raw.lower() in CONTEXTSPACE_DOC_KINDS:
            path = contextspace_doc_path(repo_root, suffix_raw)
            rel_suffix = f"{suffix_raw}.md"
        else:
            try:
                path, rel_suffix = normalize_contextspace_rel_path(
                    repo_root, suffix_raw
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

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


def build_file_chat_prompt(*, target: _Target, message: str, before: str) -> str:
    declared_profile = DEFAULT_REPO_THREAD_CONTEXT_PROFILE
    if target.kind == "ticket":
        declared_profile = "car_core"
        file_role_context = (
            f"{format_file_role_addendum('ticket', target.rel_path)}\n"
            "Edits here change what ticket flow agent will do; keep YAML "
            "frontmatter valid."
        )
    elif target.kind == "contextspace":
        declared_profile = "car_core"
        file_role_context = (
            f"{format_file_role_addendum('contextspace', target.rel_path)}\n"
            "These docs act as shared memory across ticket turns."
        )
    else:
        file_role_context = format_file_role_addendum("other", target.rel_path)
    car_context, _ = maybe_inject_car_awareness(
        "",
        declared_profile=declared_profile,
        target_path=target.rel_path,
    )
    context_prefix = f"{car_context}\n\n" if car_context else ""

    return (
        f"{context_prefix}"
        "<file_role_context>\n"
        f"{file_role_context}\n"
        "</file_role_context>\n\n"
        "You are editing a single file in Codex Autorunner.\n\n"
        "<target>\n"
        f"{target.target}\n"
        "</target>\n\n"
        "<path>\n"
        f"{target.rel_path}\n"
        "</path>\n\n"
        "<instructions>\n"
        "- This is a single-turn edit request. Don't ask the user questions.\n"
        "- You may read other files for context, but only modify the target file.\n"
        "- If no changes are needed, explain why without editing the file.\n"
        "- Respond with a short summary of what you did.\n"
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
