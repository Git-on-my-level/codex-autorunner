from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...core.state import now_iso
from ...core.utils import atomic_write, read_json
from .pr_flow import PrFlowError, PrFlowManager
from .service import GitHubService

COMMANDS = {"implement", "fix", "status", "stop", "resume"}
ISSUE_URL_RE = re.compile(r"/issues/(?P<num>\d+)")


def _chatops_state_path(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "pr_flow" / "chatops_state.json"


def _parse_command(text: str) -> Optional[tuple[str, list[str]]]:
    for line in (text or "").splitlines():
        if "@car" not in line and "/car" not in line:
            continue
        try:
            tokens = [tok for tok in shlex.split(line) if tok]
        except ValueError:
            tokens = [tok for tok in line.split() if tok]
        for idx, token in enumerate(tokens):
            raw = token.strip().rstrip(":,")
            if raw.startswith("@car") or raw == "/car":
                if idx + 1 >= len(tokens):
                    return None
                cmd = tokens[idx + 1].strip().lower()
                args = tokens[idx + 2 :]
                if cmd in COMMANDS:
                    return cmd, args
    return None


def _parse_flags(args: list[str]) -> dict[str, Any]:
    flags: dict[str, Any] = {}
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token == "--until" and idx + 1 < len(args):
            flags["stop_condition"] = args[idx + 1]
            idx += 2
            continue
        if token == "--draft":
            flags["draft"] = True
            idx += 1
            continue
        if token == "--ready":
            flags["draft"] = False
            idx += 1
            continue
        if token == "--base" and idx + 1 < len(args):
            flags["base_branch"] = args[idx + 1]
            idx += 2
            continue
        if token in ("--max-cycles", "--max_cycles") and idx + 1 < len(args):
            try:
                flags["max_cycles"] = int(args[idx + 1])
            except ValueError:
                pass
            idx += 2
            continue
        idx += 1
    return flags


def _extract_issue_number(issue_url: str) -> Optional[int]:
    if not issue_url:
        return None
    match = ISSUE_URL_RE.search(issue_url)
    if not match:
        return None
    try:
        return int(match.group("num"))
    except ValueError:
        return None


class GitHubChatOpsPoller:
    def __init__(
        self,
        repo_root: Path,
        pr_flow: PrFlowManager,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._repo_root = repo_root
        self._pr_flow = pr_flow
        self._logger = logger or logging.getLogger("codex_autorunner.github_chatops")
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        cfg = self._pr_flow.chatops_config()
        if not cfg.get("enabled", False):
            return
        poll_interval = int(cfg.get("poll_interval_seconds", 60))
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "github.chatops.poll.failed",
                    exc=exc,
                )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop_event.set()

    async def poll_once(self) -> None:
        gh = GitHubService(self._repo_root)
        if not gh.gh_available() or not gh.gh_authenticated():
            return
        repo = gh.repo_info()
        owner, repo_name = repo.name_with_owner.split("/", 1)
        state = self._load_state()
        since = state.get("last_seen")
        comments = gh.issue_comments(
            owner=owner, repo=repo_name, since=since, limit=100
        )
        if not comments:
            return
        comments.sort(key=lambda item: item.get("created_at") or "")
        processed = set(state.get("processed_ids") or [])
        max_seen = since or ""
        for comment in comments:
            comment_id = comment.get("id")
            if comment_id in processed:
                continue
            processed.add(comment_id)
            created_at = comment.get("created_at") or ""
            if created_at and created_at > max_seen:
                max_seen = created_at
            if not self._authorized(comment):
                continue
            parsed = _parse_command(comment.get("body") or "")
            if not parsed:
                continue
            command, args = parsed
            issue_number = _extract_issue_number(comment.get("issue_url") or "")
            if not issue_number:
                continue
            issue_meta = gh.issue_meta(owner=owner, repo=repo_name, number=issue_number)
            is_pr = bool(issue_meta.get("pull_request"))
            response = await self._handle_command(
                gh,
                command,
                args,
                issue_number=issue_number,
                is_pr=is_pr,
            )
            if response:
                gh.create_issue_comment(
                    owner=owner,
                    repo=repo_name,
                    number=issue_number,
                    body=response,
                )
        self._save_state(
            {
                "processed_ids": list(processed)[-500:],
                "last_seen": max_seen or now_iso(),
            }
        )

    def _authorized(self, comment: dict[str, Any]) -> bool:
        cfg = self._pr_flow.chatops_config()
        user = comment.get("user") or {}
        login = user.get("login") if isinstance(user, dict) else None
        if cfg.get("ignore_bots", True):
            if isinstance(user, dict) and user.get("type") == "Bot":
                return False
            if isinstance(login, str) and login.endswith("[bot]"):
                return False
        allow_users = cfg.get("allow_users") or []
        allow_assoc = cfg.get("allow_associations") or []
        allowed = False
        if allow_users:
            allowed = allowed or (login in allow_users)
        if allow_assoc:
            assoc = str(comment.get("author_association") or "").upper()
            allowed = allowed or (assoc in {str(a).upper() for a in allow_assoc})
        return allowed

    async def _handle_command(
        self,
        gh: GitHubService,
        command: str,
        args: list[str],
        *,
        issue_number: int,
        is_pr: bool,
    ) -> Optional[str]:
        flags = _parse_flags(args)
        try:
            if command == "status":
                flow = self._pr_flow.status()
                return self._format_status(flow)
            if command == "stop":
                flow = self._pr_flow.stop()
                return f"Stopped workflow {flow.get('id') or '(unknown)'}."
            if command == "resume":
                flow = self._pr_flow.resume()
                return self._format_status(flow, prefix="Resumed")
            if command == "implement":
                if is_pr:
                    return "Command ignored: implement is for issues, not PRs."
                payload = {
                    "mode": "issue",
                    "issue": str(issue_number),
                    **flags,
                }
                flow = self._pr_flow.start(payload=payload)
                return self._format_status(flow, prefix="Started")
            if command == "fix":
                if not is_pr:
                    return "Command ignored: fix is for PRs."
                payload = {
                    "mode": "pr",
                    "pr": str(issue_number),
                    **flags,
                }
                flow = self._pr_flow.start(payload=payload)
                return self._format_status(flow, prefix="Started")
        except PrFlowError as exc:
            return f"PR flow error: {exc}"
        except Exception as exc:
            return f"PR flow error: {exc}"
        return None

    def _format_status(self, flow: dict[str, Any], *, prefix: str = "Status") -> str:
        status = flow.get("status") or "unknown"
        step = flow.get("step") or "unknown"
        wf_id = flow.get("id") or "unknown"
        pr_url = flow.get("pr_url")
        line = f"{prefix} workflow {wf_id}: {status} (step: {step})"
        if pr_url:
            line = f"{line}\nPR: {pr_url}"
        return line

    def _load_state(self) -> dict[str, Any]:
        path = _chatops_state_path(self._repo_root)
        data = read_json(path) or {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("processed_ids", [])
        data.setdefault("last_seen", "")
        return data

    def _save_state(self, state: dict[str, Any]) -> None:
        path = _chatops_state_path(self._repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(state, indent=2) + "\n")
