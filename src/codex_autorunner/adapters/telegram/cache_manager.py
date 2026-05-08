from __future__ import annotations

import asyncio
import time
from typing import Any


class TelegramCacheManager:
    def __init__(self, *, logger: Any, config: Any) -> None:
        self._logger = logger
        self._config = config
        self._cache_timestamps: dict[str, dict[object, float]] = {}

    def touch(self, cache_name: str, key: object) -> None:
        cache = self._cache_timestamps.setdefault(cache_name, {})
        cache[key] = time.monotonic()

    def evict_expired(
        self,
        cache_name: str,
        ttl_seconds: float,
        *,
        state: Any,
    ) -> None:
        cache = self._cache_timestamps.get(cache_name)
        if not cache:
            return
        now = time.monotonic()
        expired: list[object] = []
        for key, updated_at in cache.items():
            if (now - updated_at) > ttl_seconds:
                expired.append(key)
        if not expired:
            return
        for key in expired:
            cache.pop(key, None)
            self._evict_one(cache_name, key, state=state)

    def _evict_one(self, cache_name: str, key: object, *, state: Any) -> None:
        ui_state = getattr(state, "_ui_state", None)
        if ui_state is not None and ui_state.evict_cache_entry(cache_name, key):
            return
        if cache_name == "reasoning_buffers":
            state._reasoning_buffers.pop(key, None)
        elif cache_name == "turn_preview":
            state._turn_preview_text.pop(key, None)
            state._turn_preview_updated_at.pop(key, None)
        elif cache_name == "progress_trackers":
            state._turn_progress_trackers.pop(key, None)
            state._turn_progress_rendered.pop(key, None)
            state._turn_progress_final_rendered.pop(key, None)
            state._turn_progress_final_summary.pop(key, None)
            state._turn_progress_updated_at.pop(key, None)
            state._turn_progress_backoff_until.pop(key, None)
            state._turn_progress_failure_streaks.pop(key, None)
            state._turn_progress_suppressed_counts.pop(key, None)
            task = state._turn_progress_tasks.pop(key, None)
            if task and not task.done():
                task.cancel()
            heartbeat_task = state._turn_progress_heartbeat_tasks.pop(key, None)
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
        elif cache_name == "oversize_warnings":
            state._oversize_warnings.discard(key)
        elif cache_name == "coalesced_buffers":
            state._coalesced_buffers.pop(key, None)
            state._coalesce_locks.pop(key, None)
        elif cache_name == "media_batch_buffers":
            state._media_batch_buffers.pop(key, None)
            state._media_batch_locks.pop(key, None)
        elif cache_name == "pending_approvals":
            state._pending_approvals.pop(key, None)

    async def cleanup_loop(self, *, state: Any) -> None:
        interval = max(self._config.cache.cleanup_interval_seconds, 1.0)
        while True:
            await asyncio.sleep(interval)
            cfg = self._config.cache
            self.evict_expired(
                "reasoning_buffers", cfg.reasoning_buffer_ttl_seconds, state=state
            )
            self.evict_expired(
                "turn_preview", cfg.turn_preview_ttl_seconds, state=state
            )
            self.evict_expired(
                "progress_trackers", cfg.progress_stream_ttl_seconds, state=state
            )
            self.evict_expired(
                "oversize_warnings", cfg.oversize_warning_ttl_seconds, state=state
            )
            self.evict_expired(
                "coalesced_buffers", cfg.coalesce_buffer_ttl_seconds, state=state
            )
            self.evict_expired(
                "media_batch_buffers", cfg.media_batch_buffer_ttl_seconds, state=state
            )
            self.evict_expired(
                "resume_options", cfg.selection_state_ttl_seconds, state=state
            )
            self.evict_expired(
                "bind_options", cfg.selection_state_ttl_seconds, state=state
            )
            self.evict_expired(
                "flow_run_options", cfg.selection_state_ttl_seconds, state=state
            )
            self.evict_expired(
                "agent_options", cfg.selection_state_ttl_seconds, state=state
            )
            self.evict_expired(
                "agent_profile_options", cfg.selection_state_ttl_seconds, state=state
            )
            self.evict_expired(
                "update_options", cfg.selection_state_ttl_seconds, state=state
            )
            self.evict_expired(
                "update_confirm_options", cfg.selection_state_ttl_seconds, state=state
            )
            self.evict_expired(
                "review_commit_options", cfg.selection_state_ttl_seconds, state=state
            )
            self.evict_expired(
                "review_commit_subjects", cfg.selection_state_ttl_seconds, state=state
            )
            self.evict_expired(
                "pending_review_custom", cfg.selection_state_ttl_seconds, state=state
            )
            self.evict_expired(
                "compact_pending", cfg.selection_state_ttl_seconds, state=state
            )
            self.evict_expired(
                "model_options", cfg.selection_state_ttl_seconds, state=state
            )
            self.evict_expired(
                "model_pending", cfg.model_pending_ttl_seconds, state=state
            )
            self.evict_expired(
                "document_browser_states",
                cfg.selection_state_ttl_seconds,
                state=state,
            )
            self.evict_expired(
                "pending_approvals", cfg.pending_approval_ttl_seconds, state=state
            )
            self.evict_expired(
                "pending_questions", cfg.pending_question_ttl_seconds, state=state
            )
            now = time.monotonic()
            expired_placeholders = []
            for key, timestamp in state._queued_placeholder_timestamps.items():
                if (now - timestamp) > cfg.pending_approval_ttl_seconds:
                    expired_placeholders.append(key)
            for key in expired_placeholders:
                state._queued_placeholder_map.pop(key, None)
                state._queued_placeholder_timestamps.pop(key, None)
