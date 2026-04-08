from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from ...core.chat_bindings import (
    preferred_non_pma_chat_notification_sources_by_workspace,
)
from ...core.orchestration.sqlite import open_orchestration_sqlite
from ...core.pma_thread_store import PmaThreadStore
from ...core.pr_binding_runtime import backfill_pr_binding_thread_target_ids
from ...core.pr_bindings import PrBinding, PrBindingStore
from ...core.scm_events import ScmEventStore
from ...core.scm_polling_watches import ScmPollingWatch, ScmPollingWatchStore
from ...core.scm_reaction_types import ScmReactionConfig
from ...core.text_utils import _mapping, _normalize_text
from ...core.time_utils import now_iso
from ...core.utils import atomic_write, read_json
from ...manifest import ManifestError, load_manifest

_FAILED_CHECK_CONCLUSIONS = frozenset(
    {"action_required", "cancelled", "failure", "startup_failure", "stale", "timed_out"}
)
_ACTIVE_PR_STATES = frozenset({"open", "draft"})
_VALID_PR_STATES = frozenset({"open", "draft", "closed", "merged"})
_ACTIVITY_PRIORITY = {"hot": 0, "warm": 1, "cold": 2}
_HOT_THREAD_WINDOW_MINUTES = 60
_RECENT_THREAD_WINDOW_MINUTES = 24 * 60
_WARM_INTERVAL_SECONDS_FLOOR = 15 * 60
_COLD_INTERVAL_SECONDS_FLOOR = 60 * 60
_RATE_LIMIT_MIN_REMAINING = 100
_RATE_LIMIT_RATIO_FLOOR = 0.02
_RATE_LIMIT_BACKOFF_SECONDS = 15 * 60
_RATE_LIMIT_QUOTA_CACHE_TTL_SECONDS = 10 * 60
_RATE_LIMIT_QUOTA_NEAR_LIMIT_FALLBACK_TTL_SECONDS = 60
_RATE_LIMIT_QUOTA_ERROR_CACHE_TTL_SECONDS = 30
_RATE_LIMIT_RESOURCES = ("graphql", "core")
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


def _normalize_positive_int(value: Any) -> Optional[int]:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _normalize_non_negative_int(value: Any) -> Optional[int]:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _parse_optional_iso(value: Any) -> Optional[datetime]:
    normalized = _normalize_text(value)
    if normalized is None:
        return None
    try:
        return _parse_iso(normalized)
    except ValueError:
        return None


def _is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    return "rate limit" in str(exc).lower()


def _timestamp_from_epoch(epoch_seconds: Optional[int]) -> Optional[str]:
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@dataclass(frozen=True)
class _GitHubQuotaState:
    resource: str
    remaining: int
    limit: int
    reset_epoch: Optional[int]
    near_limit: bool

    @property
    def reset_at(self) -> Optional[str]:
        return _timestamp_from_epoch(self.reset_epoch)


@dataclass(frozen=True)
class _CachedQuotaState:
    value: Optional[_GitHubQuotaState]
    expires_at: datetime


def _cached_quota_state_from_mapping(value: Any) -> Optional[_CachedQuotaState]:
    payload = _mapping(value)
    expires_at_raw = _normalize_text(payload.get("expires_at"))
    if expires_at_raw is None:
        return None
    try:
        expires_at = _parse_iso(expires_at_raw)
    except ValueError:
        return None
    quota_state_payload = _mapping(payload.get("value"))
    resource = _normalize_text(quota_state_payload.get("resource"))
    remaining = _normalize_non_negative_int(quota_state_payload.get("remaining"))
    limit = _normalize_positive_int(quota_state_payload.get("limit"))
    reset_epoch = _normalize_positive_int(quota_state_payload.get("reset_epoch"))
    near_limit_raw = quota_state_payload.get("near_limit")
    if not quota_state_payload:
        return _CachedQuotaState(value=None, expires_at=expires_at)
    if (
        resource is None
        or remaining is None
        or limit is None
        or not isinstance(near_limit_raw, bool)
    ):
        return None
    return _CachedQuotaState(
        value=_GitHubQuotaState(
            resource=resource,
            remaining=remaining,
            limit=limit,
            reset_epoch=reset_epoch,
            near_limit=near_limit_raw,
        ),
        expires_at=expires_at,
    )


