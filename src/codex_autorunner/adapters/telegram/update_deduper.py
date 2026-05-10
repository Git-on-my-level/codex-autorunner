from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from ...core.logging_utils import log_event


class TelegramUpdateDeduper:
    def __init__(
        self,
        *,
        logger: logging.Logger,
        now_func: Optional[Callable[[], float]] = None,
    ) -> None:
        self._logger = logger
        self._now = now_func or time.monotonic
        self._last_update_ids: dict[str, int] = {}
        self._last_persisted_at: dict[str, float] = {}

    async def should_process(
        self,
        key: str,
        update_id: int,
        *,
        store: Any,
        persist_interval: float,
    ) -> bool:
        if not isinstance(update_id, int):
            return True
        if isinstance(update_id, bool):
            return True
        last_id = self._last_update_ids.get(key)
        if last_id is None:
            record = None
            try:
                record = await store.get_topic(key)
            except (OSError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.update_id.load.failed",
                    exc=exc,
                    topic_key=key,
                    update_id=update_id,
                )
            last_id = record.last_update_id if record else None
            if isinstance(last_id, int) and not isinstance(last_id, bool):
                self._last_update_ids[key] = last_id
            else:
                last_id = None
        if isinstance(last_id, int) and update_id <= last_id:
            return False
        self._last_update_ids[key] = update_id
        try:
            await self._maybe_persist(
                key, update_id, store=store, persist_interval=persist_interval
            )
        except (OSError, ValueError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.update_id.persist.failed",
                exc=exc,
                topic_key=key,
                update_id=update_id,
            )
        return True

    async def _maybe_persist(
        self,
        key: str,
        update_id: int,
        *,
        store: Any,
        persist_interval: float,
    ) -> None:
        now = self._now()
        last_persisted = self._last_persisted_at.get(key, 0.0)
        if (now - last_persisted) < persist_interval:
            return

        def apply(record: Any) -> None:
            record.last_update_id = update_id

        await store.update_topic(key, apply)
        self._last_persisted_at[key] = now
