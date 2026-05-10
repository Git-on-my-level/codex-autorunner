from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .....core.chat_document_browser import (
    DocumentBrowserDocument,
    browser_page_slice,
    list_document_browser_items,
    read_document_browser_document,
)
from ...adapter import (
    InlineButton,
    TelegramCallbackQuery,
    TelegramMessage,
    build_inline_keyboard,
    chunk_message,
    encode_document_browser_callback,
)
from ...constants import DEFAULT_PAGE_SIZE, TELEGRAM_MAX_MESSAGE_LENGTH
from ...helpers import _compact_preview
from ...types import DocumentBrowserState
from .shared import TelegramCommandSupportMixin

_DOCUMENT_BROWSER_BODY_MAX_LEN = TELEGRAM_MAX_MESSAGE_LENGTH - 600
_DOCUMENT_BROWSER_BUTTON_LABEL_LIMIT = 48


class DocumentBrowserCommands(TelegramCommandSupportMixin):
    async def _handle_tickets(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        await self._open_document_browser(
            message,
            source="tickets",
            query=args,
        )

    async def _handle_contextspace(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        await self._open_document_browser(
            message,
            source="contextspace",
            query=args,
        )

    async def _handle_document_browser_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: Any,
    ) -> None:
        state = self._document_browser_states.get(key)
        actor_id = (
            str(callback.from_user_id) if callback.from_user_id is not None else None
        )
        if not self._ui_state.owner_matches(state, actor_id):
            await self._answer_callback(callback, "Selection expired")
            return
        if state is None:
            await self._answer_callback(callback, "Selection expired")
            return
        workspace_root = await self._document_browser_workspace_root(key)
        if workspace_root is None:
            await self._answer_callback(callback, "No workspace bound")
            return

        action = str(parsed.action or "").strip().lower()
        if action == "open":
            index = self._parse_browser_index(parsed.value)
            if index is None:
                await self._answer_callback(callback, "Selection expired")
                return
            items = list_document_browser_items(
                workspace_root,
                state.source,
                query=state.query,
            )
            _page, _total_pages, page_items = browser_page_slice(
                items,
                page=state.list_page,
                page_size=DEFAULT_PAGE_SIZE,
            )
            if index < 0 or index >= len(page_items):
                await self._answer_callback(callback, "Selection expired")
                return
            state.document_id = page_items[index].document_id
            state.chunk_index = 0
            await self._answer_callback(callback, "Opened")
        elif action == "list":
            page = self._parse_browser_index(parsed.value)
            if page is None:
                await self._answer_callback(callback, "Selection expired")
                return
            state.document_id = None
            state.chunk_index = 0
            state.list_page = page
            await self._answer_callback(callback, "Page updated")
        elif action == "chunk":
            chunk_index = self._parse_browser_index(parsed.value)
            if chunk_index is None or not state.document_id:
                await self._answer_callback(callback, "Selection expired")
                return
            state.chunk_index = chunk_index
            await self._answer_callback(callback, "Part updated")
        elif action == "back":
            state.document_id = None
            state.chunk_index = 0
            await self._answer_callback(callback, "Back")
        else:
            await self._answer_callback(callback, "Selection expired")
            return

        self._touch_cache_timestamp("document_browser_states", key)
        text, reply_markup = self._render_document_browser_view(
            workspace_root,
            state,
        )
        await self._update_document_browser_message(key, callback, text, reply_markup)

    async def _open_document_browser(
        self,
        message: TelegramMessage,
        *,
        source: str,
        query: str,
    ) -> None:
        record = await self._require_bound_record(message)
        if record is None:
            return
        workspace_root = self._canonical_workspace_root(record.workspace_path)
        if workspace_root is None:
            await self._send_message(
                message.chat_id,
                "Workspace unavailable.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        self._document_browser_states[key] = DocumentBrowserState(
            source=source,
            query=str(query or "").strip(),
            requester_user_id=(
                str(message.from_user_id) if message.from_user_id is not None else None
            ),
        )
        self._touch_cache_timestamp("document_browser_states", key)
        text, reply_markup = self._render_document_browser_view(
            workspace_root,
            self._document_browser_states[key],
        )
        await self._send_message(
            message.chat_id,
            text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=reply_markup,
        )

    async def _document_browser_workspace_root(self, key: str) -> Optional[Path]:
        record = await self._router.get_topic(key)
        if record is None or not record.workspace_path:
            return None
        return self._canonical_workspace_root(record.workspace_path)

    def _render_document_browser_view(
        self,
        workspace_root: Path,
        state: DocumentBrowserState,
    ) -> tuple[str, dict[str, Any] | None]:
        if state.document_id:
            document = read_document_browser_document(
                workspace_root,
                state.source,
                state.document_id,
            )
            if document is not None:
                return self._render_document_browser_document(document, state)
            state.document_id = None
            state.chunk_index = 0
        return self._render_document_browser_list(workspace_root, state)

    def _render_document_browser_list(
        self,
        workspace_root: Path,
        state: DocumentBrowserState,
    ) -> tuple[str, dict[str, Any] | None]:
        items = list_document_browser_items(
            workspace_root,
            state.source,
            query=state.query,
        )
        state.list_page, total_pages, page_items = browser_page_slice(
            items,
            page=state.list_page,
            page_size=DEFAULT_PAGE_SIZE,
        )
        title = "Browse tickets" if state.source == "tickets" else "Browse contextspace"
        if state.query:
            title = f"{title} matching '{state.query}'"
        lines = [title]
        if not page_items:
            lines.append("")
            lines.append("No documents matched this query.")
            return "\n".join(lines), None
        lines.append("")
        for idx, item in enumerate(page_items, 1):
            lines.append(f"{idx}. {item.label}")
            lines.append(f"   {item.description}")
        rows: list[list[InlineButton]] = [
            [
                InlineButton(
                    _compact_preview(
                        f"{idx}) {item.label}",
                        _DOCUMENT_BROWSER_BUTTON_LABEL_LIMIT,
                    ),
                    encode_document_browser_callback("open", str(idx - 1)),
                )
            ]
            for idx, item in enumerate(page_items, 1)
        ]
        pager: list[InlineButton] = []
        if state.list_page > 0:
            pager.append(
                InlineButton(
                    "Prev",
                    encode_document_browser_callback(
                        "list",
                        str(state.list_page - 1),
                    ),
                )
            )
        if state.list_page < (total_pages - 1):
            pager.append(
                InlineButton(
                    "Next",
                    encode_document_browser_callback(
                        "list",
                        str(state.list_page + 1),
                    ),
                )
            )
        if pager:
            rows.append(pager)
        return "\n".join(lines), build_inline_keyboard(rows)

    def _render_document_browser_document(
        self,
        document: DocumentBrowserDocument,
        state: DocumentBrowserState,
    ) -> tuple[str, dict[str, Any] | None]:
        lines = [document.title, document.description, document.rel_path]
        if not document.exists:
            lines.extend(("", "This file is missing from the current workspace."))
            keyboard = build_inline_keyboard(
                [[InlineButton("Back", encode_document_browser_callback("back"))]]
            )
            return "\n".join(lines), keyboard

        content = document.content.strip() or "(File is empty.)"
        chunks = self._chunk_browser_content(content)
        state.chunk_index = max(0, min(state.chunk_index, len(chunks) - 1))
        lines.extend(
            (
                "",
                f"Part {state.chunk_index + 1}/{len(chunks)}",
                "",
                chunks[state.chunk_index],
            )
        )
        row = [InlineButton("Back", encode_document_browser_callback("back"))]
        if state.chunk_index > 0:
            row.append(
                InlineButton(
                    "Prev",
                    encode_document_browser_callback(
                        "chunk",
                        str(state.chunk_index - 1),
                    ),
                )
            )
        if state.chunk_index < (len(chunks) - 1):
            row.append(
                InlineButton(
                    "Next",
                    encode_document_browser_callback(
                        "chunk",
                        str(state.chunk_index + 1),
                    ),
                )
            )
        return "\n".join(lines), build_inline_keyboard([row])

    def _chunk_browser_content(self, content: str) -> list[str]:
        chunks = chunk_message(
            content,
            max_len=_DOCUMENT_BROWSER_BODY_MAX_LEN,
            with_numbering=False,
        )
        return chunks or ["(File is empty.)"]

    def _parse_browser_index(self, value: Optional[str]) -> Optional[int]:
        if value is None:
            return None
        if not str(value).isdigit():
            return None
        return int(str(value))

    async def _update_document_browser_message(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        text: str,
        reply_markup: dict[str, Any] | None,
    ) -> None:
        if await self._edit_callback_message(
            callback,
            text,
            reply_markup=reply_markup,
        ):
            return
        chat_id = callback.chat_id
        if chat_id is None:
            return
        await self._send_message(
            chat_id,
            text,
            thread_id=callback.thread_id,
            reply_markup=reply_markup,
        )
