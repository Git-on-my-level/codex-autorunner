from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from ...core.pma_thread_store import PmaThreadStore
from ...core.pr_bindings import PrBinding, PrBindingStore
from ...core.scm_events import ScmEventStore
from ...core.scm_polling_watches import ScmPollingWatch, ScmPollingWatchStore
from ...core.scm_reaction_types import ScmReactionConfig
from ...core.text_utils import _mapping, _normalize_text
from ...core.time_utils import now_iso
from ...manifest import ManifestError, load_manifest

_FAILED_CHECK_CONCLUSIONS = frozenset(
    {"action_required", "cancelled", "failure", "startup_failure", "stale", "timed_out"}
)
_ACTIVE_PR_STATES = frozenset({"open", "draft"})
_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .service import GitHubService


GitHubServiceFactory = Callable[[Path, Optional[dict[str, Any]]], "GitHubService"]


def _normalize_lower_text(value: Any) -> Optional[str]:
    text = _normalize_text(value)
    return text.lower() if text is not None else None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_after_seconds(seconds: int) -> str:
    return (_utc_now() + timedelta(seconds=max(0, int(seconds)))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reaction_config_mapping(value: Any) -> dict[str, Any]:
    return ScmReactionConfig.from_mapping(value).to_dict()


def _github_automation_config(raw_config: object) -> Mapping[str, Any]:
    github = _mapping(raw_config).get("github")
    return _mapping(_mapping(github).get("automation"))


@dataclass(frozen=True)
class GitHubPollingConfig:
    enabled: bool = False
    watch_window_minutes: int = 30
    interval_seconds: int = 90

    @classmethod
    def from_mapping(cls, raw_config: object) -> "GitHubPollingConfig":
        github = _mapping(raw_config).get("github")
        automation = _mapping(github).get("automation")
        polling = _mapping(_mapping(automation).get("polling"))
        enabled = polling.get("enabled")
        watch_window_minutes = polling.get("watch_window_minutes")
        interval_seconds = polling.get("interval_seconds")
        return cls(
            enabled=bool(enabled) if isinstance(enabled, bool) else False,
            watch_window_minutes=(
                int(watch_window_minutes)
                if isinstance(watch_window_minutes, int) and watch_window_minutes > 0
                else 30
            ),
            interval_seconds=(
                int(interval_seconds)
                if isinstance(interval_seconds, int) and interval_seconds > 0
                else 90
            ),
        )


def _reaction_state_from_pr(pr: Mapping[str, Any]) -> str:
    state = _normalize_lower_text(pr.get("state"))
    is_draft = bool(pr.get("isDraft"))
    if state == "open":
        return "draft" if is_draft else "open"
    return state or "closed"


def _review_key(review: Mapping[str, Any]) -> str:
    review_id = _normalize_text(review.get("review_id"))
    if review_id is not None:
        return review_id
    submitted_at = _normalize_text(review.get("submitted_at")) or "-"
    author_login = _normalize_text(review.get("author_login")) or "-"
    body = _normalize_text(review.get("body")) or "-"
    return f"{submitted_at}:{author_login}:{body}"


def _check_key(check: Mapping[str, Any]) -> str:
    name = _normalize_text(check.get("name")) or "-"
    conclusion = _normalize_lower_text(check.get("conclusion")) or "-"
    head_sha = _normalize_text(check.get("head_sha")) or "-"
    details_url = _normalize_text(check.get("details_url")) or "-"
    return f"{head_sha}:{name}:{conclusion}:{details_url}"


def _comment_timestamp(comment: Mapping[str, Any]) -> Optional[str]:
    for key in ("updated_at", "updatedAt", "created_at", "createdAt"):
        timestamp = _normalize_text(comment.get(key))
        if timestamp is not None:
            return timestamp
    return None


def _comment_key(comment: Mapping[str, Any]) -> str:
    comment_id = _normalize_text(comment.get("comment_id"))
    if comment_id is not None:
        return comment_id
    timestamp = _comment_timestamp(comment) or "-"
    author_login = _normalize_text(comment.get("author_login")) or "-"
    body = _normalize_text(comment.get("body")) or "-"
    path = _normalize_text(comment.get("path")) or "-"
    line = comment.get("line") if isinstance(comment.get("line"), int) else "-"
    return f"{timestamp}:{author_login}:{path}:{line}:{body}"


def _snapshot_map(snapshot: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = snapshot.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _build_snapshot(
    *,
    binding: PrBinding,
    service: GitHubService,
) -> dict[str, Any]:
    pr = service.pr_view(number=binding.pr_number, repo_slug=binding.repo_slug)
    head_sha = _normalize_text(pr.get("headRefOid"))
    pr_state = _reaction_state_from_pr(pr)
    pr_author = pr.get("author")
    pr_author_login = (
        _normalize_text(pr_author.get("login"))
        if isinstance(pr_author, Mapping)
        else None
    )
    owner, repo = binding.repo_slug.split("/", 1)
    reviews = service.pr_reviews(owner=owner, repo=repo, number=binding.pr_number)
    checks = service.pr_checks(number=binding.pr_number)
    issue_comments = service.issue_comments(
        owner=owner,
        repo=repo,
        number=binding.pr_number,
    )
    review_threads = service.pr_review_threads(
        owner=owner,
        repo=repo,
        number=binding.pr_number,
    )

    changes_requested_reviews: dict[str, Any] = {}
    for review in reviews:
        if _normalize_lower_text(review.get("review_state")) != "changes_requested":
            continue
        payload = {
            "action": "submitted",
            "review_id": review.get("review_id"),
            "review_state": review.get("review_state"),
            "body": review.get("body"),
            "html_url": review.get("html_url"),
            "author_login": review.get("author_login"),
            "commit_id": review.get("commit_id"),
            "submitted_at": review.get("submitted_at"),
        }
        changes_requested_reviews[_review_key(review)] = {
            key: value for key, value in payload.items() if value is not None
        }

    failed_checks: dict[str, Any] = {}
    for check in checks:
        status = _normalize_lower_text(check.get("status"))
        conclusion = _normalize_lower_text(check.get("conclusion"))
        if status != "completed" or conclusion not in _FAILED_CHECK_CONCLUSIONS:
            continue
        payload = {
            "action": "completed",
            "name": _normalize_text(check.get("name")),
            "status": status,
            "conclusion": conclusion,
            "details_url": _normalize_text(check.get("details_url")),
            "head_sha": head_sha,
        }
        failed_checks[_check_key(payload)] = {
            key: value for key, value in payload.items() if value is not None
        }

    current_issue_comments: dict[str, Any] = {}
    for comment in issue_comments:
        payload = {
            "action": "created",
            "comment_id": _normalize_text(comment.get("comment_id")),
            "body": _normalize_text(comment.get("body")),
            "html_url": _normalize_text(comment.get("html_url")),
            "author_login": _normalize_text(comment.get("author_login")),
            "author_type": _normalize_text(comment.get("author_type")),
            "author_association": _normalize_text(comment.get("author_association")),
            "issue_number": binding.pr_number,
            "issue_author_login": pr_author_login,
            "line": (
                comment.get("line") if isinstance(comment.get("line"), int) else None
            ),
            "path": _normalize_text(comment.get("path")),
            "pull_request_review_id": _normalize_text(
                comment.get("pull_request_review_id")
            ),
            "commit_id": _normalize_text(comment.get("commit_id")),
            "updated_at": _comment_timestamp(comment),
        }
        current_issue_comments[_comment_key(payload)] = {
            key: value for key, value in payload.items() if value is not None
        }

    current_review_thread_comments: dict[str, Any] = {}
    for thread in review_threads:
        comments = thread.get("comments")
        if not isinstance(comments, list):
            continue
        thread_resolved = bool(thread.get("isResolved"))
        for comment in comments:
            if not isinstance(comment, Mapping):
                continue
            payload = {
                "action": "created",
                "comment_id": _normalize_text(comment.get("comment_id")),
                "body": _normalize_text(comment.get("body")),
                "html_url": _normalize_text(comment.get("html_url")),
                "author_login": _normalize_text(comment.get("author_login")),
                "author_type": _normalize_text(comment.get("author_type")),
                "author_association": _normalize_text(
                    comment.get("author_association")
                ),
                "issue_number": binding.pr_number,
                "issue_author_login": pr_author_login,
                "thread_resolved": thread_resolved,
                "line": (
                    comment.get("line")
                    if isinstance(comment.get("line"), int)
                    else None
                ),
                "path": _normalize_text(comment.get("path")),
                "updated_at": _comment_timestamp(comment),
            }
            current_review_thread_comments[_comment_key(payload)] = {
                key: value for key, value in payload.items() if value is not None
            }

    snapshot: dict[str, Any] = {
        "pr_state": pr_state,
        "changes_requested_reviews": changes_requested_reviews,
        "failed_checks": failed_checks,
        "issue_comments": current_issue_comments,
        "review_thread_comments": current_review_thread_comments,
    }
    if head_sha is not None:
        snapshot["head_sha"] = head_sha
    return snapshot


class GitHubScmPollingService:
    def __init__(
        self,
        hub_root: Path,
        *,
        raw_config: Optional[dict[str, Any]] = None,
        github_service_factory: Optional[GitHubServiceFactory] = None,
        watch_store: Optional[ScmPollingWatchStore] = None,
        event_store: Optional[ScmEventStore] = None,
    ) -> None:
        self._hub_root = Path(hub_root)
        self._raw_config = raw_config or {}
        if github_service_factory is None:
            from .service import GitHubService

            github_service_factory = GitHubService
        self._github_service_factory = github_service_factory
        self._watch_store = watch_store or ScmPollingWatchStore(self._hub_root)
        self._event_store = event_store or ScmEventStore(self._hub_root)

    def arm_watch(
        self,
        *,
        binding: PrBinding,
        workspace_root: Path,
        reaction_config: Optional[Mapping[str, Any]] = None,
    ) -> Optional[ScmPollingWatch]:
        polling_config = GitHubPollingConfig.from_mapping(self._raw_config)
        if not polling_config.enabled or binding.pr_state not in _ACTIVE_PR_STATES:
            return None

        now_timestamp = now_iso()
        expires_at = _iso_after_seconds(polling_config.watch_window_minutes * 60)
        next_poll_at = _iso_after_seconds(polling_config.interval_seconds)
        snapshot: dict[str, Any] = {"baseline_pending": True}
        try:
            github = self._github_service_factory(
                workspace_root,
                self._raw_config if isinstance(self._raw_config, dict) else None,
            )
            snapshot = _build_snapshot(binding=binding, service=github)
        except Exception:
            _LOGGER.warning(
                "Failed establishing SCM polling baseline for %s#%s",
                binding.repo_slug,
                binding.pr_number,
                exc_info=True,
            )
            next_poll_at = now_timestamp

        return self._watch_store.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug=binding.repo_slug,
            repo_id=binding.repo_id,
            pr_number=binding.pr_number,
            workspace_root=str(workspace_root.resolve()),
            thread_target_id=binding.thread_target_id,
            poll_interval_seconds=polling_config.interval_seconds,
            next_poll_at=next_poll_at,
            expires_at=expires_at,
            reaction_config=_reaction_config_mapping(
                _github_automation_config(reaction_config or self._raw_config)
            ),
            snapshot=snapshot,
        )

    def discover_and_arm_missing_watches(self, *, limit: int = 20) -> dict[str, int]:
        counts = {
            "candidate_workspaces": 0,
            "bindings_discovered": 0,
            "watches_armed": 0,
            "discovery_errors": 0,
        }
        polling_config = GitHubPollingConfig.from_mapping(self._raw_config)
        if not polling_config.enabled:
            return counts

        candidate_roots, workspaces_by_repo_id, workspaces_by_thread_id = (
            self._candidate_workspace_roots()
        )
        counts["candidate_workspaces"] = len(candidate_roots)

        active_bindings = self._active_bindings(limit=max(100, limit * 10))
        for workspace_root in candidate_roots:
            try:
                github = self._github_service_factory(
                    workspace_root,
                    self._raw_config if isinstance(self._raw_config, dict) else None,
                )
                binding = github.discover_pr_binding(cwd=workspace_root)
            except Exception:
                _LOGGER.warning(
                    "Failed discovering polling binding for workspace %s",
                    workspace_root,
                    exc_info=True,
                )
                counts["discovery_errors"] += 1
                continue
            if binding is None or binding.pr_state not in _ACTIVE_PR_STATES:
                continue
            if binding.binding_id not in active_bindings:
                counts["bindings_discovered"] += 1
            active_bindings[binding.binding_id] = binding

        repo_slug_cache: dict[str, Optional[str]] = {}
        for binding in active_bindings.values():
            watch = self._watch_store.get_watch(
                provider="github",
                binding_id=binding.binding_id,
            )

            resolved_workspace_root = self._resolve_workspace_root_for_binding(
                binding=binding,
                existing_watch=watch,
                candidate_roots=candidate_roots,
                workspaces_by_repo_id=workspaces_by_repo_id,
                workspaces_by_thread_id=workspaces_by_thread_id,
                repo_slug_cache=repo_slug_cache,
            )
            if resolved_workspace_root is None:
                continue
            if (
                watch is not None
                and watch.state == "active"
                and Path(watch.workspace_root).resolve()
                == resolved_workspace_root.resolve()
            ):
                continue
            try:
                armed: Optional[ScmPollingWatch]
                if watch is not None and watch.state == "active":
                    armed = self._repair_active_watch(
                        binding=binding,
                        watch=watch,
                        workspace_root=resolved_workspace_root,
                    )
                else:
                    armed = self.arm_watch(
                        binding=binding,
                        workspace_root=resolved_workspace_root,
                    )
            except Exception:
                _LOGGER.warning(
                    "Failed arming discovered SCM polling watch for %s#%s",
                    binding.repo_slug,
                    binding.pr_number,
                    exc_info=True,
                )
                counts["discovery_errors"] += 1
                continue
            if armed is not None:
                counts["watches_armed"] += 1
        return counts

    def process_due_watches(self, *, limit: int = 20) -> dict[str, int]:
        counts = {
            "due": 0,
            "polled": 0,
            "events_emitted": 0,
            "expired": 0,
            "closed": 0,
            "errors": 0,
        }
        due_watches = self._watch_store.claim_due_watches(
            provider="github",
            limit=limit,
        )
        counts["due"] = len(due_watches)
        if not due_watches:
            return counts

        binding_store = PrBindingStore(self._hub_root)
        for watch in due_watches:
            if _parse_iso(watch.expires_at) <= _utc_now():
                self._watch_store.close_watch(watch_id=watch.watch_id, state="expired")
                counts["expired"] += 1
                continue

            binding = binding_store.get_binding_by_pr(
                provider="github",
                repo_slug=watch.repo_slug,
                pr_number=watch.pr_number,
            )
            if binding is None or binding.binding_id != watch.binding_id:
                self._watch_store.close_watch(watch_id=watch.watch_id, state="closed")
                counts["closed"] += 1
                continue
            if binding.pr_state not in _ACTIVE_PR_STATES:
                self._watch_store.close_watch(watch_id=watch.watch_id, state="closed")
                counts["closed"] += 1
                continue

            workspace_root = Path(watch.workspace_root)
            try:
                github = self._github_service_factory(
                    workspace_root,
                    self._raw_config if isinstance(self._raw_config, dict) else None,
                )
                snapshot = _build_snapshot(binding=binding, service=github)
            except Exception as exc:
                self._watch_store.refresh_watch(
                    watch_id=watch.watch_id,
                    next_poll_at=_iso_after_seconds(watch.poll_interval_seconds),
                    last_polled_at=now_iso(),
                    last_error_text=str(exc),
                )
                counts["errors"] += 1
                continue

            if snapshot.get("pr_state") not in _ACTIVE_PR_STATES:
                self._watch_store.close_watch(watch_id=watch.watch_id, state="closed")
                counts["closed"] += 1
                continue

            previous_snapshot = (
                watch.snapshot if isinstance(watch.snapshot, dict) else {}
            )
            baseline_pending = bool(previous_snapshot.get("baseline_pending"))
            emitted = 0
            if not baseline_pending:
                emitted += self._emit_new_conditions(
                    watch=watch,
                    binding=binding,
                    previous_snapshot=previous_snapshot,
                    snapshot=snapshot,
                )

            self._watch_store.refresh_watch(
                watch_id=watch.watch_id,
                snapshot=snapshot,
                next_poll_at=_iso_after_seconds(watch.poll_interval_seconds),
                last_polled_at=now_iso(),
                last_error_text=None,
            )
            counts["polled"] += 1
            counts["events_emitted"] += emitted
        return counts

    def process(self, *, limit: int = 20) -> dict[str, int]:
        counts = {
            "due": 0,
            "polled": 0,
            "events_emitted": 0,
            "expired": 0,
            "closed": 0,
            "errors": 0,
            "candidate_workspaces": 0,
            "bindings_discovered": 0,
            "watches_armed": 0,
            "discovery_errors": 0,
        }
        discovery_counts = self.discover_and_arm_missing_watches(limit=limit)
        due_counts = self.process_due_watches(limit=limit)
        for key, value in discovery_counts.items():
            counts[key] = counts.get(key, 0) + int(value)
        for key, value in due_counts.items():
            counts[key] = counts.get(key, 0) + int(value)
        return counts

    def _active_bindings(self, *, limit: int) -> dict[str, PrBinding]:
        binding_store = PrBindingStore(self._hub_root)
        active_bindings: dict[str, PrBinding] = {}
        for state in sorted(_ACTIVE_PR_STATES):
            for binding in binding_store.list_bindings(
                provider="github",
                pr_state=state,
                limit=limit,
            ):
                active_bindings[binding.binding_id] = binding
        return active_bindings

    def _repair_active_watch(
        self,
        *,
        binding: PrBinding,
        watch: ScmPollingWatch,
        workspace_root: Path,
    ) -> ScmPollingWatch:
        return self._watch_store.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug=binding.repo_slug,
            repo_id=binding.repo_id,
            pr_number=binding.pr_number,
            workspace_root=str(workspace_root.resolve()),
            thread_target_id=binding.thread_target_id,
            poll_interval_seconds=watch.poll_interval_seconds,
            next_poll_at=watch.next_poll_at,
            expires_at=watch.expires_at,
            reaction_config=watch.reaction_config,
            snapshot=watch.snapshot,
        )

    def _candidate_workspace_roots(
        self,
    ) -> tuple[list[Path], dict[str, list[Path]], dict[str, Path]]:
        roots: list[Path] = []
        seen_roots: set[Path] = set()
        workspaces_by_repo_id: dict[str, list[Path]] = {}
        workspaces_by_thread_id: dict[str, Path] = {}

        def add_root(
            workspace_root: Path,
            *,
            repo_id: Optional[str] = None,
            thread_target_id: Optional[str] = None,
        ) -> None:
            resolved_root = workspace_root.resolve()
            if not resolved_root.exists() or not resolved_root.is_dir():
                return
            if resolved_root not in seen_roots:
                seen_roots.add(resolved_root)
                roots.append(resolved_root)
            normalized_repo_id = _normalize_text(repo_id)
            if normalized_repo_id is not None:
                bucket = workspaces_by_repo_id.setdefault(normalized_repo_id, [])
                if resolved_root not in bucket:
                    bucket.append(resolved_root)
            normalized_thread_target_id = _normalize_text(thread_target_id)
            if normalized_thread_target_id is not None:
                workspaces_by_thread_id[normalized_thread_target_id] = resolved_root

        manifest_path = self._hub_root / ".codex-autorunner" / "manifest.yml"
        if manifest_path.exists():
            try:
                manifest = load_manifest(manifest_path, self._hub_root)
            except ManifestError:
                manifest = None
            if manifest is not None:
                for repo in manifest.repos:
                    if not repo.enabled:
                        continue
                    add_root(self._hub_root / repo.path, repo_id=repo.id)

        try:
            threads = PmaThreadStore(self._hub_root).list_threads(
                status="active",
                limit=500,
            )
        except Exception:
            threads = []
        for thread in threads:
            workspace_root = _normalize_text(thread.get("workspace_root"))
            if workspace_root is None:
                continue
            add_root(
                Path(workspace_root),
                repo_id=_normalize_text(thread.get("repo_id")),
                thread_target_id=_normalize_text(thread.get("managed_thread_id")),
            )
        return roots, workspaces_by_repo_id, workspaces_by_thread_id

    def _resolve_workspace_root_for_binding(
        self,
        *,
        binding: PrBinding,
        existing_watch: Optional[ScmPollingWatch],
        candidate_roots: list[Path],
        workspaces_by_repo_id: Mapping[str, list[Path]],
        workspaces_by_thread_id: Mapping[str, Path],
        repo_slug_cache: dict[str, Optional[str]],
    ) -> Optional[Path]:
        if existing_watch is not None:
            existing_watch_root = Path(existing_watch.workspace_root).resolve()
            if existing_watch_root.exists() and existing_watch_root.is_dir():
                return existing_watch_root

        if binding.thread_target_id is not None:
            thread_root = workspaces_by_thread_id.get(binding.thread_target_id)
            if thread_root is not None:
                return thread_root

        if binding.repo_id is not None:
            repo_roots = workspaces_by_repo_id.get(binding.repo_id) or []
            if repo_roots:
                return repo_roots[0]

        for candidate_root in candidate_roots:
            candidate_key = str(candidate_root)
            if candidate_key not in repo_slug_cache:
                try:
                    github = self._github_service_factory(
                        candidate_root,
                        (
                            self._raw_config
                            if isinstance(self._raw_config, dict)
                            else None
                        ),
                    )
                    repo_slug_cache[candidate_key] = _normalize_text(
                        github.repo_info().name_with_owner
                    )
                except Exception:
                    repo_slug_cache[candidate_key] = None
            if repo_slug_cache.get(candidate_key) == binding.repo_slug:
                return candidate_root
        return None

    def _emit_new_conditions(
        self,
        *,
        watch: ScmPollingWatch,
        binding: PrBinding,
        previous_snapshot: Mapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> int:
        previous_reviews = _snapshot_map(previous_snapshot, "changes_requested_reviews")
        current_reviews = _snapshot_map(snapshot, "changes_requested_reviews")
        previous_checks = _snapshot_map(previous_snapshot, "failed_checks")
        current_checks = _snapshot_map(snapshot, "failed_checks")
        previous_issue_comments = _snapshot_map(previous_snapshot, "issue_comments")
        current_issue_comments = _snapshot_map(snapshot, "issue_comments")
        previous_review_thread_comments = _snapshot_map(
            previous_snapshot, "review_thread_comments"
        )
        current_review_thread_comments = _snapshot_map(
            snapshot, "review_thread_comments"
        )

        automation = self._build_automation_service(
            reaction_config=watch.reaction_config or self._raw_config,
        )
        emitted = 0
        for key, payload in current_reviews.items():
            if key in previous_reviews:
                continue
            event = self._event_store.record_event(
                event_id=f"github:poll:review:{watch.watch_id}:{uuid.uuid4().hex[:12]}",
                provider="github",
                event_type="pull_request_review",
                occurred_at=_normalize_text(payload.get("submitted_at")) or now_iso(),
                received_at=now_iso(),
                repo_slug=watch.repo_slug,
                repo_id=binding.repo_id or watch.repo_id,
                pr_number=watch.pr_number,
                correlation_id=f"scm-poll:{watch.watch_id}",
                payload=dict(payload),
            )
            automation.ingest_event(event)
            emitted += 1

        for key, payload in current_checks.items():
            if key in previous_checks:
                continue
            event = self._event_store.record_event(
                event_id=f"github:poll:check:{watch.watch_id}:{uuid.uuid4().hex[:12]}",
                provider="github",
                event_type="check_run",
                occurred_at=now_iso(),
                received_at=now_iso(),
                repo_slug=watch.repo_slug,
                repo_id=binding.repo_id or watch.repo_id,
                pr_number=watch.pr_number,
                correlation_id=f"scm-poll:{watch.watch_id}",
                payload=dict(payload),
            )
            automation.ingest_event(event)
            emitted += 1

        for key, payload in current_issue_comments.items():
            if key in previous_issue_comments:
                continue
            event = self._event_store.record_event(
                event_id=(
                    f"github:poll:issue-comment:{watch.watch_id}:{uuid.uuid4().hex[:12]}"
                ),
                provider="github",
                event_type="issue_comment",
                occurred_at=_comment_timestamp(payload) or now_iso(),
                received_at=now_iso(),
                repo_slug=watch.repo_slug,
                repo_id=binding.repo_id or watch.repo_id,
                pr_number=watch.pr_number,
                correlation_id=f"scm-poll:{watch.watch_id}",
                payload=dict(payload),
            )
            automation.ingest_event(event)
            emitted += 1

        for key, payload in current_review_thread_comments.items():
            if bool(payload.get("thread_resolved")):
                continue
            if key in previous_review_thread_comments:
                continue
            event = self._event_store.record_event(
                event_id=(
                    f"github:poll:review-comment:{watch.watch_id}:{uuid.uuid4().hex[:12]}"
                ),
                provider="github",
                event_type="pull_request_review_comment",
                occurred_at=_comment_timestamp(payload) or now_iso(),
                received_at=now_iso(),
                repo_slug=watch.repo_slug,
                repo_id=binding.repo_id or watch.repo_id,
                pr_number=watch.pr_number,
                correlation_id=f"scm-poll:{watch.watch_id}",
                payload=dict(payload),
            )
            automation.ingest_event(event)
            emitted += 1

        if emitted:
            automation.process_now()
        return emitted

    def _build_automation_service(
        self,
        *,
        reaction_config: Mapping[str, Any] | None,
    ):
        from ...core.scm_automation_service import ScmAutomationService

        return ScmAutomationService(
            self._hub_root,
            reaction_config=reaction_config or self._raw_config,
        )


def build_hub_scm_poll_processor(
    *,
    hub_root: Path,
    raw_config: Optional[dict[str, Any]] = None,
):
    def processor(limit: int = 20) -> dict[str, int]:
        return GitHubScmPollingService(
            hub_root,
            raw_config=raw_config,
        ).process(limit=limit)

    return processor


__all__ = [
    "GitHubPollingConfig",
    "GitHubScmPollingService",
    "build_hub_scm_poll_processor",
]
