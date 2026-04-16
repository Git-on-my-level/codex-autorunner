from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from ...core.locks import file_lock
from ...core.orchestration.sqlite import open_orchestration_sqlite
from ...core.pma_thread_store import PmaThreadStore
from ...core.pr_binding_runtime import (
    backfill_pr_binding_thread_target_ids,
)
from ...core.pr_bindings import PrBinding, PrBindingStore
from ...core.scm_events import ScmEventStore
from ...core.scm_polling_watches import ScmPollingWatch, ScmPollingWatchStore
from ...core.scm_reaction_types import ScmReactionConfig
from ...core.text_utils import _mapping, _normalize_text, lock_path_for
from ...core.time_utils import now_iso
from ...core.utils import atomic_write, read_json
from .polling_discovery import (
    binding_from_polling_row,
    collect_candidate_workspace_roots,
    compute_activity_tier,
    compute_poll_interval_for_tier,
    prioritized_discovery_roots,
    resolve_workspace_root_for_binding,
    thread_has_pr_open_hint,
)
from .polling_discovery import (
    compute_thread_activity as _compute_thread_activity,
)
from .polling_events import emit_comment_backfill, emit_new_conditions
from .polling_quota import (
    CachedQuotaState,
    GitHubQuotaState,
    cached_quota_state_from_mapping,
    cached_quota_state_to_mapping,
    quota_state_cache_expiry,
    quota_state_from_payload,
    rate_limit_backoff_until,
)
from .polling_snapshot import (
    build_snapshot,
    initial_post_open_boost_until,
    snapshot_with_polling_metadata,
)

_thread_has_pr_open_hint = thread_has_pr_open_hint
_ACTIVE_PR_STATES = frozenset({"open", "draft"})
_VALID_PR_STATES = frozenset({"open", "draft", "closed", "merged"})
_TERMINAL_PR_STATES = frozenset({"closed", "merged"})
_ACTIVITY_PRIORITY = {"hot": 0, "warm": 1, "cold": 2}
_VALID_ACTIVITY_TIERS = frozenset(_ACTIVITY_PRIORITY.keys())
_DEFAULT_NO_ACTIVITY_TIER = "cold"
_DEFAULT_DISCOVERY_TERMINAL_LOOKBACK_MINUTES = 24 * 60
_DEFAULT_POST_OPEN_BOOST_MINUTES = 30
_DEFAULT_POST_OPEN_BOOST_INTERVAL_SECONDS = 30
_POST_OPEN_BOOST_UNTIL_SNAPSHOT_KEY = "post_open_boost_until"
_RATE_LIMIT_BACKOFF_SECONDS = 15 * 60
_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .service import GitHubService


GitHubServiceFactory = Callable[[Path, Optional[dict[str, Any]]], "GitHubService"]


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


def _parse_optional_iso(value: Any) -> Optional[datetime]:
    normalized = _normalize_text(value)
    if normalized is None:
        return None
    try:
        return _parse_iso(normalized)
    except ValueError:
        return None


def _normalize_positive_int(value: Any) -> Optional[int]:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _normalize_lower_text(value: Any) -> Optional[str]:
    text = _normalize_text(value)
    return text.lower() if text is not None else None


