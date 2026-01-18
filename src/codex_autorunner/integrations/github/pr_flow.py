from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ...agents.opencode.supervisor import OpenCodeSupervisor
from ...core.config import ConfigError
from ...core.doc_chat import DocChatService
from ...core.engine import Engine, LockError
from ...core.hub import HubSupervisor
from ...core.locks import FileLock, FileLockBusy, FileLockError
from ...core.logging_utils import log_event
from ...core.state import now_iso
from ...core.utils import atomic_write, read_json
from ...manifest import ManifestRepo, load_manifest
from ...spec_ingest import SpecIngestError, SpecIngestService
from ..app_server.supervisor import WorkspaceAppServerSupervisor
from .service import GitHubService, parse_pr_input

PR_FLOW_VERSION = 1
DEFAULT_PR_FLOW_CONFIG: dict[str, Any] = {
    "enabled": True,
    "max_cycles": 3,
    "stop_condition": "no_issues",
    "max_implementation_runs": None,
    "max_wallclock_seconds": None,
    "review": {
        "include_codex": True,
        "include_github": True,
        "include_checks": True,
        "severity_threshold": "minor",
    },
    "chatops": {
        "enabled": False,
        "poll_interval_seconds": 60,
        "allow_users": [],
        "allow_associations": [],
        "ignore_bots": True,
    },
}
REVIEW_MINOR_KEYWORDS = (
    "nit",
    "minor",
    "optional",
    "non-blocking",
    "non blocking",
    "suggestion",
)
REVIEW_MAJOR_KEYWORDS = (
    "blocker",
    "must",
    "required",
    "error",
    "fail",
    "security",
    "bug",
)


class PrFlowError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class PrFlowReviewSummary:
    total: int
    major: int
    minor: int
    resolved: int


