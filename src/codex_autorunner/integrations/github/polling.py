from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from ...core.pr_bindings import PrBinding, PrBindingStore
from ...core.scm_events import ScmEventStore
from ...core.scm_polling_watches import ScmPollingWatch, ScmPollingWatchStore
from ...core.scm_reaction_types import ScmReactionConfig
from ...core.time_utils import now_iso

_FAILED_CHECK_CONCLUSIONS = frozenset(
    {"action_required", "cancelled", "failure", "startup_failure", "stale", "timed_out"}
)
_ACTIVE_PR_STATES = frozenset({"open", "draft"})
_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .service import GitHubService


GitHubServiceFactory = Callable[[Path, Optional[dict[str, Any]]], "GitHubService"]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normalize_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


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
    owner, repo = binding.repo_slug.split("/", 1)
    reviews = service.pr_reviews(owner=owner, repo=repo, number=binding.pr_number)
    checks = service.pr_checks(number=binding.pr_number)

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

    snapshot: dict[str, Any] = {
        "pr_state": pr_state,
        "changes_requested_reviews": changes_requested_reviews,
        "failed_checks": failed_checks,
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

    def process_due_watches(self, *, limit: int = 20) -> dict[str, int]:
        counts = {
            "due": 0,
            "polled": 0,
            "events_emitted": 0,
            "expired": 0,
            "closed": 0,
            "errors": 0,
        }
        due_watches = self._watch_store.list_due_watches(
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
        ).process_due_watches(limit=limit)

    return processor


__all__ = [
    "GitHubPollingConfig",
    "GitHubScmPollingService",
    "build_hub_scm_poll_processor",
]
