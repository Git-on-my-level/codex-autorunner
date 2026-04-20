from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from ...core.git_utils import (
    describe_newt_reject_reasons,
    git_submodule_paths,
    reset_branch_from_origin_main,
)

DIRTY_WORKTREE_NEWT_MESSAGE = (
    "working tree has uncommitted changes; commit or stash before /newt"
)


@dataclass(frozen=True)
class NewtRejectState:
    reasons: tuple[str, ...]
    submodule_paths: tuple[str, ...]


@dataclass(frozen=True)
class NewtResetResult:
    default_branch: str
    setup_command_count: int
    submodule_paths: tuple[str, ...]


class NewtSetupCommandsError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        default_branch: str,
        submodule_paths: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.default_branch = default_branch
        self.submodule_paths = tuple(submodule_paths)


def build_discord_newt_branch_name(channel_id: str, workspace_root: Path) -> str:
    safe_channel_id = re.sub(r"[^a-zA-Z0-9]+", "-", channel_id).strip("-")
    if not safe_channel_id:
        safe_channel_id = "channel"
    return f"thread-{safe_channel_id}-{_workspace_branch_suffix(workspace_root)}"


def build_telegram_newt_branch_name(
    *,
    chat_id: int,
    workspace_root: Path,
    thread_id: Optional[int],
    message_id: int,
    update_id: int,
) -> str:
    safe_chat_id = re.sub(r"[^a-zA-Z0-9]+", "-", str(chat_id)).strip("-")
    if not safe_chat_id:
        safe_chat_id = "chat"
    if thread_id is None:
        thread_identity = f"msg-{message_id}-upd-{update_id}"
    else:
        thread_identity = f"thread-{thread_id}"
    safe_thread_id = re.sub(r"[^a-zA-Z0-9]+", "-", thread_identity).strip("-")
    if not safe_thread_id:
        safe_thread_id = "unscoped"
    return (
        f"thread-chat-{safe_chat_id}-{safe_thread_id}-"
        f"{_workspace_branch_suffix(workspace_root)}"
    )


def build_newt_workspace_token(workspace_root: Path) -> str:
    return hashlib.sha256(str(workspace_root).encode("utf-8")).hexdigest()[:12]


def is_newt_dirty_worktree_error(exc: BaseException) -> bool:
    return DIRTY_WORKTREE_NEWT_MESSAGE in str(exc)


def describe_newt_reject_state(repo_root: Path) -> NewtRejectState:
    return NewtRejectState(
        reasons=tuple(describe_newt_reject_reasons(repo_root)),
        submodule_paths=tuple(git_submodule_paths(repo_root)),
    )


def format_newt_submodule_summary(
    command_label: str,
    submodule_paths: Sequence[str],
    *,
    max_examples: int = 3,
) -> str:
    if not submodule_paths:
        return ""
    examples = ", ".join(f"`{path}`" for path in submodule_paths[:max_examples])
    remaining = len(submodule_paths) - max_examples
    if remaining > 0:
        noun = "submodule" if remaining == 1 else "submodules"
        examples = f"{examples}, and {remaining} more {noun}"
    noun = "Submodule" if len(submodule_paths) == 1 else "Submodules"
    return f"{noun} in this workspace: {examples}. `{command_label}` resets them too."


def build_newt_reject_lines(
    *,
    command_label: str,
    reasons: Sequence[str],
    submodule_paths: Sequence[str] = (),
    resolution_hint: Optional[str] = None,
) -> list[str]:
    lines = [
        f"Can't start a fresh `{command_label}` yet.",
        "",
        "Why:",
        *[f"- {reason}" for reason in reasons],
        "",
    ]
    summary = format_newt_submodule_summary(command_label, submodule_paths)
    if summary:
        lines.extend([summary, ""])
    if isinstance(resolution_hint, str) and resolution_hint.strip():
        lines.append(resolution_hint.strip())
    return lines


async def run_newt_branch_reset(
    workspace_root: Path,
    branch_name: str,
    *,
    repo_id_hint: Optional[str] = None,
    hub_client: Any = None,
) -> NewtResetResult:
    default_branch = await asyncio.to_thread(
        reset_branch_from_origin_main,
        workspace_root,
        branch_name,
    )
    submodule_paths = tuple(git_submodule_paths(workspace_root))
    setup_command_count = 0
    if hub_client is not None:
        try:
            from ...core.hub_control_plane import WorkspaceSetupCommandRequest

            cp_result = await hub_client.run_workspace_setup_commands(
                WorkspaceSetupCommandRequest(
                    workspace_root=str(workspace_root),
                    repo_id_hint=repo_id_hint,
                )
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise NewtSetupCommandsError(
                str(exc),
                default_branch=default_branch,
                submodule_paths=submodule_paths,
            ) from exc
        setup_command_count = cp_result.setup_command_count
    return NewtResetResult(
        default_branch=default_branch,
        setup_command_count=setup_command_count,
        submodule_paths=submodule_paths,
    )


def _workspace_branch_suffix(workspace_root: Path) -> str:
    return hashlib.sha256(str(workspace_root).encode("utf-8")).hexdigest()[:10]


__all__ = [
    "DIRTY_WORKTREE_NEWT_MESSAGE",
    "NewtRejectState",
    "NewtResetResult",
    "NewtSetupCommandsError",
    "build_discord_newt_branch_name",
    "build_newt_reject_lines",
    "build_newt_workspace_token",
    "build_telegram_newt_branch_name",
    "describe_newt_reject_state",
    "format_newt_submodule_summary",
    "is_newt_dirty_worktree_error",
    "run_newt_branch_reset",
]