def _is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    return "rate limit" in str(exc).lower()


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
    post_open_boost_minutes: int = _DEFAULT_POST_OPEN_BOOST_MINUTES
    post_open_boost_interval_seconds: int = _DEFAULT_POST_OPEN_BOOST_INTERVAL_SECONDS
    discovery_interval_seconds: int = 6 * 60
    discovery_workspace_limit: int = 1
    discovery_include_manifest_repos: bool = False
    discovery_terminal_thread_lookback_minutes: int = (
        _DEFAULT_DISCOVERY_TERMINAL_LOOKBACK_MINUTES
    )
    no_activity_tier: str = _DEFAULT_NO_ACTIVITY_TIER

    @classmethod
    def from_mapping(cls, raw_config: object) -> "GitHubPollingConfig":
        github = _mapping(raw_config).get("github")
        automation = _mapping(github).get("automation")
        polling = _mapping(_mapping(automation).get("polling"))
        enabled = polling.get("enabled")
        watch_window_minutes = polling.get("watch_window_minutes")
        interval_seconds = polling.get("interval_seconds")
        post_open_boost_minutes = polling.get("post_open_boost_minutes")
        post_open_boost_interval_seconds = polling.get(
            "post_open_boost_interval_seconds"
        )
        discovery_interval_seconds = polling.get("discovery_interval_seconds")
        discovery_workspace_limit = polling.get("discovery_workspace_limit")
        discovery_include_manifest_repos = polling.get(
            "discovery_include_manifest_repos"
        )
        discovery_terminal_thread_lookback_minutes = polling.get(
            "discovery_terminal_thread_lookback_minutes"
        )
        no_activity_tier = _normalize_lower_text(polling.get("no_activity_tier"))
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
            post_open_boost_minutes=(
                int(post_open_boost_minutes)
                if (
                    isinstance(post_open_boost_minutes, int)
                    and post_open_boost_minutes >= 0
                )
                else _DEFAULT_POST_OPEN_BOOST_MINUTES
            ),
            post_open_boost_interval_seconds=(
                int(post_open_boost_interval_seconds)
                if (
                    isinstance(post_open_boost_interval_seconds, int)
                    and post_open_boost_interval_seconds >= 0
                )
                else _DEFAULT_POST_OPEN_BOOST_INTERVAL_SECONDS
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
            discovery_include_manifest_repos=(
                bool(discovery_include_manifest_repos)
                if isinstance(discovery_include_manifest_repos, bool)
                else False
            ),
            discovery_terminal_thread_lookback_minutes=(
                int(discovery_terminal_thread_lookback_minutes)
                if (
                    not isinstance(discovery_terminal_thread_lookback_minutes, bool)
                    and isinstance(discovery_terminal_thread_lookback_minutes, int)
                    and discovery_terminal_thread_lookback_minutes > 0
                )
                else _DEFAULT_DISCOVERY_TERMINAL_LOOKBACK_MINUTES
            ),
            no_activity_tier=(
                no_activity_tier
                if no_activity_tier in _VALID_ACTIVITY_TIERS
                else _DEFAULT_NO_ACTIVITY_TIER
            ),
        )

    @property
    def comment_backfill_window_seconds(self) -> int:
        return self.watch_window_minutes * 60


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

    def _build_automation_service(
        self,
        *,
        reaction_config: Mapping[str, Any] | None,
    ):
        from ...core.scm_automation_service import ScmAutomationService

        return ScmAutomationService(
            self._hub_root,
            reaction_config=reaction_config or self._raw_config,
            schedule_deferred_publish_drain=True,
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
        schedule_forced_by_rate_limit = False
        snapshot: dict[str, Any] = {"baseline_pending": True}
        if establish_baseline:
            try:
                github = self._github_service_factory(
                    workspace_root,
                    self._raw_config if isinstance(self._raw_config, dict) else None,
                )
                snapshot = build_snapshot(binding=binding, service=github)
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
                schedule_forced_by_rate_limit = _is_rate_limit_error(exc)
        post_open_boost = initial_post_open_boost_until(
            binding=binding,
            snapshot=snapshot,
            polling_config=polling_config,
            now=_utc_now(),
            parse_optional_iso=_parse_optional_iso,
            iso_after_seconds=_iso_after_seconds,
        )
        snapshot = snapshot_with_polling_metadata(
            snapshot=snapshot,
            post_open_boost_until=post_open_boost,
        )
        boost_interval_seconds = polling_config.post_open_boost_interval_seconds
        if (
            not schedule_forced_by_rate_limit
            and post_open_boost is not None
            and boost_interval_seconds > 0
            and _parse_iso(post_open_boost) > _utc_now()
        ):
            boosted_next_poll_at = _iso_after_seconds(boost_interval_seconds)
            current_next_poll_at = _parse_optional_iso(scheduled_next_poll_at)
            if (
                current_next_poll_at is None
                or _parse_iso(boosted_next_poll_at) < current_next_poll_at
            ):
                scheduled_next_poll_at = boosted_next_poll_at

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
            emit_comment_backfill(
                event_store=self._event_store,
                watch=watch,
                binding=binding,
                snapshot=snapshot,
                reference_timestamp=now_timestamp,
                window_seconds=polling_config.comment_backfill_window_seconds,
                automation_service_factory=lambda: self._build_automation_service(
                    reaction_config=watch.reaction_config or self._raw_config,
                ),
                parse_optional_iso=_parse_optional_iso,
                now_iso_fn=now_iso,
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

        (
            candidate_roots,
            workspaces_by_repo_id,
            workspaces_by_thread_id,
            workspace_branch_hints,
        ) = self._candidate_workspace_roots()
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
            prioritized = prioritized_discovery_roots(
                candidate_roots=candidate_roots,
                workspace_activity=workspace_activity,
                discovery_interval_seconds=polling_config.discovery_interval_seconds,
                discovery_limit=discovery_limit,
                now=_utc_now(),
            )
            for workspace_root in prioritized[:discovery_limit]:
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
                    binding = github.discover_pr_binding(
                        branch=workspace_branch_hints.get(workspace_root),
                        cwd=workspace_root,
                    )
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
        quota_state_cache: dict[str, Optional[GitHubQuotaState]] = {}
        for binding in active_bindings.values():
            watch = self._watch_store.get_watch(
                provider="github",
                binding_id=binding.binding_id,
            )

            resolved_workspace_root = resolve_workspace_root_for_binding(
                binding=binding,
                existing_watch=watch,
                candidate_roots=candidate_roots,
                workspaces_by_repo_id=workspaces_by_repo_id,
                workspaces_by_thread_id=workspaces_by_thread_id,
                repo_slug_cache=repo_slug_cache,
                github_service_factory=self._github_service_factory,
                raw_config=self._raw_config,
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
            activity_tier = compute_activity_tier(
                binding=binding,
                workspace_root=resolved_workspace_root,
                watch=watch,
                thread_activity_by_thread=thread_activity_by_thread,
                workspace_activity=workspace_activity,
                no_activity_tier=polling_config.no_activity_tier,
                now=_utc_now(),
            )
            scheduled_next_poll_at = _iso_after_seconds(
                compute_poll_interval_for_tier(
                    activity_tier=activity_tier,
                    base_interval_seconds=polling_config.interval_seconds,
                )
            )
            quota_state: Optional[GitHubQuotaState] = None
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
                                next_poll_at=rate_limit_backoff_until(
                                    quota_state,
                                    now=_utc_now(),
                                    parse_iso_fn=_parse_iso,
                                    iso_after_seconds_fn=_iso_after_seconds,
                                ),
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
            activity_tier = compute_activity_tier(
                binding=binding,
                workspace_root=workspace_root,
                watch=watch,
                thread_activity_by_thread=thread_activity_by_thread,
                workspace_activity=workspace_activity,
                no_activity_tier=polling_config.no_activity_tier,
                now=_utc_now(),
            )
            pending_watches.append((activity_tier, watch, binding, workspace_root))

        pending_watches.sort(
            key=lambda item: (
                _ACTIVITY_PRIORITY.get(item[0], 1),
                item[1].next_poll_at,
                item[1].watch_id,
            )
        )
        quota_state_cache: dict[str, Optional[GitHubQuotaState]] = {}
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
                    next_poll_at=rate_limit_backoff_until(
                        quota_state,
                        now=_utc_now(),
                        parse_iso_fn=_parse_iso,
                        iso_after_seconds_fn=_iso_after_seconds,
                    ),
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
                snapshot = build_snapshot(binding=binding, service=github)
            except Exception as exc:
                if _is_rate_limit_error(exc):
                    self._invalidate_quota_state_cache()
                    self._watch_store.refresh_watch(
                        watch_id=watch.watch_id,
                        next_poll_at=rate_limit_backoff_until(
                            quota_state,
                            now=_utc_now(),
                            parse_iso_fn=_parse_iso,
                            iso_after_seconds_fn=_iso_after_seconds,
                        ),
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

            terminal_pr_state = _normalize_lower_text(snapshot.get("pr_state"))
            if terminal_pr_state not in _ACTIVE_PR_STATES:
                self._watch_store.close_watch(watch_id=watch.watch_id, state="closed")
                if terminal_pr_state in _TERMINAL_PR_STATES:
                    binding_store.close_binding(
                        provider=binding.provider,
                        repo_slug=binding.repo_slug,
                        pr_number=binding.pr_number,
                        pr_state=terminal_pr_state,
                    )
                counts["closed"] += 1
                continue

            previous_snapshot = (
                watch.snapshot if isinstance(watch.snapshot, dict) else {}
            )
            baseline_pending = bool(previous_snapshot.get("baseline_pending"))
            _watch_ref = watch

            def _make_automation(
                _w: ScmPollingWatch = _watch_ref,
            ) -> Any:
                return self._build_automation_service(
                    reaction_config=_w.reaction_config or self._raw_config,
                )

            emitted = 0
            if baseline_pending:
                baseline_reference_timestamp = now_iso()
                emitted += emit_comment_backfill(
                    event_store=self._event_store,
                    watch=watch,
                    binding=binding,
                    snapshot=snapshot,
                    reference_timestamp=baseline_reference_timestamp,
                    window_seconds=polling_config.comment_backfill_window_seconds,
                    automation_service_factory=_make_automation,
                    parse_optional_iso=_parse_optional_iso,
                    now_iso_fn=now_iso,
                )
            else:
                emitted += emit_new_conditions(
                    event_store=self._event_store,
                    watch=watch,
                    binding=binding,
                    previous_snapshot=previous_snapshot,
                    snapshot=snapshot,
                    automation_service_factory=_make_automation,
                    now_iso_fn=now_iso,
                )
            post_open_boost = _normalize_text(
                _mapping(previous_snapshot).get(_POST_OPEN_BOOST_UNTIL_SNAPSHOT_KEY)
            )
            if post_open_boost is None:
                post_open_boost = initial_post_open_boost_until(
                    binding=binding,
                    snapshot=snapshot,
                    polling_config=polling_config,
                    now=_utc_now(),
                    parse_optional_iso=_parse_optional_iso,
                    iso_after_seconds=_iso_after_seconds,
                )
            snapshot = snapshot_with_polling_metadata(
                snapshot=snapshot,
                previous_snapshot=previous_snapshot,
                post_open_boost_until=post_open_boost,
            )

            self._watch_store.refresh_watch(
                watch_id=watch.watch_id,
                snapshot=snapshot,
                next_poll_at=_iso_after_seconds(
                    self._poll_interval_for_watch(
                        activity_tier=activity_tier,
                        polling_config=polling_config,
                        snapshot=snapshot,
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
            "GitHub SCM poll cycles: scanned=%s/%s discovered=%s armed=%s "
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
        polling_config = GitHubPollingConfig.from_mapping(self._raw_config)
        return backfill_pr_binding_thread_target_ids(
            self._hub_root,
            limit=limit,
            include_recent_terminal_threads=True,
            terminal_thread_lookback=timedelta(
                minutes=max(
                    1,
                    polling_config.discovery_terminal_thread_lookback_minutes,
                )
            ),
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
                binding = binding_from_polling_row(row)
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
        try:
            threads = PmaThreadStore(self._hub_root).list_threads(
                status="active",
                limit=1000,
            )
        except Exception:
            return {}, {}
        return _compute_thread_activity(
            threads,
            parse_optional_iso=_parse_optional_iso,
        )

    def _post_open_boost_interval_for_snapshot(
        self,
        *,
        snapshot: Mapping[str, Any],
        polling_config: GitHubPollingConfig,
    ) -> Optional[int]:
        boost_interval_seconds = polling_config.post_open_boost_interval_seconds
        if boost_interval_seconds <= 0:
            return None
        boost_until = _parse_optional_iso(
            _mapping(snapshot).get(_POST_OPEN_BOOST_UNTIL_SNAPSHOT_KEY)
        )
        if boost_until is None or boost_until <= _utc_now():
            return None
        return boost_interval_seconds

    def _poll_interval_for_watch(
        self,
        *,
        activity_tier: str,
        polling_config: GitHubPollingConfig,
        snapshot: Mapping[str, Any],
    ) -> int:
        tier_interval = compute_poll_interval_for_tier(
            activity_tier=activity_tier,
            base_interval_seconds=polling_config.interval_seconds,
        )
        boost_interval = self._post_open_boost_interval_for_snapshot(
            snapshot=snapshot,
            polling_config=polling_config,
        )
        if boost_interval is None:
            return tier_interval
        return min(tier_interval, boost_interval)

    def _quota_state_for_workspace(
        self,
        *,
        workspace_root: Path,
        cache: dict[str, Optional[GitHubQuotaState]],
    ) -> Optional[GitHubQuotaState]:
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
            cache[cache_key] = quota_state_from_payload(github.rate_limit_status())
        except Exception:
            cache[cache_key] = None
        self._write_cached_quota_state(
            cache_key=cache_key,
            value=CachedQuotaState(
                value=cache[cache_key],
                expires_at=quota_state_cache_expiry(
                    cache[cache_key],
                    now=now,
                    parse_iso_fn=_parse_iso,
                ),
            ),
        )
        return cache[cache_key]

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

    def _read_cached_quota_state(self, *, cache_key: str) -> Optional[CachedQuotaState]:
        state = self._read_polling_state()
        quota_state_cache = _mapping(state.get("quota_state_cache"))
        return cached_quota_state_from_mapping(quota_state_cache.get(cache_key))

    def _write_cached_quota_state(
        self,
        *,
        cache_key: str,
        value: CachedQuotaState,
    ) -> None:
        state = self._read_polling_state()
        quota_state_cache = dict(_mapping(state.get("quota_state_cache")))
        serialized = cached_quota_state_to_mapping(value)
        if serialized is None:
            quota_state_cache.pop(cache_key, None)
        else:
            quota_state_cache[cache_key] = serialized
        state["quota_state_cache"] = quota_state_cache
        self._write_polling_state(state)

    def _candidate_workspace_roots(
        self,
    ) -> tuple[list[Path], dict[str, list[Path]], dict[str, Path], dict[Path, str]]:
        polling_config = GitHubPollingConfig.from_mapping(self._raw_config)
        return collect_candidate_workspace_roots(
            hub_root=self._hub_root,
            raw_config=self._raw_config,
            active_watch_workspace_candidates_fn=self._active_watch_workspace_candidates,
            active_bindings_fn=self._active_bindings,
            now=_utc_now(),
            parse_optional_iso=_parse_optional_iso,
            polling_config=polling_config,
        )

    def _active_watch_workspace_candidates(
        self,
        *,
        limit: int = 1000,
    ) -> list[tuple[str, Optional[str], Optional[str]]]:
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            rows = conn.execute(
                """
                SELECT workspace_root, repo_id, thread_target_id
                  FROM orch_scm_polling_watches
                 WHERE provider = ?
                   AND state = 'active'
                 ORDER BY updated_at DESC, started_at DESC, watch_id DESC
                 LIMIT ?
                """,
                ("github", max(1, int(limit))),
            ).fetchall()
        return [
            (
                str(row["workspace_root"] or ""),
                _normalize_text(row["repo_id"]),
                _normalize_text(row["thread_target_id"]),
            )
            for row in rows
        ]

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

    def _claim_discovery_cycle(self, *, polling_config: GitHubPollingConfig) -> bool:
        discovery_interval_seconds = max(1, polling_config.discovery_interval_seconds)
        cycle_slot = int(_utc_now().timestamp()) // discovery_interval_seconds
        with file_lock(lock_path_for(self._polling_state_path)):
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
