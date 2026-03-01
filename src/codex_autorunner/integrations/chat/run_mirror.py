from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core.config import load_repo_config
from ...core.flows import FlowStore
from ...core.locks import file_lock
from ...core.logging_utils import log_event
from ...core.state_roots import (
    StateRootError,
    resolve_repo_state_root,
    validate_path_within_roots,
)
from ...core.time_utils import now_iso

logger = logging.getLogger(__name__)

_ARTIFACT_KIND_BY_DIRECTION = {
    "inbound": "chat_inbound",
    "outbound": "chat_outbound",
}
_TEXT_PREVIEW_MAX_CHARS = 240
_TEXT_MAX_CHARS = 4000
_DEFAULT_KIND = "notice"
_DEFAULT_ACTOR_BY_DIRECTION = {
    "inbound": "user",
    "outbound": "car",
}


def _coerce_text_or_int(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return None


class ChatRunMirror:
    """Best-effort chat event mirroring for flow runs."""

    def __init__(self, repo_root: Path, *, logger_: Optional[logging.Logger] = None):
        self._repo_root = repo_root.resolve()
        self._repo_state_root = resolve_repo_state_root(self._repo_root).resolve()
        self._flow_root = (self._repo_state_root / "flows").resolve()
        self._logger = logger_ or logger

    def mirror_inbound(
        self,
        *,
        run_id: str,
        platform: str,
        event_type: Optional[str] = None,
        kind: Optional[str] = None,
        actor: Optional[str] = None,
        text: Optional[str] = None,
        chat_id: Optional[str | int] = None,
        thread_id: Optional[str | int] = None,
        message_id: Optional[str | int] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._mirror(
            direction="inbound",
            run_id=run_id,
            platform=platform,
            event_type=event_type,
            kind=kind,
            actor=actor,
            text=text,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            meta=meta,
        )

    def mirror_outbound(
        self,
        *,
        run_id: str,
        platform: str,
        event_type: Optional[str] = None,
        kind: Optional[str] = None,
        actor: Optional[str] = None,
        text: Optional[str] = None,
        chat_id: Optional[str | int] = None,
        thread_id: Optional[str | int] = None,
        message_id: Optional[str | int] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._mirror(
            direction="outbound",
            run_id=run_id,
            platform=platform,
            event_type=event_type,
            kind=kind,
            actor=actor,
            text=text,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            meta=meta,
        )

    def _mirror(
        self,
        *,
        direction: str,
        run_id: str,
        platform: str,
        event_type: Optional[str],
        kind: Optional[str],
        actor: Optional[str],
        text: Optional[str],
        chat_id: Optional[str | int],
        thread_id: Optional[str | int],
        message_id: Optional[str | int],
        meta: Optional[Mapping[str, Any]],
    ) -> None:
        run_id_norm = (run_id or "").strip()
        platform_norm = (platform or "").strip().lower()
        event_type_norm = (event_type or "").strip() or None
        kind_norm = (kind or "").strip().lower() or None
        if kind_norm is None:
            kind_norm = event_type_norm
        if not kind_norm:
            kind_norm = _DEFAULT_KIND
        actor_norm = (actor or "").strip().lower() or _DEFAULT_ACTOR_BY_DIRECTION.get(
            direction, "car"
        )
        if not run_id_norm or not platform_norm:
            return

        path = self._jsonl_path(run_id_norm, direction)
        if path is None:
            return

        chat_id_norm = _coerce_text_or_int(chat_id)
        thread_id_norm = _coerce_text_or_int(thread_id)
        message_id_norm = _coerce_text_or_int(message_id)
        text_raw = text if isinstance(text, str) else ""
        text_trimmed = text_raw[:_TEXT_MAX_CHARS]
        meta_payload = dict(meta) if isinstance(meta, Mapping) else {}
        meta_payload.setdefault("run_id", run_id_norm)
        if event_type_norm:
            meta_payload.setdefault("event_type", event_type_norm)

        record: dict[str, Any] = {
            "ts": now_iso(),
            "direction": direction,
            "platform": platform_norm,
            "chat_id": chat_id_norm or "",
            "thread_id": thread_id_norm,
            "message_id": message_id_norm,
            "actor": actor_norm,
            "kind": kind_norm,
            "text": text_trimmed,
            "meta": meta_payload,
            # Compatibility fields retained for current readers.
            "run_id": run_id_norm,
            "event_type": event_type_norm or kind_norm,
            "text_preview": text_trimmed[:_TEXT_PREVIEW_MAX_CHARS],
            "text_bytes": len(text_raw.encode("utf-8")),
        }

        try:
            self._append_jsonl(path, record)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "chat.run_mirror.append_failed",
                run_id=run_id_norm,
                direction=direction,
                path=str(path),
                exc=exc,
            )
            return

        try:
            self._ensure_artifact(run_id_norm, direction, path)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "chat.run_mirror.artifact_failed",
                run_id=run_id_norm,
                direction=direction,
                path=str(path),
                exc=exc,
            )

    def _jsonl_path(self, run_id: str, direction: str) -> Optional[Path]:
        path = (self._flow_root / run_id / "chat" / f"{direction}.jsonl").resolve()
        try:
            validate_path_within_roots(path, allowed_roots=[self._flow_root])
        except StateRootError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "chat.run_mirror.path_outside_root",
                run_id=run_id,
                direction=direction,
                path=str(path),
                allowed_root=str(self._flow_root),
                exc=exc,
            )
            return None
        return path

    def _append_jsonl(self, path: Path, record: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with file_lock(lock_path):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(record), sort_keys=True) + "\n")

    def _ensure_artifact(self, run_id: str, direction: str, path: Path) -> None:
        kind = _ARTIFACT_KIND_BY_DIRECTION.get(direction)
        if kind is None:
            return

        artifact_rel_path = path
        try:
            artifact_rel_path = path.relative_to(self._repo_root)
        except ValueError:
            artifact_rel_path = path

        store = self._open_flow_store()
        try:
            existing = store.get_artifacts(run_id)
            if any(artifact.kind == kind for artifact in existing):
                return
            store.create_artifact(
                artifact_id=f"{run_id}:{kind}",
                run_id=run_id,
                kind=kind,
                path=artifact_rel_path.as_posix(),
                metadata={"direction": direction},
            )
        finally:
            store.close()

    def _open_flow_store(self) -> FlowStore:
        config = load_repo_config(self._repo_root)
        store = FlowStore(
            self._repo_root / ".codex-autorunner" / "flows.db",
            durable=config.durable_writes,
        )
        store.initialize()
        return store