def _cached_quota_state_to_mapping(
    value: Optional[_CachedQuotaState],
) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    quota_state = value.value
    payload: dict[str, Any] = {
        "expires_at": value.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "value": None,
    }
    if quota_state is not None:
        payload["value"] = {
            "resource": quota_state.resource,
            "remaining": quota_state.remaining,
            "limit": quota_state.limit,
            "reset_epoch": quota_state.reset_epoch,
            "near_limit": quota_state.near_limit,
        }
    return payload


def _quota_state_from_payload(
    payload: Mapping[str, Any],
) -> Optional[_GitHubQuotaState]:
    resources = _mapping(payload.get("resources"))
    selected: Optional[tuple[float, int, _GitHubQuotaState]] = None
    for resource_name in _RATE_LIMIT_RESOURCES:
        entry = _mapping(resources.get(resource_name))
        limit = _normalize_positive_int(entry.get("limit"))
        remaining = _normalize_non_negative_int(entry.get("remaining"))
        reset_epoch = _normalize_positive_int(entry.get("reset"))
        if limit is None or remaining is None:
            continue
        remaining_ratio = remaining / float(limit)
        near_limit = (
            remaining <= min(_RATE_LIMIT_MIN_REMAINING, limit)
            or remaining_ratio <= _RATE_LIMIT_RATIO_FLOOR
        )
        candidate = _GitHubQuotaState(
            resource=resource_name,
            remaining=remaining,
            limit=limit,
            reset_epoch=reset_epoch,
            near_limit=near_limit,
        )
        ranking = (remaining_ratio, remaining, candidate)
        if selected is None or ranking[:2] < selected[:2]:
            selected = ranking
    return selected[2] if selected is not None else None


def _rate_limit_backoff_until(quota_state: Optional[_GitHubQuotaState]) -> str:
    reset_at = quota_state.reset_at if quota_state is not None else None
    if reset_at is not None:
        reset_dt = _parse_iso(reset_at)
        if reset_dt > _utc_now():
            return reset_at
    return _iso_after_seconds(_RATE_LIMIT_BACKOFF_SECONDS)


def _activity_sort_key(timestamp: Optional[datetime]) -> tuple[int, float]:
    if timestamp is None:
        return (1, 0.0)
    recent_cutoff = _utc_now() - timedelta(minutes=_RECENT_THREAD_WINDOW_MINUTES)
    if timestamp >= recent_cutoff:
        return (0, -timestamp.timestamp())
    return (2, -timestamp.timestamp())


def _rotated(items: list[Path], *, offset: int) -> list[Path]:
    if not items:
        return []
    normalized_offset = offset % len(items)
    if normalized_offset == 0:
        return list(items)
    return list(items[normalized_offset:]) + list(items[:normalized_offset])


def _binding_from_polling_row(row: sqlite3.Row) -> PrBinding:
    binding_id = _normalize_text(row["binding_id"])
    provider = _normalize_text(row["provider"])
    repo_slug = _normalize_text(row["repo_slug"])
    pr_number = _normalize_positive_int(row["pr_number"])
    pr_state = _normalize_lower_text(row["pr_state"])
    created_at = _normalize_text(row["created_at"])
    updated_at = _normalize_text(row["updated_at"])
    if binding_id is None:
        raise ValueError("binding_id is required")
    if provider is None:
        raise ValueError("provider is required")
    if repo_slug is None:
        raise ValueError("repo_slug is required")
    if pr_number is None:
        raise ValueError("pr_number must be > 0")
    if pr_state not in _VALID_PR_STATES:
        raise ValueError("pr_state must be a valid PR state")
    if created_at is None or updated_at is None:
        raise ValueError("binding timestamps are required")
    return PrBinding(
        binding_id=binding_id,
        provider=provider,
        repo_slug=repo_slug,
        pr_number=pr_number,
        pr_state=pr_state,
        created_at=created_at,
        updated_at=updated_at,
        repo_id=_normalize_text(row["repo_id"]),
        head_branch=_normalize_text(row["head_branch"]),
        base_branch=_normalize_text(row["base_branch"]),
        thread_target_id=_normalize_text(row["thread_target_id"]),
        closed_at=_normalize_text(row["closed_at"]),
    )


