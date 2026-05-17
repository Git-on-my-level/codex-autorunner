from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

import yaml

from ...manifest import ManifestRepo
from ..hub_topology import HubTopologyRepository
from ..hub_worktree_manager import WorktreeManager
from ..utils import is_within
from .models import (
    JOB_RUNNING,
    TARGET_POLICY_AUTO_WORKTREE,
    TARGET_POLICY_EXISTING_REPO,
    TARGET_POLICY_EXISTING_WORKTREE,
    TARGET_POLICY_HUB,
    TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
    TARGET_POLICY_PR_WORKTREE,
    AutomationJob,
)
from .worker import AutomationExecutorResult

TicketFlowStarter = Callable[..., Awaitable[Any]]
RunCoroutine = Callable[[Any], Any]

_SAFE_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_TICKET_NAME_RE = re.compile(r"^TICKET-(\d{3,})(?:[^/]*)\.md$", re.IGNORECASE)
_TICKET_ID_RE = re.compile(r"^[A-Za-z0-9._-]{6,128}$")


@dataclass(frozen=True)
class ResolvedAutomationWorktree:
    base_repo_id: str
    repo_id: str
    path: Path
    branch: str
    created: bool


class TicketFlowAutomationExecutor:
    def __init__(
        self,
        *,
        hub_root: Path,
        topology_repository: HubTopologyRepository,
        worktree_manager: WorktreeManager,
        start_flow_run_fn: Optional[TicketFlowStarter] = None,
        run_coroutine_fn: Optional[RunCoroutine] = None,
    ) -> None:
        self._hub_root = hub_root.resolve()
        self._topology_repository = topology_repository
        self._worktree_manager = worktree_manager
        self._start_flow_run_fn = start_flow_run_fn or _default_start_ticket_flow_run
        self._run_coroutine_fn = run_coroutine_fn or _run_coroutine

    def execute(self, job: AutomationJob) -> AutomationExecutorResult:
        resolved = self.resolve_worktree(job)
        materialized = self.materialize_ticket_pack(job, resolved.path)
        metadata = {
            "automation_job_id": job.job_id,
            "automation_rule_id": job.rule_id,
            "automation_event_id": job.event_id,
            "automation_worktree_repo_id": resolved.repo_id,
            "automation_base_repo_id": resolved.base_repo_id,
            "automation_materialized": materialized,
        }
        record = self._run_coroutine_fn(
            self._start_flow_run_fn(
                resolved.path,
                input_data={"workspace_root": str(resolved.path)},
                metadata=metadata,
                run_id=str(job.executor.get("run_id") or "").strip() or None,
            )
        )
        run_id = str(getattr(record, "id", "") or "")
        return AutomationExecutorResult(
            status=JOB_RUNNING,
            summary="started ticket-flow automation run",
            data={
                "execution_phase": "running",
                "worktree": {
                    "base_repo_id": resolved.base_repo_id,
                    "repo_id": resolved.repo_id,
                    "path": str(resolved.path),
                    "branch": resolved.branch,
                    "created": resolved.created,
                },
                "materialized": materialized,
                "run_id": run_id,
            },
            execution_refs={
                "ticket_flow_repo_id": resolved.repo_id,
                "ticket_flow_worktree_id": resolved.repo_id,
                "ticket_flow_run_id": run_id,
            },
        )

    def resolve_worktree(self, job: AutomationJob) -> ResolvedAutomationWorktree:
        target = dict(job.target or {})
        policy = str(
            target.get("policy") or target.get("target_policy") or TARGET_POLICY_HUB
        ).strip()
        if policy == TARGET_POLICY_HUB:
            raise ValueError("ticket_flow executor requires a repo/worktree target")
        manifest = self._topology_repository.load_manifest()
        base_repo_id = _resolve_base_repo_id(manifest.repos, target, policy)
        branch = automation_branch_name(job, target=target, policy=policy)
        repo_id = automation_repo_id(base_repo_id, branch)
        existing = manifest.get(repo_id)
        if existing is not None:
            path = (self._hub_root / existing.path).resolve()
            if existing.kind != "worktree":
                raise ValueError(f"automation repo id is not a worktree: {repo_id}")
            if not path.exists():
                raise ValueError(f"automation worktree missing on disk: {repo_id}")
            return ResolvedAutomationWorktree(
                base_repo_id=base_repo_id,
                repo_id=repo_id,
                path=path,
                branch=branch,
                created=False,
            )
        snapshot = self._worktree_manager.create_worktree(
            base_repo_id=base_repo_id,
            branch=branch,
        )
        return ResolvedAutomationWorktree(
            base_repo_id=base_repo_id,
            repo_id=snapshot.id,
            path=snapshot.path.resolve(),
            branch=branch,
            created=True,
        )

    def materialize_ticket_pack(
        self, job: AutomationJob, worktree_path: Path
    ) -> dict[str, Any]:
        pack = _ticket_pack_config(job)
        tickets, docs = _load_pack_files(pack, base_path=worktree_path)
        ticket_dir = worktree_path / ".codex-autorunner" / "tickets"
        context_dir = worktree_path / ".codex-autorunner" / "contextspace"
        for path, content in tickets:
            target = _safe_materialized_path(ticket_dir, path, must_be_ticket=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for path, content in docs:
            target = _safe_materialized_path(context_dir, path, must_be_ticket=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        errors = _validate_materialized_tickets(ticket_dir)
        if errors:
            raise ValueError("; ".join(errors))
        return {
            "ticket_count": len(tickets),
            "contextspace_count": len(docs),
            "source": str(pack.get("source") or pack.get("kind") or "inline"),
        }


def automation_branch_name(
    job: AutomationJob, *, target: dict[str, Any], policy: str
) -> str:
    rule_slug = _slug(str(target.get("rule_slug") or job.rule_id), fallback="rule")
    pr_number = target.get("pr_number") or _nested(
        job.payload, "event", "payload", "pr", "number"
    )
    if policy == TARGET_POLICY_PR_WORKTREE and pr_number not in (None, ""):
        return f"automation/pr-{_slug(str(pr_number), fallback='unknown')}/{rule_slug}"
    return f"automation/{rule_slug}/{_slug(job.job_id[:12], fallback='job')}"


def automation_repo_id(base_repo_id: str, branch: str) -> str:
    safe_branch = re.sub(r"[^a-zA-Z0-9._/-]+", "-", branch).strip("-") or "work"
    return f"{base_repo_id}--{safe_branch.replace('/', '-')}"


def _resolve_base_repo_id(
    repos: Iterable[ManifestRepo], target: dict[str, Any], policy: str
) -> str:
    repo_by_id = {repo.id: repo for repo in repos}
    raw_repo_id = str(
        target.get("base_repo_id")
        or target.get("repo_id")
        or target.get("worktree_id")
        or ""
    ).strip()
    if not raw_repo_id:
        raise ValueError(f"{policy} target requires repo_id/base_repo_id")
    repo = repo_by_id.get(raw_repo_id)
    if repo is None:
        raise ValueError(f"target repo not found: {raw_repo_id}")
    if policy in {
        TARGET_POLICY_EXISTING_REPO,
        TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
        TARGET_POLICY_AUTO_WORKTREE,
        TARGET_POLICY_PR_WORKTREE,
    }:
        if repo.kind != "base":
            raise ValueError(f"{policy} target must reference a base repo")
        return repo.id
    if policy == TARGET_POLICY_EXISTING_WORKTREE:
        if repo.kind != "worktree" or not repo.worktree_of:
            raise ValueError("existing_worktree target must reference a worktree")
        if repo.worktree_of not in repo_by_id:
            raise ValueError(f"base repo not found for worktree: {repo.worktree_of}")
        return repo.worktree_of
    raise ValueError(f"unsupported target policy for ticket_flow: {policy}")


def _ticket_pack_config(job: AutomationJob) -> dict[str, Any]:
    pack = job.executor.get("ticket_pack") or job.executor.get("pack")
    if pack is None:
        pack = job.executor
    if not isinstance(pack, dict):
        raise ValueError("ticket_flow ticket_pack must be an object")
    return dict(pack)


def _load_pack_files(
    pack: dict[str, Any], *, base_path: Path
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    source = str(pack.get("source") or pack.get("kind") or "inline").strip()
    if source == "builtin_template":
        name = str(pack.get("name") or pack.get("template") or "bootstrap").strip()
        if name != "bootstrap":
            raise ValueError(f"unknown builtin ticket_flow template: {name}")
        return [("TICKET-001.md", _render_bootstrap_ticket_template())], []
    if source == "repo_relative_path":
        raw_path = str(pack.get("path") or pack.get("repo_relative_path") or "").strip()
        root = _safe_repo_relative_path(base_path, raw_path)
        return _load_pack_from_directory(root)
    if source == "generated_pack":
        raise ValueError("generated_pack ticket sources require a configured producer")
    if source != "inline":
        raise ValueError(f"unsupported ticket pack source: {source}")
    tickets = _inline_entries(pack.get("tickets") or pack.get("ticket_files"))
    docs = _inline_entries(pack.get("contextspace") or pack.get("contextspace_files"))
    if not tickets:
        raise ValueError("ticket_flow pack must include at least one ticket")
    return tickets, docs


def _load_pack_from_directory(
    root: Path,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    ticket_root = root / ".codex-autorunner" / "tickets"
    context_root = root / ".codex-autorunner" / "contextspace"
    if not ticket_root.exists():
        ticket_root = root / "tickets" if (root / "tickets").exists() else root
    tickets: list[tuple[str, str]] = []
    for path in sorted(ticket_root.iterdir(), key=lambda item: item.name):
        if path.is_file() and _parse_ticket_index(path.name) is not None:
            tickets.append((path.name, path.read_text(encoding="utf-8")))
    docs: list[tuple[str, str]] = []
    if context_root.exists() and context_root.is_dir():
        for path in sorted(context_root.iterdir(), key=lambda item: item.name):
            if path.is_file() and path.suffix == ".md":
                docs.append((path.name, path.read_text(encoding="utf-8")))
    if not tickets:
        raise ValueError(f"ticket pack path contains no ticket files: {root}")
    return tickets, docs


def _inline_entries(raw: Any) -> list[tuple[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("inline ticket pack entries must be a list")
    entries: list[tuple[str, str]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"inline entry {idx} must be an object")
        path = str(item.get("path") or item.get("name") or "").strip()
        content = item.get("content")
        if not path:
            raise ValueError(f"inline entry {idx} requires path")
        if not isinstance(content, str):
            raise ValueError(f"inline entry {idx} requires string content")
        entries.append((path, content))
    return entries


def _safe_repo_relative_path(root: Path, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("repo_relative_path source requires path")
    candidate = Path(raw_path)
    if candidate.is_absolute() or "\\" in raw_path or ".." in candidate.parts:
        raise ValueError("repo-relative ticket pack path is unsafe")
    resolved = (root / candidate).resolve()
    if not is_within(root=root.resolve(), target=resolved):
        raise ValueError("repo-relative ticket pack path escapes target repo")
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"ticket pack path not found: {raw_path}")
    return resolved


def _safe_materialized_path(root: Path, raw_path: str, *, must_be_ticket: bool) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute() or "\\" in raw_path or ".." in candidate.parts:
        raise ValueError(f"unsafe materialized path: {raw_path}")
    if must_be_ticket and _parse_ticket_index(candidate.name) is None:
        raise ValueError(f"ticket filename must match TICKET-###*.md: {raw_path}")
    if not must_be_ticket and candidate.suffix != ".md":
        raise ValueError(f"contextspace file must be markdown: {raw_path}")
    resolved = (root / candidate).resolve()
    if not is_within(root=root.resolve(), target=resolved):
        raise ValueError(f"materialized path escapes destination: {raw_path}")
    return resolved


def _validate_materialized_tickets(ticket_dir: Path) -> list[str]:
    errors: list[str] = []
    index_to_paths: dict[int, list[str]] = {}
    ticket_id_to_paths: dict[str, list[str]] = {}
    ticket_count = 0
    unfinished_count = 0
    for path in sorted(ticket_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        index = _parse_ticket_index(path.name)
        if index is None:
            continue
        ticket_count += 1
        index_to_paths.setdefault(index, []).append(path.name)
        ticket_errors, ticket_id, done = _validate_ticket_content(path)
        if done is False:
            unfinished_count += 1
        errors.extend([f"{path.name}: {error}" for error in ticket_errors])
        if ticket_id:
            ticket_id_to_paths.setdefault(ticket_id, []).append(path.name)
    for index, filenames in index_to_paths.items():
        if len(filenames) > 1:
            errors.append(
                f"Duplicate ticket index {index:03d}: multiple files share the same index"
            )
    for ticket_id, filenames in ticket_id_to_paths.items():
        if len(filenames) > 1:
            errors.append(
                f"Duplicate ticket_id {ticket_id!r}: multiple files share the same logical ticket identity"
            )
    if ticket_count > 0 and unfinished_count == 0:
        errors.append("Ticket pack must include at least one unfinished ticket.")
    return errors


def _parse_ticket_index(name: str) -> Optional[int]:
    match = _TICKET_NAME_RE.match(name)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _validate_ticket_content(
    path: Path,
) -> tuple[list[str], Optional[str], Optional[bool]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ["Failed to read ticket"], None, None
    data = _parse_frontmatter(raw)
    if data is None:
        return ["Missing or invalid YAML frontmatter (expected a mapping)."], None, None
    errors: list[str] = []
    ticket_id = data.get("ticket_id")
    if not isinstance(ticket_id, str) or not _TICKET_ID_RE.match(ticket_id.strip()):
        errors.append(
            "frontmatter.ticket_id is required and must match [A-Za-z0-9._-]{6,128}."
        )
        ticket_id_text = None
    else:
        ticket_id_text = ticket_id.strip()
    agent = data.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        errors.append("frontmatter.agent is required (e.g. 'codex' or 'opencode').")
    done = data.get("done")
    if not isinstance(done, bool):
        errors.append("frontmatter.done is required and must be a boolean.")
        done_value = None
    else:
        done_value = done
    if "depends_on" in data:
        errors.append("frontmatter.depends_on is not supported in automation packs.")
    return errors, ticket_id_text, done_value


def _parse_frontmatter(raw: str) -> Optional[dict[str, Any]]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end_idx: Optional[int] = None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() in {"---", "..."}:
            end_idx = idx
            break
    if end_idx is None:
        return None
    try:
        loaded = yaml.safe_load("\n".join(lines[1:end_idx]))
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _render_bootstrap_ticket_template() -> str:
    ticket_id = f"tkt_{uuid.uuid4().hex}"
    return f"""---
agent: codex
done: false
ticket_id: "{ticket_id}"
title: Bootstrap ticket plan
goal: Capture scope and seed follow-up tickets
---

You are the first ticket in a new ticket_flow run.

- Read `.codex-autorunner/ISSUE.md`. If it is missing, ask the user for scope.
- If helpful, create or update contextspace docs under `.codex-autorunner/contextspace/`.
- Break the work into additional `TICKET-00X.md` files with clear owners/goals.
"""


def _slug(value: str, *, fallback: str) -> str:
    slug = _SAFE_SLUG_RE.sub("-", value.strip().lower()).strip("-._")
    return slug[:80] or fallback


def _nested(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _run_coroutine(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _default_start_ticket_flow_run(*args: Any, **kwargs: Any) -> Any:
    from ...flows.ticket_flow.runtime_helpers import start_ticket_flow_run

    return await start_ticket_flow_run(*args, **kwargs)


__all__ = [
    "ResolvedAutomationWorktree",
    "TicketFlowAutomationExecutor",
    "automation_branch_name",
    "automation_repo_id",
]