def _merge_defaults(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def _pr_flow_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    github_cfg = raw_config.get("github") if isinstance(raw_config, dict) else None
    github_cfg = github_cfg if isinstance(github_cfg, dict) else {}
    pr_flow = github_cfg.get("pr_flow")
    pr_flow = pr_flow if isinstance(pr_flow, dict) else {}
    return _merge_defaults(DEFAULT_PR_FLOW_CONFIG, pr_flow)


def _workflow_root(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "pr_flow"


def _default_state() -> dict[str, Any]:
    return {
        "version": PR_FLOW_VERSION,
        "id": None,
        "status": "idle",
        "mode": None,
        "step": None,
        "issue": None,
        "pr": None,
        "issue_number": None,
        "issue_title": None,
        "issue_url": None,
        "pr_number": None,
        "pr_url": None,
        "base_branch": None,
        "head_branch": None,
        "worktree_repo_id": None,
        "worktree_path": None,
        "cycle": 0,
        "max_cycles": None,
        "stop_condition": None,
        "draft": None,
        "max_implementation_runs": None,
        "max_wallclock_seconds": None,
        "review_summary": None,
        "review_bundle_path": None,
        "review_snapshot_index": 0,
        "workflow_log_path": None,
        "final_report_path": None,
        "last_error": None,
        "started_at": None,
        "updated_at": None,
        "finished_at": None,
    }


def _slugify(value: str, *, max_len: int = 48) -> str:
    raw = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip().lower()).strip("-")
    if not raw:
        return "work"
    return raw[:max_len].strip("-") or "work"


def _classify_review_text(text: str) -> str:
    lowered = (text or "").lower()
    if any(word in lowered for word in REVIEW_MAJOR_KEYWORDS):
        return "major"
    if any(word in lowered for word in REVIEW_MINOR_KEYWORDS):
        return "minor"
    return "major"


def _normalize_stop_condition(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = value.strip().lower()
    if raw in ("minor", "minor_only", "minor-only"):
        return "minor_only"
    if raw in ("clean", "no_issues", "no-issues"):
        return "no_issues"
    return raw


def _format_review_summary(summary: Optional[PrFlowReviewSummary]) -> Optional[dict]:
    if summary is None:
        return None
    return {
        "total": summary.total,
        "major": summary.major,
        "minor": summary.minor,
        "resolved": summary.resolved,
    }


def _safe_text(value: Any, limit: int = 400) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _normalize_review_snippet(value: Any, limit: int = 100) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    for marker in ("- ", "* ", "• ", "-  ", "*  ", "•  "):
        if text.startswith(marker):
            text = text[len(marker) :]
            break
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class PrFlowManager:
    def __init__(
        self,
        repo_root: Path,
        *,
        app_server_supervisor: Optional[WorkspaceAppServerSupervisor] = None,
        opencode_supervisor: Optional[OpenCodeSupervisor] = None,
        logger: Optional[logging.Logger] = None,
        hub_root: Optional[Path] = None,
    ) -> None:
        self.repo_root = repo_root
        self._app_server_supervisor = app_server_supervisor
        self._opencode_supervisor = opencode_supervisor
        self._logger = logger or logging.getLogger("codex_autorunner.pr_flow")
        self._hub_root = hub_root
        self._state_path = _workflow_root(repo_root) / "state.json"
        self._lock_path = repo_root / ".codex-autorunner" / "locks" / "pr_flow.lock"
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._lock_handle: Optional[FileLock] = None
        self._config = _pr_flow_config(self._load_engine().config.raw)

    def status(self) -> dict[str, Any]:
        state = self._load_state()
        is_running = bool(self._thread and self._thread.is_alive())
        state["running"] = is_running
        if state.get("status") == "running" and not is_running:
            state["status"] = "stopped"
            state["last_error"] = "Recovered from restart"
            state["updated_at"] = now_iso()
            self._save_state(state)
        return state

    def start(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                raise PrFlowError("PR flow already running", status_code=409)
            if not self._config.get("enabled", True):
                raise PrFlowError("PR flow disabled by config", status_code=409)
            self._acquire_lock()
            thread_started = False
            try:
                state = self._initialize_state(payload=payload)
                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self._run_flow, args=(state["id"],), daemon=True
                )
                self._thread.start()
                thread_started = True
                return state
            finally:
                if not thread_started:
                    self._release_lock()

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        state = self._load_state()
        if state.get("status") == "running":
            state["status"] = "stopping"
            state["updated_at"] = now_iso()
            self._save_state(state)
        return state

    def resume(self) -> dict[str, Any]:
        with self._thread_lock:
            state = self._load_state()
            if state.get("status") not in ("stopped", "failed", "idle", "stopping"):
                raise PrFlowError("PR flow cannot be resumed in the current state")
            if self._thread and self._thread.is_alive():
                raise PrFlowError("PR flow already running", status_code=409)
            self._acquire_lock()
            thread_started = False
            try:
                self._stop_event.clear()
                state["status"] = "running"
                state["updated_at"] = now_iso()
                state["last_error"] = None
                self._save_state(state)
                self._thread = threading.Thread(
                    target=self._run_flow, args=(state["id"],), daemon=True
                )
                self._thread.start()
                thread_started = True
                return state
            finally:
                if not thread_started:
                    self._release_lock()

    def collect_reviews(self) -> dict[str, Any]:
        state = self._load_state()
        if not state.get("worktree_path"):
            raise PrFlowError("PR flow has no active worktree")
        summary, bundle_path, _review_data = self._collect_reviews(state)
        state["review_summary"] = _format_review_summary(summary)
        state["review_bundle_path"] = bundle_path
        state["updated_at"] = now_iso()
        self._save_state(state)
        return state

    def chatops_config(self) -> dict[str, Any]:
        return self._config.get("chatops", {})

    def _load_engine(self, repo_root: Optional[Path] = None) -> Engine:
        root = repo_root or self.repo_root
        return Engine(root)

    def _log_line(self, state: dict[str, Any], message: str) -> None:
        workflow_dir = self._workflow_dir(state)
        workflow_dir.mkdir(parents=True, exist_ok=True)
        log_path = workflow_dir / "workflow.log"
        line = f"[{now_iso()}] {message}\n"
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            return
        state["workflow_log_path"] = log_path.as_posix()
        state["updated_at"] = now_iso()
        self._save_state(state)

    def _load_state(self) -> dict[str, Any]:
        state = read_json(self._state_path) or {}
        if not isinstance(state, dict):
            state = {}
        base = _default_state()
        base.update(state)
        return base

    def _save_state(self, state: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._state_path, json.dumps(state, indent=2) + "\n")

    def _initialize_state(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        mode = str(payload.get("mode") or "issue").strip().lower()
        issue = payload.get("issue")
        pr = payload.get("pr")
        if mode not in ("issue", "pr"):
            raise PrFlowError("mode must be 'issue' or 'pr'")
        if mode == "issue" and not issue:
            raise PrFlowError("issue is required for issue mode")
        if mode == "pr" and not pr:
            raise PrFlowError("pr is required for pr mode")
        workflow_id = uuid.uuid4().hex
        state = _default_state()
        state.update(
            {
                "id": workflow_id,
                "status": "running",
                "mode": mode,
                "step": "preflight",
                "issue": issue,
                "pr": pr,
                "draft": payload.get("draft"),
                "base_branch": payload.get("base_branch"),
                "stop_condition": _normalize_stop_condition(
                    payload.get("stop_condition")
                ),
                "max_cycles": payload.get("max_cycles"),
                "max_implementation_runs": payload.get("max_implementation_runs"),
                "max_wallclock_seconds": payload.get("max_wallclock_seconds"),
                "started_at": now_iso(),
                "updated_at": now_iso(),
                "finished_at": None,
                "last_error": None,
            }
        )
        state["workflow_log_path"] = (
            _workflow_root(self.repo_root) / workflow_id / "workflow.log"
        ).as_posix()
        self._save_state(state)
        return state

    def _workflow_dir(self, state: dict[str, Any]) -> Path:
        workflow_id = state.get("id") or "current"
        return _workflow_root(self.repo_root) / str(workflow_id)

    def _acquire_lock(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(self._lock_path)
        try:
            lock.acquire(blocking=False)
        except FileLockBusy as exc:
            raise PrFlowError("PR flow lock already held", status_code=409) from exc
        except FileLockError as exc:
            raise PrFlowError(str(exc)) from exc
        self._lock_handle = lock

    def _release_lock(self) -> None:
        if self._lock_handle is not None:
            try:
                self._lock_handle.release()
            except Exception:
                pass
        self._lock_handle = None

    def _should_stop(self) -> bool:
        return self._stop_event.is_set()

    def _run_flow(self, workflow_id: str) -> None:
        state = self._load_state()
        if state.get("id") != workflow_id:
            return
        try:
            self._log_line(state, "PR flow starting.")
            self._execute_flow(state)
            if state.get("status") == "running":
                state["status"] = "completed"
                state["finished_at"] = now_iso()
                self._log_line(state, "PR flow completed.")
        except Exception as exc:
            state["status"] = "failed"
            state["last_error"] = str(exc)
            state["finished_at"] = now_iso()
            self._log_line(state, f"PR flow failed: {exc}")
        finally:
            state["updated_at"] = now_iso()
            self._save_state(state)
            self._release_lock()

    def _execute_flow(self, state: dict[str, Any]) -> None:
        steps = [
            "preflight",
            "resolve_base",
            "link",
            "create_worktree",
            "spec",
            "ingest",
            "implement",
            "sync_pr",
            "review_loop",
        ]
        start_step = state.get("step")
        if start_step in steps:
            start_index = steps.index(start_step)
        else:
            start_index = 0
        for idx in range(start_index, len(steps)):
            step = steps[idx]
            state["step"] = step
            state["status"] = "running"
            state["updated_at"] = now_iso()
            self._save_state(state)
            if self._should_stop():
                self._mark_stopped(state)
                return
            if step == "preflight":
                self._preflight(state)
            elif step == "resolve_base":
                self._resolve_base(state)
            elif step == "link":
                self._link_issue_or_pr(state)
            elif step == "create_worktree":
                self._create_worktree(state)
            elif step == "spec":
                if state.get("mode") == "issue":
                    self._generate_spec(state)
            elif step == "ingest":
                if state.get("mode") == "issue":
                    self._ingest_spec(state)
            elif step == "implement":
                self._run_implementation(state)
            elif step == "sync_pr":
                self._sync_pr(state)
            elif step == "review_loop":
                self._review_loop(state)
            if self._should_stop():
                self._mark_stopped(state)
                return
        if state.get("status") == "running":
            state["status"] = "completed"
            state["finished_at"] = now_iso()
            self._save_state(state)

    def _mark_stopped(self, state: dict[str, Any]) -> None:
        state["status"] = "stopped"
        state["updated_at"] = now_iso()
        state["finished_at"] = now_iso()
        self._save_state(state)

    def _preflight(self, state: dict[str, Any]) -> None:
        engine = self._load_engine()
        gh = GitHubService(engine.repo_root, raw_config=engine.config.raw)
        if not gh.gh_available():
            raise PrFlowError("GitHub CLI (gh) not available", status_code=500)
        if not gh.gh_authenticated():
            raise PrFlowError(
                "GitHub CLI not authenticated (run `gh auth login`)",
                status_code=401,
            )
        if engine.runner_pid():
            raise PrFlowError("Autorunner is active; stop it before starting PR flow")
        if state.get("mode") == "issue" and not gh.is_clean():
            raise PrFlowError(
                "Working tree has uncommitted changes; clean it before starting PR flow"
            )
        self._log_line(state, "Preflight ok.")

    def _resolve_base(self, state: dict[str, Any]) -> None:
        engine = self._load_engine()
        gh = GitHubService(engine.repo_root, raw_config=engine.config.raw)
        repo = gh.repo_info()
        base_override = (state.get("base_branch") or "").strip()
        if state.get("mode") == "pr" and not base_override:
            return
        base = base_override or repo.default_branch or "main"
        state["base_branch"] = base
        self._save_state(state)
        self._log_line(state, f"Base branch resolved: {base}")

    def _link_issue_or_pr(self, state: dict[str, Any]) -> None:
        engine = self._load_engine()
        gh = GitHubService(engine.repo_root, raw_config=engine.config.raw)
        mode = state.get("mode")
        if mode == "issue":
            issue_ref = str(state.get("issue") or "")
            link_state = gh.link_issue(issue_ref)
            issue = link_state.get("issue") or {}
            state["issue_number"] = issue.get("number")
            state["issue_title"] = issue.get("title")
            state["issue_url"] = issue.get("url")
            state["updated_at"] = now_iso()
            self._save_state(state)
            return
        if mode == "pr":
            pr_ref = str(state.get("pr") or "")
            pr_number, pr_url, head_ref, base_ref = self._resolve_pr_input(gh, pr_ref)
            state["pr_number"] = pr_number
            state["pr_url"] = pr_url
            if head_ref:
                state["head_branch"] = head_ref
            if base_ref and not state.get("base_branch"):
                state["base_branch"] = base_ref
            state["updated_at"] = now_iso()
            self._save_state(state)

    def _resolve_pr_input(
        self, gh: GitHubService, pr_ref: str
    ) -> tuple[int, Optional[str], Optional[str], Optional[str]]:
        raw = (pr_ref or "").strip()
        if raw.startswith("#"):
            raw = raw[1:].strip()
        if raw.isdigit():
            number = int(raw)
        else:
            slug, number = parse_pr_input(raw)
            repo = gh.repo_info()
            if slug and slug.lower() != repo.name_with_owner.lower():
                raise PrFlowError(
                    f"PR must be in this repo ({repo.name_with_owner}); got {slug}"
                )
        pr_obj = gh.pr_view(number=number)
        return (
            int(pr_obj.get("number") or number),
            pr_obj.get("url"),
            pr_obj.get("headRefName"),
            pr_obj.get("baseRefName"),
        )

    def _resolve_base_repo(self, hub: HubSupervisor) -> ManifestRepo:
        manifest = load_manifest(hub.hub_config.manifest_path, hub.hub_config.root)
        target = self.repo_root.resolve()
        for repo in manifest.repos:
            repo_path = (hub.hub_config.root / repo.path).resolve()
            if repo_path == target:
                if repo.kind == "worktree" and repo.worktree_of:
                    base = manifest.get(repo.worktree_of)
                    if base:
                        return base
                return repo
        raise PrFlowError("Unable to resolve base repo for worktree creation")

    def _ensure_hub(self) -> HubSupervisor:
        if self._hub_root is not None:
            return HubSupervisor.from_path(self._hub_root)
        try:
            return HubSupervisor.from_path(self.repo_root)
        except (ConfigError, ValueError) as exc:
            raise PrFlowError(
                "Hub config not found; PR flow requires hub worktrees"
            ) from exc

    def _create_worktree(self, state: dict[str, Any]) -> None:
        if state.get("worktree_path"):
            return
        hub = self._ensure_hub()
        base_repo = self._resolve_base_repo(hub)
        base_repo_path = (hub.hub_config.root / base_repo.path).resolve()
        mode = state.get("mode")
        base_branch = state.get("base_branch") or "main"
        branch = None
        start_point = f"origin/{base_branch}"
        if mode == "issue":
            issue_number = int(state.get("issue_number") or 0)
            slug = _slugify(state.get("issue_title") or "")
            branch = f"car/issue-{issue_number}-{slug}"
        elif mode == "pr":
            branch = state.get("head_branch") or ""
            if branch:
                start_point = f"origin/{branch}"
            else:
                pr_number = int(state.get("pr_number") or 0)
                branch = f"car/pr-{pr_number}-fix"
        if not branch:
            raise PrFlowError("Unable to determine branch name for worktree")
        if mode == "pr" and state.get("pr_number"):
            self._ensure_pr_head_available(
                base_repo_path,
                pr_number=int(state.get("pr_number") or 0),
                branch=branch,
            )
        snapshot = hub.create_worktree(
            base_repo_id=base_repo.id,
            branch=branch,
            force=False,
            start_point=start_point,
        )
        state["worktree_repo_id"] = snapshot.id
        state["worktree_path"] = snapshot.path.as_posix()
        state["head_branch"] = branch
        state["updated_at"] = now_iso()
        self._save_state(state)
        self._log_line(state, f"Worktree created: {snapshot.path}")
        worktree_root = snapshot.path
        state["final_report_path"] = (
            worktree_root / ".codex-autorunner" / "SUMMARY.md"
        ).as_posix()
        self._save_state(state)
        if state.get("mode") == "issue" and state.get("issue"):
            engine = self._load_engine(worktree_root)
            gh = GitHubService(worktree_root, raw_config=engine.config.raw)
            gh.link_issue(str(state.get("issue")))

    def _ensure_pr_head_available(
        self,
        base_repo_path: Path,
        *,
        pr_number: int,
        branch: str,
    ) -> None:
        engine = self._load_engine(base_repo_path)
        gh = GitHubService(base_repo_path, raw_config=engine.config.raw)
        try:
            gh.ensure_pr_head(number=int(pr_number), branch=branch, cwd=base_repo_path)
        except Exception as exc:
            raise PrFlowError(f"Unable to fetch PR head: {exc}") from exc

    def _generate_spec(self, state: dict[str, Any]) -> None:
        if self._app_server_supervisor is None:
            raise PrFlowError("App-server backend is not configured")
        worktree_root = self._require_worktree_root(state)
        engine = self._load_engine(worktree_root)
        gh = GitHubService(worktree_root, raw_config=engine.config.raw)
        prompt, _link_state = gh.build_spec_prompt_from_issue(str(state.get("issue")))
        doc_chat = DocChatService(
            engine,
            app_server_supervisor=self._app_server_supervisor,
            app_server_events=None,
            opencode_supervisor=self._opencode_supervisor,
        )

        async def _run() -> dict:
            req = doc_chat.parse_request(
                {"message": prompt, "stream": False}, kind="spec"
            )
            async with doc_chat.doc_lock():
                return await doc_chat.execute(req)

        result = asyncio.run(_run())
        if result.get("status") != "ok":
            detail = result.get("detail") or "SPEC generation failed"
            raise PrFlowError(detail)
        self._log_line(state, "SPEC generated from issue.")

    def _ingest_spec(self, state: dict[str, Any]) -> None:
        if self._app_server_supervisor is None:
            raise PrFlowError("App-server backend is not configured")
        worktree_root = self._require_worktree_root(state)
        engine = self._load_engine(worktree_root)
        ingest = SpecIngestService(
            engine,
            app_server_supervisor=self._app_server_supervisor,
            opencode_supervisor=self._opencode_supervisor,
        )

        async def _run() -> dict:
            await ingest.execute(force=True, spec_path=None, message=None)
            return ingest.apply_patch()

        try:
            asyncio.run(_run())
        except SpecIngestError as exc:
            raise PrFlowError(str(exc)) from exc
        self._log_line(state, "SPEC ingested into TODO/PROGRESS/OPINIONS.")

    def _run_implementation(self, state: dict[str, Any]) -> None:
        worktree_root = self._require_worktree_root(state)
        engine = self._load_engine(worktree_root)
        max_runs = state.get("max_implementation_runs")
        if max_runs is None:
            max_runs = self._config.get("max_implementation_runs")
        try:
            if max_runs is not None and int(max_runs) <= 0:
                max_runs = None
        except (TypeError, ValueError):
            max_runs = None
        max_wallclock = state.get("max_wallclock_seconds")
        if max_wallclock is None:
            max_wallclock = self._config.get("max_wallclock_seconds")
        try:
            engine.acquire_lock(force=False)
        except LockError as exc:
            raise PrFlowError(str(exc)) from exc
        prev_wallclock = engine.config.runner_max_wallclock_seconds
        if max_wallclock is not None:
            engine.config.runner_max_wallclock_seconds = int(max_wallclock)
        try:
            engine.clear_stop_request()
            engine.run_loop(
                stop_after_runs=int(max_runs) if max_runs is not None else None,
                external_stop_flag=self._stop_event,
            )
        finally:
            engine.config.runner_max_wallclock_seconds = prev_wallclock
            engine.release_lock()
        self._log_line(state, "Implementation loop completed.")

    def _sync_pr(self, state: dict[str, Any]) -> None:
        worktree_root = self._require_worktree_root(state)
        engine = self._load_engine(worktree_root)
        gh = GitHubService(worktree_root, raw_config=engine.config.raw)
        draft = state.get("draft")
        if draft is None:
            draft = bool(
                (engine.config.raw.get("github") or {}).get("pr_draft_default", True)
            )
        result = gh.sync_pr(draft=bool(draft))
        pr = result.get("pr") if isinstance(result, dict) else None
        if isinstance(pr, dict):
            state["pr_number"] = pr.get("number")
            state["pr_url"] = pr.get("url")
        state["updated_at"] = now_iso()
        self._save_state(state)
        self._log_line(state, "PR synced.")

    def _review_loop(self, state: dict[str, Any]) -> None:
        max_cycles = state.get("max_cycles")
        if max_cycles is None:
            max_cycles = self._config.get("max_cycles", 1)
        try:
            max_cycles = max(1, int(max_cycles))
        except (TypeError, ValueError):
            max_cycles = 1
        stop_condition = _normalize_stop_condition(
            state.get("stop_condition")
            or self._config.get("stop_condition", "no_issues")
        )
        cycle = int(state.get("cycle") or 0)
        while cycle < int(max_cycles):
            if self._should_stop():
                self._mark_stopped(state)
                return
            cycle += 1
            state["cycle"] = cycle
            state["updated_at"] = now_iso()
            self._save_state(state)
            summary, bundle_path, review_data = self._collect_reviews(state)
            state["review_summary"] = _format_review_summary(summary)
            state["review_bundle_path"] = bundle_path
            state["updated_at"] = now_iso()
            self._save_state(state)
            if summary.total == 0:
                self._log_line(state, "No review issues found.")
                return
            if stop_condition == "minor_only" and summary.major == 0:
                self._log_line(state, "Only minor issues remain; stopping.")
                return
            if cycle >= int(max_cycles):
                self._log_line(state, "Max review cycles reached.")
                return
            self._apply_review_to_todo(state, bundle_path, summary, review_data)
            self._run_implementation(state)
            self._sync_pr(state)

    def _collect_reviews(
        self, state: dict[str, Any]
    ) -> tuple[PrFlowReviewSummary, Optional[str], dict[str, Any]]:
        worktree_root = self._require_worktree_root(state)
        engine = self._load_engine(worktree_root)
        gh = GitHubService(worktree_root, raw_config=engine.config.raw)
        repo = gh.repo_info()
        owner, repo_name = repo.name_with_owner.split("/", 1)
        pr_number = state.get("pr_number")
        if not pr_number:
            raise PrFlowError("PR number not available for review collection")
        threads = []
        if self._config.get("review", {}).get("include_github", True):
            threads = gh.pr_review_threads(
                owner=owner, repo=repo_name, number=int(pr_number)
            )
        checks = []
        if self._config.get("review", {}).get("include_checks", True):
            checks = gh.pr_checks(number=int(pr_number))
        codex_review = None
        if self._config.get("review", {}).get("include_codex", True):
            codex_review = self._run_codex_review(worktree_root, state)
        summary, lines = self._format_review_bundle(
            state, threads=threads, checks=checks, codex_review=codex_review
        )
        review_snapshot_index = int(state.get("review_snapshot_index") or 0) + 1
        state["review_snapshot_index"] = review_snapshot_index
        state["updated_at"] = now_iso()
        self._save_state(state)
        workflow_dir = self._workflow_dir(state)
        workflow_dir.mkdir(parents=True, exist_ok=True)
        filename = f"review_bundle_snapshot_{review_snapshot_index}.md"
        bundle_path = workflow_dir / filename
        atomic_write(bundle_path, "\n".join(lines).rstrip() + "\n")
        self._log_line(state, f"Review bundle written: {bundle_path}")
        worktree_context_dir = worktree_root / ".codex-autorunner" / "contexts"
        worktree_context_dir.mkdir(parents=True, exist_ok=True)
        worktree_bundle_path = worktree_context_dir / f"pr_{filename}"
        atomic_write(worktree_bundle_path, "\n".join(lines).rstrip() + "\n")
        self._log_line(
            state, f"Review bundle written to worktree: {worktree_bundle_path}"
        )
        review_data = {
            "threads": threads,
            "checks": checks,
            "codex_review": codex_review,
        }
        return summary, worktree_bundle_path.as_posix(), review_data

    def _format_review_bundle(
        self,
        state: dict[str, Any],
        *,
        threads: list[dict[str, Any]],
        checks: list[dict[str, Any]],
        codex_review: Optional[str],
    ) -> tuple[PrFlowReviewSummary, list[str]]:
        major = 0
        minor = 0
        resolved = 0
        items: list[str] = []
        if threads:
            items.append("## GitHub Review Threads")
            thread_idx = 0
            for thread in threads:
                if not isinstance(thread, dict):
                    continue
                comments = thread.get("comments")
                if not isinstance(comments, list):
                    continue
                thread_idx += 1
                status = "resolved" if thread.get("isResolved") else "unresolved"
                items.append(f"- Thread {thread_idx} ({status})")
                if status == "resolved":
                    resolved += 1
                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    body = comment.get("body") or ""
                    severity = _classify_review_text(body)
                    if status != "resolved":
                        if severity == "minor":
                            minor += 1
                        else:
                            major += 1
                    author = comment.get("author") or {}
                    author_name = (
                        author.get("login")
                        if isinstance(author, dict)
                        else str(author or "unknown")
                    )
                    location = comment.get("path") or "(unknown file)"
                    line = comment.get("line")
                    if isinstance(line, int):
                        location = f"{location}:{line}"
                    snippet = _safe_text(body, 200)
                    items.append(
                        f"  - [{severity}] {location} by {author_name}: {snippet}"
                    )
            items.append("")
        if checks:
            items.append("## CI Checks")
            for check in checks:
                name = check.get("name") or "check"
                status = check.get("status") or "unknown"
                conclusion = check.get("conclusion") or "unknown"
                line = f"- {name}: {status} ({conclusion})"
                url = check.get("details_url")
                if url:
                    line = f"{line} - {url}"
                items.append(line)
                if conclusion in (
                    "failure",
                    "cancelled",
                    "timed_out",
                    "action_required",
                ):
                    major += 1
            items.append("")
        if codex_review:
            items.append("## Codex Review")
            for raw_line in codex_review.splitlines():
                text = raw_line.strip()
                if not text:
                    continue
                severity = _classify_review_text(text)
                if severity == "minor":
                    minor += 1
                else:
                    major += 1
                items.append(f"- [{severity}] {text}")
            items.append("")
        total = major + minor
        summary = PrFlowReviewSummary(
            total=total, major=major, minor=minor, resolved=resolved
        )
        lines = [
            "# PR Flow Review Bundle",
            f"Workflow: {state.get('id')}",
            f"Cycle: {state.get('cycle')}",
            f"PR: {state.get('pr_url') or state.get('pr_number')}",
            "",
            "## Summary",
            f"- Total issues: {summary.total}",
            f"- Major: {summary.major}",
            f"- Minor: {summary.minor}",
            f"- Resolved threads: {summary.resolved}",
            "",
        ]
        lines.extend(items)
        return summary, lines

    def _apply_review_to_todo(
        self,
        state: dict[str, Any],
        bundle_path: Optional[str],
        summary: PrFlowReviewSummary,
        review_data: dict[str, Any],
    ) -> None:
        worktree_root = self._require_worktree_root(state)
        engine = self._load_engine(worktree_root)
        todo_path = engine.config.doc_path("todo")
        existing = todo_path.read_text(encoding="utf-8") if todo_path.exists() else ""

        severity_threshold = self._config.get("review", {}).get(
            "severity_threshold", "minor"
        )

        items: list[str] = []

        threads = review_data.get("threads", [])
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            if thread.get("isResolved"):
                continue
            comments = thread.get("comments")
            if not isinstance(comments, list):
                continue
            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                body = comment.get("body") or ""
                severity = _classify_review_text(body)

                if severity_threshold == "major" and severity == "minor":
                    continue

                author = comment.get("author") or {}
                author_name = (
                    author.get("login")
                    if isinstance(author, dict)
                    else str(author or "unknown")
                )
                location = comment.get("path") or "(unknown file)"
                line = comment.get("line")
                if isinstance(line, int):
                    location = f"{location}:{line}"
                snippet = _normalize_review_snippet(body, 100)
                items.append(
                    f"- [ ] Address review: {location} {snippet} ({author_name})"
                )

        checks = review_data.get("checks", [])
        for check in checks:
            if not isinstance(check, dict):
                continue
            name = check.get("name") or "check"
            conclusion = check.get("conclusion") or "unknown"

            if conclusion not in (
                "failure",
                "cancelled",
                "timed_out",
                "action_required",
            ):
                continue

            severity = "major"
            if severity_threshold == "major" and severity == "minor":
                continue

            details_url = check.get("details_url") or ""
            url_suffix = f" {details_url}" if details_url else ""
            items.append(f"- [ ] Fix failing check: {name} ({conclusion}){url_suffix}")

        codex_review = review_data.get("codex_review")
        if codex_review:
            for raw_line in codex_review.splitlines():
                text = raw_line.strip()
                if not text:
                    continue
                severity = _classify_review_text(text)

                if severity_threshold == "major" and severity == "minor":
                    continue

                items.append(f"- [ ] Address Codex review: {text}")

        header = f"## Review Feedback Cycle {state.get('cycle')}"
        note = f"- Summary: {summary.total} issues ({summary.major} major, {summary.minor} minor)"
        bundle_line = (
            f"- Review bundle: {bundle_path}"
            if bundle_path
            else "- Review bundle: (missing)"
        )
        lines = [header, note, bundle_line]
        if items:
            lines.extend(items)

        block = "\n".join(lines) + "\n"
        new_text = f"{block}{existing}" if existing else block
        atomic_write(todo_path, new_text)
        self._log_line(state, "Appended review feedback to TODO.")

    def _run_codex_review(
        self, worktree_root: Path, state: dict[str, Any]
    ) -> Optional[str]:
        if self._app_server_supervisor is None:
            return None
        try:
            base_branch = state.get("base_branch") or "main"
            target = {"type": "baseBranch", "branch": base_branch}

            async def _run() -> str:
                client = await self._app_server_supervisor.get_client(worktree_root)
                turn = await client.review_start(
                    thread_id=uuid.uuid4().hex,
                    target=target,
                    delivery="inline",
                    cwd=str(worktree_root),
                )
                result = await turn.wait()
                if not result.agent_messages:
                    return ""
                return "\n\n".join(result.agent_messages).strip()

            review_text = asyncio.run(_run())
            if review_text:
                self._log_line(state, "Codex review completed.")
            return review_text or None
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "pr_flow.codex_review.failed",
                exc=exc,
            )
            return None

    def _require_worktree_root(self, state: dict[str, Any]) -> Path:
        worktree_path = state.get("worktree_path")
        if not worktree_path:
            raise PrFlowError("Worktree not available")
        hub = self._ensure_hub()
        root = (hub.hub_config.root / worktree_path).resolve()
        if not root.exists():
            raise PrFlowError(f"Worktree path missing: {root}")
        return root