@dataclass(frozen=True)
class GitHubPollingConfig:
    enabled: bool = False
    watch_window_minutes: int = 30
    interval_seconds: int = 90
    discovery_interval_seconds: int = 6 * 60
    discovery_workspace_limit: int = 1

    @classmethod
    def from_mapping(cls, raw_config: object) -> "GitHubPollingConfig":
        github = _mapping(raw_config).get("github")
        automation = _mapping(github).get("automation")
        polling = _mapping(_mapping(automation).get("polling"))
        enabled = polling.get("enabled")
        watch_window_minutes = polling.get("watch_window_minutes")
        interval_seconds = polling.get("interval_seconds")
        discovery_interval_seconds = polling.get("discovery_interval_seconds")
        discovery_workspace_limit = polling.get("discovery_workspace_limit")
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
            discovery_interval_seconds=(
                int(discovery_interval_seconds)
                if (
                    isinstance(discovery_interval_seconds, int)
                    and discovery_interval_seconds > 0
                )
                else 6 * 60
            ),
            discovery_workspace_limit=(
                int(discovery_workspace_limit)
                if (
                    isinstance(discovery_workspace_limit, int)
                    and discovery_workspace_limit > 0
                )
                else 1
            ),
        )

    @property
    def comment_backfill_window_seconds(self) -> int:
        return self.watch_window_minutes * 60


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


def _comment_backfill_lower_bound(
    *,
    snapshot: Mapping[str, Any],
    reference_timestamp: str,
    window_seconds: int,
) -> Optional[datetime]:
    reference_at = _parse_optional_iso(reference_timestamp)
    if reference_at is None:
        return None
    lower_bound = reference_at - timedelta(seconds=max(0, int(window_seconds)))
    pr_created_at = _parse_optional_iso(snapshot.get("pr_created_at"))
    if pr_created_at is not None and pr_created_at > lower_bound:
        lower_bound = pr_created_at
    return lower_bound


def _comment_in_backfill_window(
    comment: Mapping[str, Any],
    *,
    lower_bound: Optional[datetime],
) -> bool:
    if lower_bound is None:
        return False
    comment_at = _parse_optional_iso(_comment_timestamp(comment))
    return comment_at is not None and comment_at >= lower_bound


def _snapshot_without_backfilled_comments(
    snapshot: Mapping[str, Any],
    *,
    reference_timestamp: str,
    window_seconds: int,
) -> dict[str, Any]:
    lower_bound = _comment_backfill_lower_bound(
        snapshot=snapshot,
        reference_timestamp=reference_timestamp,
        window_seconds=window_seconds,
    )
    if lower_bound is None:
        return dict(snapshot)

    previous_snapshot = dict(snapshot)
    issue_comments = {
        key: dict(payload)
        for key, payload in _snapshot_map(snapshot, "issue_comments").items()
        if not _comment_in_backfill_window(payload, lower_bound=lower_bound)
    }
    review_thread_comments = {
        key: dict(payload)
        for key, payload in _snapshot_map(snapshot, "review_thread_comments").items()
        if not _comment_in_backfill_window(payload, lower_bound=lower_bound)
    }
    if issue_comments:
        previous_snapshot["issue_comments"] = issue_comments
    else:
        previous_snapshot.pop("issue_comments", None)
    if review_thread_comments:
        previous_snapshot["review_thread_comments"] = review_thread_comments
    else:
        previous_snapshot.pop("review_thread_comments", None)
    return previous_snapshot


