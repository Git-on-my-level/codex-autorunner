from __future__ import annotations

import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .....core.filebox import delete_regular_files, list_regular_files
from .....core.logging_utils import log_event
from ....chat.constants import TOPIC_NOT_BOUND_MESSAGE
from ...adapter import TelegramAPIError, TelegramMessage
from ...helpers import _path_within
from ...state import TelegramTopicRecord
from ..media_ingress import record_with_media_workspace as _record_with_media_workspace


class FileBoxCommandsMixin:
    def _files_usage(self, *, pma: bool) -> str:
        header = "Usage:"
        lines = [
            header,
            "/files",
            "/files inbox",
            "/files outbox",
            "/files all",
            "/files send <filename>",
            "/files clear inbox|outbox|all",
        ]
        if pma:
            lines.append(
                "Note: PMA files live in .codex-autorunner/filebox/inbox|outbox."
            )
        return "\n".join(lines)

    async def _send_pma_outbox_file(
        self,
        path: Path,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> bool:
        try:
            data = path.read_bytes()
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.pma_outbox.read_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        try:
            await self._bot.send_document(
                chat_id,
                data,
                filename=path.name,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
            )
        except TelegramAPIError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.pma_outbox.send_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        return True

    def _format_bytes(self, size: int) -> str:
        if size < 1024:
            return f"{size} B"
        value = size / 1024
        for unit in ("KB", "MB", "GB", "TB"):
            if value < 1024:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} PB"

    def _list_files(self, folder: Path) -> list[Path]:
        return list_regular_files(folder)

    async def _send_outbox_file(
        self,
        path: Path,
        *,
        sent_dir: Path,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> bool:
        try:
            data = path.read_bytes()
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.outbox.read_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        try:
            await self._bot.send_document(
                chat_id,
                data,
                filename=path.name,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
            )
        except TelegramAPIError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.outbox.send_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        try:
            sent_dir.mkdir(parents=True, exist_ok=True)
            destination = sent_dir / path.name
            if destination.exists():
                token = secrets.token_hex(3)
                destination = sent_dir / f"{path.stem}-{token}{path.suffix}"
            path.replace(destination)
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.outbox.move_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        log_event(
            self._logger,
            logging.INFO,
            "telegram.files.outbox.sent",
            chat_id=chat_id,
            thread_id=thread_id,
            path=str(path),
        )
        return True

    async def _flush_outbox_files(
        self,
        record: Optional[TelegramTopicRecord],
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        topic_key: Optional[str] = None,
    ) -> None:
        if (
            record is None
            or not record.workspace_path
            or not self._config.media.enabled
            or not self._config.media.files
        ):
            return
        if topic_key:
            key = topic_key
        else:
            key = await self._resolve_topic_key(chat_id, thread_id)
        pma_enabled = bool(getattr(record, "pma_enabled", False))
        pending_dir = self._files_outbox_pending_dir(record.workspace_path, key)
        if pma_enabled:
            pma_outbox = self._pma_outbox_dir()
            if pma_outbox is not None:
                pending_dir = pma_outbox
        if not pending_dir.exists():
            return
        files = self._list_files(pending_dir)
        if not files:
            return
        sent_dir = self._files_outbox_sent_dir(record.workspace_path, key)
        max_bytes = self._config.media.max_file_bytes
        for path in files:
            if not _path_within(root=pending_dir, target=path):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > max_bytes:
                await self._send_message(
                    chat_id,
                    f"Outbox file too large: {path.name} (max {max_bytes} bytes).",
                    thread_id=thread_id,
                    reply_to=reply_to,
                )
                continue
            if pma_enabled:
                success = await self._send_pma_outbox_file(
                    path,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to=reply_to,
                )
                if success:
                    try:
                        path.unlink()
                    except OSError as exc:
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "telegram.files.pma_outbox.delete_failed",
                            chat_id=chat_id,
                            thread_id=thread_id,
                            path=str(path),
                            exc=exc,
                        )
            else:
                await self._send_outbox_file(
                    path,
                    sent_dir=sent_dir,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to=reply_to,
                )

    def _format_file_listing(self, title: str, files: list[Path]) -> str:
        if not files:
            return f"{title}: (empty)"
        lines = [f"{title} ({len(files)}):"]
        for path in files[:50]:
            try:
                stats = path.stat()
            except OSError:
                continue
            mtime = datetime.fromtimestamp(stats.st_mtime).isoformat(timespec="seconds")
            lines.append(
                f"- {path.name} ({self._format_bytes(stats.st_size)}, {mtime})"
            )
        if len(files) > 50:
            lines.append(f"... and {len(files) - 50} more")
        return "\n".join(lines)

    def _delete_files_in_dir(self, folder: Path) -> int:
        return delete_regular_files(folder)

    async def _handle_files(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        if not self._config.media.enabled or not self._config.media.files:
            await self._send_message(
                message.chat_id,
                "File handling is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.ensure_topic(message.chat_id, message.thread_id)
        record, pma_error = _record_with_media_workspace(self, record)
        if pma_error:
            await self._send_message(
                message.chat_id,
                pma_error,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                self._with_conversation_id(
                    TOPIC_NOT_BOUND_MESSAGE,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        pma_enabled = bool(getattr(record, "pma_enabled", False))
        if pma_enabled:
            inbox_dir = self._pma_inbox_dir()
            pending_dir = self._pma_outbox_dir()
            sent_dir = pending_dir
            if inbox_dir is None or pending_dir is None:
                await self._send_message(
                    message.chat_id,
                    "PMA unavailable; hub root not configured.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
        else:
            inbox_dir = self._files_inbox_dir(record.workspace_path, key)
            pending_dir = self._files_outbox_pending_dir(record.workspace_path, key)
            sent_dir = self._files_outbox_sent_dir(record.workspace_path, key)
        argv = self._parse_command_args(args)
        if not argv:
            inbox_items = self._list_files(inbox_dir)
            pending_items = self._list_files(pending_dir)
            sent_items = [] if pma_enabled else self._list_files(sent_dir)
            usage = self._files_usage(pma=pma_enabled)
            if pma_enabled:
                text = "\n".join(
                    [
                        f"Inbox: {len(inbox_items)} file(s)",
                        f"Outbox: {len(pending_items)} file(s)",
                        usage,
                    ]
                )
            else:
                text = "\n".join(
                    [
                        f"Inbox: {len(inbox_items)} item(s)",
                        f"Outbox pending: {len(pending_items)} item(s)",
                        f"Outbox sent: {len(sent_items)} item(s)",
                        usage,
                    ]
                )
            await self._send_message(
                message.chat_id,
                text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        subcommand = argv[0].lower()
        if subcommand == "inbox":
            files = self._list_files(inbox_dir)
            text = self._format_file_listing("Inbox", files)
            await self._send_message(
                message.chat_id,
                text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if subcommand == "outbox":
            pending_items = self._list_files(pending_dir)
            if pma_enabled:
                text = self._format_file_listing("Outbox", pending_items)
            else:
                sent_items = self._list_files(sent_dir)
                text = "\n".join(
                    [
                        self._format_file_listing("Outbox pending", pending_items),
                        "",
                        self._format_file_listing("Outbox sent", sent_items),
                    ]
                )
            await self._send_message(
                message.chat_id,
                text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if subcommand == "all":
            inbox_items = self._list_files(inbox_dir)
            pending_items = self._list_files(pending_dir)
            if pma_enabled:
                text = "\n".join(
                    [
                        self._format_file_listing("Inbox", inbox_items),
                        "",
                        self._format_file_listing("Outbox", pending_items),
                    ]
                )
            else:
                sent_items = self._list_files(sent_dir)
                text = "\n".join(
                    [
                        self._format_file_listing("Inbox", inbox_items),
                        "",
                        self._format_file_listing("Outbox pending", pending_items),
                        "",
                        self._format_file_listing("Outbox sent", sent_items),
                    ]
                )
            await self._send_message(
                message.chat_id,
                text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if subcommand == "clear":
            if len(argv) < 2:
                await self._send_message(
                    message.chat_id,
                    self._files_usage(pma=pma_enabled),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            target = argv[1].lower()
            deleted = 0
            if target == "inbox":
                deleted = self._delete_files_in_dir(inbox_dir)
            elif target == "outbox":
                deleted = self._delete_files_in_dir(pending_dir)
                if not pma_enabled:
                    deleted += self._delete_files_in_dir(sent_dir)
            elif target == "all":
                deleted = self._delete_files_in_dir(inbox_dir)
                deleted += self._delete_files_in_dir(pending_dir)
                if not pma_enabled:
                    deleted += self._delete_files_in_dir(sent_dir)
            else:
                await self._send_message(
                    message.chat_id,
                    self._files_usage(pma=pma_enabled),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                f"Deleted {deleted} file(s).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if subcommand == "send":
            if len(argv) < 2:
                await self._send_message(
                    message.chat_id,
                    self._files_usage(pma=pma_enabled),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            name = Path(argv[1]).name
            candidate = pending_dir / name
            if (
                not _path_within(root=pending_dir, target=candidate)
                or not candidate.is_file()
            ):
                await self._send_message(
                    message.chat_id,
                    f"Outbox file not found: {name}",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            size = candidate.stat().st_size
            max_bytes = self._config.media.max_file_bytes
            if size > max_bytes:
                await self._send_message(
                    message.chat_id,
                    f"Outbox file too large: {name} (max {max_bytes} bytes).",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if pma_enabled:
                success = await self._send_pma_outbox_file(
                    candidate,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
            else:
                success = await self._send_outbox_file(
                    candidate,
                    sent_dir=sent_dir,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
            result = "Sent." if success else "Failed to send."
            await self._send_message(
                message.chat_id,
                result,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            self._files_usage(pma=pma_enabled),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