def _build_snapshot(
    *,
    binding: PrBinding,
    service: GitHubService,
) -> dict[str, Any]:
    pr = service.pr_view(number=binding.pr_number, repo_slug=binding.repo_slug)
    head_sha = _normalize_text(pr.get("headRefOid"))
    pr_created_at = _normalize_text(pr.get("createdAt")) or _normalize_text(
        pr.get("created_at")
    )
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
    if pr_created_at is not None:
        snapshot["pr_created_at"] = pr_created_at
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

            def _default_github_service_factory(repo_root, service_raw_config):
                return GitHubService(
                    repo_root,
                    service_raw_config,
                    config_root=self._hub_root,
                    traffic_class="polling",
                )

            github_service_factory = _default_github_service_factory
        self._github_service_factory = github_service_factory
        self._watch_store = watch_store or ScmPollingWatchStore(self._hub_root)
        self._event_store = event_store or ScmEventStore(self._hub_root)
        self._polling_state_path = (
            self._hub_root / ".codex-autorunner" / "github_polling_state.json"
        )

    def _emit_comment_backfill(
        self,
        *,
        watch: ScmPollingWatch,
        binding: PrBinding,
        snapshot: Mapping[str, Any],
        reference_timestamp: str,
        window_seconds: int,
    ) -> int:
        previous_snapshot = _snapshot_without_backfilled_comments(
            snapshot,
            reference_timestamp=reference_timestamp,
            window_seconds=window_seconds,
        )
        if _snapshot_map(previous_snapshot, "issue_comments") == _snapshot_map(
            snapshot, "issue_comments"
        ) and _snapshot_map(
            previous_snapshot, "review_thread_comments"
        ) == _snapshot_map(
            snapshot, "review_thread_comments"
        ):
            return 0
        return self._emit_new_conditions(
            watch=watch,
            binding=binding,
            previous_snapshot=previous_snapshot,
            snapshot=snapshot,
        )

    def arm_watch(
        self,
        *,
        binding: PrBinding,
        workspace_root: Path,
        reaction_config: Optional[Mapping[str, Any]] = None,
        establish_baseline: bool = True,
        next_poll_at: Optional[str] = None,
    ) -> Optional[ScmPollingWatch]:
        polling_config = GitHubPollingConfig.from_mapping(self._raw_config)
        if not polling_config.enabled or binding.pr_state not in _ACTIVE_PR_STATES:
            return None

        now_timestamp = now_iso()
        expires_at = _iso_after_seconds(polling_config.watch_window_minutes * 60)
        scheduled_next_poll_at = next_poll_at or _iso_after_seconds(
            polling_config.interval_seconds
        )
        snapshot: dict[str, Any] = {"baseline_pending": True}
        if establish_baseline:
            try:
                github = self._github_service_factory(
                    workspace_root,
                    self._raw_config if isinstance(self._raw_config, dict) else None,
                )
                snapshot = _build_snapshot(binding=binding, service=github)
            except Exception as exc:
                _LOGGER.warning(
                    "Failed establishing SCM polling baseline for %s#%s",
                    binding.repo_slug,
                    binding.pr_number,
                    exc_info=True,
                )
                if _is_rate_limit_error(exc):
                    self._invalidate_quota_state_cache()
                scheduled_next_poll_at = (
                    _iso_after_seconds(_RATE_LIMIT_BACKOFF_SECONDS)
                    if _is_rate_limit_error(exc)
                    else now_timestamp
                )

        watch = self._watch_store.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug=binding.repo_slug,
            repo_id=binding.repo_id,
            pr_number=binding.pr_number,
            workspace_root=str(workspace_root.resolve()),
            thread_target_id=binding.thread_target_id,
            poll_interval_seconds=polling_config.interval_seconds,
            next_poll_at=scheduled_next_poll_at,
            expires_at=expires_at,
            reaction_config=_reaction_config_mapping(
                _github_automation_config(reaction_config or self._raw_config)
            ),
            snapshot=snapshot,
        )
        if snapshot.get("baseline_pending"):
            return watch
        try:
            self._emit_comment_backfill(
                watch=watch,
                binding=binding,
                snapshot=snapshot,
                reference_timestamp=now_timestamp,
                window_seconds=polling_config.comment_backfill_window_seconds,
            )
        except Exception:
            _LOGGER.warning(
                "Failed emitting SCM polling comment backfill for %s#%s",
                binding.repo_slug,
                binding.pr_number,
                exc_info=True,
            )
        return watch

    def discover_and_arm_missing_watches(self, *, limit: int = 20) -> dict[str, int]:
        counts = {
            "candidate_workspaces": 0,
            "candidate_workspaces_scanned": 0,
            "bindings_discovered": 0,
            "watches_armed": 0,
            "discovery_errors": 0,
            "invalid_bindings_skipped": 0,
            "rate_limited_skipped": 0,
        }
        polling_config = GitHubPollingConfig.from_mapping(self._raw_config)
        if not polling_config.enabled:
            return counts

        candidate_roots, workspaces_by_repo_id, workspaces_by_thread_id = (
            self._candidate_workspace_roots()
        )
        counts["candidate_workspaces"] = len(candidate_roots)
        thread_activity_by_thread, workspace_activity = self._thread_activity()

        active_bindings, invalid_bindings = self._active_bindings(
            limit=max(100, limit * 10)
        )
        counts["invalid_bindings_skipped"] += invalid_bindings

        if self._claim_discovery_cycle(polling_config=polling_config):
            discovery_limit = max(
                1,
                min(limit, polling_config.discovery_workspace_limit),
            )
            prioritized_roots = self._prioritized_discovery_roots(
                candidate_roots=candidate_roots,
                workspace_activity=workspace_activity,
                polling_config=polling_config,
                discovery_limit=discovery_limit,
            )
            for workspace_root in prioritized_roots[:discovery_limit]:
                counts["candidate_workspaces_scanned"] += 1
                try:
                    github = self._github_service_factory(
                        workspace_root,
                        (
                            self._raw_config
                            if isinstance(self._raw_config, dict)
                            else None
                        ),
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
        quota_state_cache: dict[str, Optional[_GitHubQuotaState]] = {}
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
            activity_tier = self._activity_tier_for_binding(
                binding=binding,
                workspace_root=resolved_workspace_root,
                watch=watch,
                thread_activity_by_thread=thread_activity_by_thread,
                workspace_activity=workspace_activity,
            )
            scheduled_next_poll_at = _iso_after_seconds(
                self._poll_interval_for_tier(
                    activity_tier=activity_tier,
                    polling_config=polling_config,
                )
            )
            quota_state: Optional[_GitHubQuotaState] = None
            defer_baseline = False
            if watch is None or watch.state != "active":
                quota_state = self._quota_state_for_workspace(
                    workspace_root=resolved_workspace_root,
                    cache=quota_state_cache,
                )
                defer_baseline = bool(
                    quota_state is not None
                    and quota_state.near_limit
                    and activity_tier != "hot"
                )
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
                        establish_baseline=not defer_baseline,
                        next_poll_at=scheduled_next_poll_at,
                    )
                    if armed is not None and defer_baseline:
                        armed = (
                            self._watch_store.refresh_watch(
                                watch_id=armed.watch_id,
                                next_poll_at=_rate_limit_backoff_until(quota_state),
                                last_error_text=(
                                    "GitHub rate-limit budget low; baseline deferred"
                                ),
                            )
                            or armed
                        )
                        counts["rate_limited_skipped"] += 1
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
            "rate_limited_skipped": 0,
        }
        polling_config = GitHubPollingConfig.from_mapping(self._raw_config)
        due_watches = self._watch_store.claim_due_watches(
            provider="github",
            limit=limit,
        )
        counts["due"] = len(due_watches)
        if not due_watches:
            return counts

        thread_activity_by_thread, workspace_activity = self._thread_activity()
        binding_store = PrBindingStore(self._hub_root)
        pending_watches: list[tuple[str, ScmPollingWatch, PrBinding, Path]] = []
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
            activity_tier = self._activity_tier_for_binding(
                binding=binding,
                workspace_root=workspace_root,
                watch=watch,
                thread_activity_by_thread=thread_activity_by_thread,
                workspace_activity=workspace_activity,
            )
            pending_watches.append((activity_tier, watch, binding, workspace_root))

        pending_watches.sort(
            key=lambda item: (
                _ACTIVITY_PRIORITY.get(item[0], 1),
                item[1].next_poll_at,
                item[1].watch_id,
            )
        )
        quota_state_cache: dict[str, Optional[_GitHubQuotaState]] = {}
        for activity_tier, watch, binding, workspace_root in pending_watches:
            quota_state = self._quota_state_for_workspace(
                workspace_root=workspace_root,
                cache=quota_state_cache,
            )
            if (
                quota_state is not None
                and quota_state.near_limit
                and activity_tier != "hot"
            ):
                self._watch_store.refresh_watch(
                    watch_id=watch.watch_id,
                    next_poll_at=_rate_limit_backoff_until(quota_state),
                    last_polled_at=now_iso(),
                    last_error_text="GitHub rate-limit budget low; polling deferred",
                )
                counts["rate_limited_skipped"] += 1
                continue
            try:
                github = self._github_service_factory(
                    workspace_root,
                    self._raw_config if isinstance(self._raw_config, dict) else None,
                )
                snapshot = _build_snapshot(binding=binding, service=github)
            except Exception as exc:
                if _is_rate_limit_error(exc):
                    self._invalidate_quota_state_cache()
                    self._watch_store.refresh_watch(
                        watch_id=watch.watch_id,
                        next_poll_at=_rate_limit_backoff_until(quota_state),
                        last_polled_at=now_iso(),
                        last_error_text=str(exc),
                    )
                    counts["rate_limited_skipped"] += 1
                    continue
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
            if baseline_pending:
                baseline_reference_timestamp = now_iso()
                emitted += self._emit_comment_backfill(
                    watch=watch,
                    binding=binding,
                    snapshot=snapshot,
                    reference_timestamp=baseline_reference_timestamp,
                    window_seconds=polling_config.comment_backfill_window_seconds,
                )
            else:
                emitted += self._emit_new_conditions(
                    watch=watch,
                    binding=binding,
                    previous_snapshot=previous_snapshot,
                    snapshot=snapshot,
                )

            self._watch_store.refresh_watch(
                watch_id=watch.watch_id,
                snapshot=snapshot,
                next_poll_at=_iso_after_seconds(
                    self._poll_interval_for_tier(
                        activity_tier=activity_tier,
                        polling_config=polling_config,
                    )
                ),
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
            "candidate_workspaces_scanned": 0,
            "bindings_discovered": 0,
            "bindings_backfilled": 0,
            "watches_armed": 0,
            "discovery_errors": 0,
            "invalid_bindings_skipped": 0,
            "rate_limited_skipped": 0,
        }
        discovery_counts = self.discover_and_arm_missing_watches(limit=limit)
        backfill_counts = self.backfill_binding_thread_targets(limit=max(limit, 200))
        due_counts = self.process_due_watches(limit=limit)
        for key, value in discovery_counts.items():
            counts[key] = counts.get(key, 0) + int(value)
        counts["bindings_backfilled"] += int(backfill_counts["bindings_updated"])
        for key, value in due_counts.items():
            counts[key] = counts.get(key, 0) + int(value)
        _LOGGER.info(
            "GitHub SCM poll cycle: scanned=%s/%s discovered=%s armed=%s "
            "backfilled=%s due=%s polled=%s emitted=%s rate_limited=%s invalid_bindings=%s "
            "closed=%s expired=%s errors=%s",
            counts["candidate_workspaces_scanned"],
            counts["candidate_workspaces"],
            counts["bindings_discovered"],
            counts["watches_armed"],
            counts["bindings_backfilled"],
            counts["due"],
            counts["polled"],
            counts["events_emitted"],
            counts["rate_limited_skipped"],
            counts["invalid_bindings_skipped"],
            counts["closed"],
            counts["expired"],
            counts["errors"] + counts["discovery_errors"],
        )
        return counts

    def backfill_binding_thread_targets(self, *, limit: int = 200) -> dict[str, int]:
        return backfill_pr_binding_thread_target_ids(
            self._hub_root,
            limit=limit,
        )

    def _active_bindings(self, *, limit: int) -> tuple[dict[str, PrBinding], int]:
        active_bindings: dict[str, PrBinding] = {}
        invalid_rows = 0
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_pr_bindings
                 WHERE provider = ?
                   AND pr_state IN (?, ?)
                 ORDER BY updated_at DESC, created_at DESC, pr_number DESC
                 LIMIT ?
                """,
                ("github", "open", "draft", limit),
            ).fetchall()
        for row in rows:
            try:
                binding = _binding_from_polling_row(row)
            except Exception:
                invalid_rows += 1
                _LOGGER.warning(
                    "Skipping malformed SCM polling binding row binding_id=%s repo_slug=%s",
                    row["binding_id"] if "binding_id" in row.keys() else None,
                    row["repo_slug"] if "repo_slug" in row.keys() else None,
                    exc_info=True,
                )
                continue
            active_bindings[binding.binding_id] = binding
        return active_bindings, invalid_rows

    def _thread_activity(
        self,
    ) -> tuple[dict[str, datetime], dict[str, datetime]]:
        by_thread: dict[str, datetime] = {}
        by_workspace: dict[str, datetime] = {}
        try:
            threads = PmaThreadStore(self._hub_root).list_threads(
                status="active",
                limit=1000,
            )
        except Exception:
            return by_thread, by_workspace
        for thread in threads:
            activity_at = max(
                (
                    timestamp
                    for timestamp in (
                        _parse_optional_iso(thread.get("status_updated_at")),
                        _parse_optional_iso(thread.get("updated_at")),
                    )
                    if timestamp is not None
                ),
                default=None,
            )
            if activity_at is None:
                continue
            thread_target_id = _normalize_text(thread.get("managed_thread_id"))
            if thread_target_id is not None:
                prior_thread_activity = by_thread.get(thread_target_id)
                if prior_thread_activity is None or activity_at > prior_thread_activity:
                    by_thread[thread_target_id] = activity_at
            workspace_root = _normalize_text(thread.get("workspace_root"))
            if workspace_root is not None:
                workspace_key = str(Path(workspace_root).resolve())
                prior_workspace_activity = by_workspace.get(workspace_key)
                if (
                    prior_workspace_activity is None
                    or activity_at > prior_workspace_activity
                ):
                    by_workspace[workspace_key] = activity_at
        return by_thread, by_workspace

    def _activity_tier_for_binding(
        self,
        *,
        binding: PrBinding,
        workspace_root: Path,
        watch: Optional[ScmPollingWatch],
        thread_activity_by_thread: Mapping[str, datetime],
        workspace_activity: Mapping[str, datetime],
    ) -> str:
        activity_at: Optional[datetime] = None
        if binding.thread_target_id is not None:
            activity_at = thread_activity_by_thread.get(binding.thread_target_id)
        if (
            activity_at is None
            and watch is not None
            and watch.thread_target_id is not None
        ):
            activity_at = thread_activity_by_thread.get(watch.thread_target_id)
        if activity_at is None:
            activity_at = workspace_activity.get(str(workspace_root.resolve()))
        if activity_at is None:
            return "warm"
        if activity_at >= _utc_now() - timedelta(minutes=_HOT_THREAD_WINDOW_MINUTES):
            return "hot"
        if activity_at >= _utc_now() - timedelta(minutes=_RECENT_THREAD_WINDOW_MINUTES):
            return "warm"
        return "cold"

    def _poll_interval_for_tier(
        self,
        *,
        activity_tier: str,
        polling_config: GitHubPollingConfig,
    ) -> int:
        if activity_tier == "hot":
            return polling_config.interval_seconds
        if activity_tier == "cold":
            return max(
                polling_config.interval_seconds * 40,
                _COLD_INTERVAL_SECONDS_FLOOR,
            )
        return max(
            polling_config.interval_seconds * 10,
            _WARM_INTERVAL_SECONDS_FLOOR,
        )

    def _quota_state_for_workspace(
        self,
        *,
        workspace_root: Path,
        cache: dict[str, Optional[_GitHubQuotaState]],
    ) -> Optional[_GitHubQuotaState]:
        cache_key = "global"
        if cache_key in cache:
            return cache[cache_key]
        now = _utc_now()
        persisted = self._read_cached_quota_state(cache_key=cache_key)
        if persisted is not None:
            if persisted.expires_at > now:
                cache[cache_key] = persisted.value
                return persisted.value
            self._invalidate_quota_state_cache(cache_key=cache_key)
        try:
            github = self._github_service_factory(
                workspace_root,
                self._raw_config if isinstance(self._raw_config, dict) else None,
            )
            cache[cache_key] = _quota_state_from_payload(github.rate_limit_status())
        except Exception:
            cache[cache_key] = None
        self._write_cached_quota_state(
            cache_key=cache_key,
            value=_CachedQuotaState(
                value=cache[cache_key],
                expires_at=self._quota_state_cache_expiry(cache[cache_key], now=now),
            ),
        )
        return cache[cache_key]

    def _quota_state_cache_expiry(
        self,
        quota_state: Optional[_GitHubQuotaState],
        *,
        now: Optional[datetime] = None,
    ) -> datetime:
        current = now or _utc_now()
        if quota_state is None:
            return current + timedelta(
                seconds=_RATE_LIMIT_QUOTA_ERROR_CACHE_TTL_SECONDS
            )
        if not quota_state.near_limit:
            return current + timedelta(seconds=_RATE_LIMIT_QUOTA_CACHE_TTL_SECONDS)
        reset_at = quota_state.reset_at
        if reset_at is not None:
            reset_timestamp = _parse_iso(reset_at)
            if reset_timestamp > current:
                return min(
                    reset_timestamp,
                    current + timedelta(seconds=_RATE_LIMIT_QUOTA_CACHE_TTL_SECONDS),
                )
        return current + timedelta(
            seconds=_RATE_LIMIT_QUOTA_NEAR_LIMIT_FALLBACK_TTL_SECONDS
        )

    def _invalidate_quota_state_cache(self, *, cache_key: str = "global") -> None:
        state = self._read_polling_state()
        quota_state_cache = _mapping(state.get("quota_state_cache"))
        if cache_key not in quota_state_cache:
            return
        updated_cache = dict(quota_state_cache)
        updated_cache.pop(cache_key, None)
        state["quota_state_cache"] = updated_cache
        self._write_polling_state(state)

    def _read_polling_state(self) -> dict[str, Any]:
        state = read_json(self._polling_state_path) or {}
        return dict(state) if isinstance(state, dict) else {}

    def _write_polling_state(self, state: Mapping[str, Any]) -> None:
        self._polling_state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self._polling_state_path,
            json.dumps(state, indent=2, sort_keys=True) + "\n",
        )

    def _read_cached_quota_state(
        self, *, cache_key: str
    ) -> Optional[_CachedQuotaState]:
        state = self._read_polling_state()
        quota_state_cache = _mapping(state.get("quota_state_cache"))
        return _cached_quota_state_from_mapping(quota_state_cache.get(cache_key))

    def _write_cached_quota_state(
        self,
        *,
        cache_key: str,
        value: _CachedQuotaState,
    ) -> None:
        state = self._read_polling_state()
        quota_state_cache = dict(_mapping(state.get("quota_state_cache")))
        serialized = _cached_quota_state_to_mapping(value)
        if serialized is None:
            quota_state_cache.pop(cache_key, None)
        else:
            quota_state_cache[cache_key] = serialized
        state["quota_state_cache"] = quota_state_cache
        self._write_polling_state(state)

    def _prioritized_discovery_roots(
        self,
        *,
        candidate_roots: list[Path],
        workspace_activity: Mapping[str, datetime],
        polling_config: GitHubPollingConfig,
        discovery_limit: int,
    ) -> list[Path]:
        if len(candidate_roots) <= 1:
            return list(candidate_roots)
        grouped: dict[int, list[Path]] = {0: [], 1: [], 2: []}
        for root in candidate_roots:
            activity_key = _activity_sort_key(
                workspace_activity.get(str(root.resolve()))
            )
            grouped.setdefault(activity_key[0], []).append(root)
        cycle_index = int(_utc_now().timestamp()) // max(
            1, polling_config.discovery_interval_seconds
        )
        rotation_offset = cycle_index * max(1, discovery_limit)
        ordered: list[Path] = []
        for group_key in sorted(grouped):
            bucket = grouped[group_key]
            ordered.extend(_rotated(bucket, offset=rotation_offset))
        return ordered

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
        try:
            preferred_chat_sources = (
                preferred_non_pma_chat_notification_sources_by_workspace(
                    hub_root=self._hub_root,
                    raw_config=self._raw_config,
                )
            )
        except Exception:
            preferred_chat_sources = {}
        for workspace_root in preferred_chat_sources:
            add_root(Path(workspace_root))
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

    def _claim_discovery_cycle(self, *, polling_config: GitHubPollingConfig) -> bool:
        discovery_interval_seconds = max(1, polling_config.discovery_interval_seconds)
        cycle_slot = int(_utc_now().timestamp()) // discovery_interval_seconds
        state = self._read_polling_state()
        last_cycle_slot = state.get("last_discovery_cycle_slot")
        if isinstance(last_cycle_slot, int) and last_cycle_slot == cycle_slot:
            return False
        state["last_discovery_cycle_slot"] = cycle_slot
        state["last_discovery_claimed_at"] = now_iso()
        self._write_polling_state(state)
        return True


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
