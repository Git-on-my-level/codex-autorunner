from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence, Union

import httpx

from .logging_utils import log_event


TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_CALLBACK_DATA_LIMIT = 64

INTERRUPT_ALIASES = {
    "^c",
    "ctrl-c",
    "ctrl+c",
    "esc",
    "escape",
}


class TelegramAPIError(Exception):
    """Raised when the Telegram Bot API returns an error."""


@dataclass(frozen=True)
class TelegramMessage:
    update_id: int
    message_id: int
    chat_id: int
    thread_id: Optional[int]
    from_user_id: Optional[int]
    text: Optional[str]
    date: Optional[int]
    is_topic_message: bool


@dataclass(frozen=True)
class TelegramCallbackQuery:
    update_id: int
    callback_id: str
    from_user_id: Optional[int]
    data: Optional[str]
    message_id: Optional[int]
    chat_id: Optional[int]
    thread_id: Optional[int]


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    message: Optional[TelegramMessage]
    callback: Optional[TelegramCallbackQuery]


@dataclass(frozen=True)
class TelegramCommand:
    name: str
    args: str
    raw: str


@dataclass(frozen=True)
class TelegramAllowlist:
    allowed_chat_ids: set[int]
    allowed_user_ids: set[int]
    require_topic: bool = False


@dataclass(frozen=True)
class InlineButton:
    text: str
    callback_data: str


@dataclass(frozen=True)
class ApprovalCallback:
    decision: str
    request_id: str


@dataclass(frozen=True)
class ResumeCallback:
    thread_id: str


@dataclass(frozen=True)
class BindCallback:
    repo_id: str


def parse_command(
    text: Optional[str], *, bot_username: Optional[str] = None
) -> Optional[TelegramCommand]:
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, tail = stripped.partition(" ")
    command = head.lstrip("/")
    if not command:
        return None
    if "@" in command:
        name, _, target = command.partition("@")
        if bot_username and target.lower() != bot_username.lower():
            return None
        command = name
    return TelegramCommand(name=command.lower(), args=tail.strip(), raw=stripped)


def is_interrupt_alias(text: Optional[str]) -> bool:
    if not text:
        return False
    normalized = text.strip().lower()
    if normalized in INTERRUPT_ALIASES:
        return True
    return normalized == "/interrupt"


def parse_update(update: dict[str, Any]) -> Optional[TelegramUpdate]:
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return None
    message = _parse_message(update_id, update.get("message"))
    if message is None:
        message = _parse_message(update_id, update.get("edited_message"))
    callback = _parse_callback(update_id, update.get("callback_query"))
    if message is None and callback is None:
        return None
    return TelegramUpdate(update_id=update_id, message=message, callback=callback)


def _parse_message(update_id: int, payload: Any) -> Optional[TelegramMessage]:
    if not isinstance(payload, dict):
        return None
    message_id = payload.get("message_id")
    chat = payload.get("chat")
    if not isinstance(message_id, int) or not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return None
    thread_id = payload.get("message_thread_id")
    if thread_id is not None and not isinstance(thread_id, int):
        thread_id = None
    sender = payload.get("from")
    from_user_id = sender.get("id") if isinstance(sender, dict) else None
    if from_user_id is not None and not isinstance(from_user_id, int):
        from_user_id = None
    text = payload.get("text")
    if text is not None and not isinstance(text, str):
        text = None
    date = payload.get("date")
    if date is not None and not isinstance(date, int):
        date = None
    is_topic_message = bool(payload.get("is_topic_message"))
    return TelegramMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        thread_id=thread_id,
        from_user_id=from_user_id,
        text=text,
        date=date,
        is_topic_message=is_topic_message,
    )


def _parse_callback(update_id: int, payload: Any) -> Optional[TelegramCallbackQuery]:
    if not isinstance(payload, dict):
        return None
    callback_id = payload.get("id")
    if not isinstance(callback_id, str):
        return None
    sender = payload.get("from")
    from_user_id = sender.get("id") if isinstance(sender, dict) else None
    if from_user_id is not None and not isinstance(from_user_id, int):
        from_user_id = None
    data = payload.get("data")
    if data is not None and not isinstance(data, str):
        data = None
    message = payload.get("message")
    message_id = None
    chat_id = None
    thread_id = None
    if isinstance(message, dict):
        message_id = message.get("message_id")
        chat = message.get("chat")
        if isinstance(chat, dict):
            chat_id = chat.get("id")
        thread_id = message.get("message_thread_id")
    if message_id is not None and not isinstance(message_id, int):
        message_id = None
    if chat_id is not None and not isinstance(chat_id, int):
        chat_id = None
    if thread_id is not None and not isinstance(thread_id, int):
        thread_id = None
    return TelegramCallbackQuery(
        update_id=update_id,
        callback_id=callback_id,
        from_user_id=from_user_id,
        data=data,
        message_id=message_id,
        chat_id=chat_id,
        thread_id=thread_id,
    )


def allowlist_allows(update: TelegramUpdate, allowlist: TelegramAllowlist) -> bool:
    if not allowlist.allowed_chat_ids or not allowlist.allowed_user_ids:
        return False
    chat_id = None
    user_id = None
    thread_id = None
    if update.message:
        chat_id = update.message.chat_id
        user_id = update.message.from_user_id
        thread_id = update.message.thread_id
    elif update.callback:
        chat_id = update.callback.chat_id
        user_id = update.callback.from_user_id
        thread_id = update.callback.thread_id
    if chat_id is None or user_id is None:
        return False
    if chat_id not in allowlist.allowed_chat_ids:
        return False
    if user_id not in allowlist.allowed_user_ids:
        return False
    if allowlist.require_topic and thread_id is None:
        return False
    return True


def chunk_message(
    text: Optional[str],
    *,
    max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH,
    with_numbering: bool = True,
) -> list[str]:
    if not text:
        return []
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    if len(text) <= max_len:
        return [text]
    parts = _split_text(text, max_len)
    if not with_numbering or len(parts) == 1:
        return parts
    parts = _apply_numbering(text, max_len)
    return parts


def _apply_numbering(text: str, max_len: int) -> list[str]:
    parts = _split_text(text, max_len)
    total = len(parts)
    while True:
        prefix_len = len(_part_prefix(total, total))
        allowed = max_len - prefix_len
        if allowed <= 0:
            raise ValueError("max_len too small for numbering")
        parts = _split_text(text, allowed)
        new_total = len(parts)
        if new_total == total:
            break
        total = new_total
    return [f"{_part_prefix(idx, total)}{chunk}" for idx, chunk in enumerate(parts, 1)]


def _part_prefix(index: int, total: int) -> str:
    return f"Part {index}/{total}\n"


def _split_text(text: str, limit: int) -> list[str]:
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut == -1:
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunk = remaining[:cut]
        remaining = remaining[cut:]
        parts.append(chunk)
    return parts


def build_inline_keyboard(
    rows: Sequence[Sequence[InlineButton]],
) -> dict[str, Any]:
    keyboard: list[list[dict[str, str]]] = []
    for row in rows:
        keyboard.append(
            [{"text": button.text, "callback_data": button.callback_data} for button in row]
        )
    return {"inline_keyboard": keyboard}


def encode_approval_callback(decision: str, request_id: str) -> str:
    data = f"appr:{decision}:{request_id}"
    _validate_callback_data(data)
    return data


def encode_resume_callback(thread_id: str) -> str:
    data = f"resume:{thread_id}"
    _validate_callback_data(data)
    return data


def encode_bind_callback(repo_id: str) -> str:
    data = f"bind:{repo_id}"
    _validate_callback_data(data)
    return data


def parse_callback_data(
    data: Optional[str],
) -> Optional[Union[ApprovalCallback, ResumeCallback, BindCallback]]:
    if not data:
        return None
    if data.startswith("appr:"):
        _, _, rest = data.partition(":")
        decision, sep, request_id = rest.partition(":")
        if not decision or not sep or not request_id:
            return None
        return ApprovalCallback(decision=decision, request_id=request_id)
    if data.startswith("resume:"):
        _, _, thread_id = data.partition(":")
        if not thread_id:
            return None
        return ResumeCallback(thread_id=thread_id)
    if data.startswith("bind:"):
        _, _, repo_id = data.partition(":")
        if not repo_id:
            return None
        return BindCallback(repo_id=repo_id)
    return None


def build_approval_keyboard(
    request_id: str, *, include_session: bool = False
) -> dict[str, Any]:
    rows: list[list[InlineButton]] = [
        [
            InlineButton("Accept", encode_approval_callback("accept", request_id)),
            InlineButton("Decline", encode_approval_callback("decline", request_id)),
        ],
        [InlineButton("Cancel", encode_approval_callback("cancel", request_id))],
    ]
    if include_session:
        rows[0].insert(
            1, InlineButton("Accept session", encode_approval_callback("accept_session", request_id))
        )
    return build_inline_keyboard(rows)


def build_resume_keyboard(options: Sequence[tuple[str, str]]) -> dict[str, Any]:
    rows = [
        [InlineButton(label, encode_resume_callback(thread_id))]
        for thread_id, label in options
    ]
    return build_inline_keyboard(rows)


def build_bind_keyboard(options: Sequence[tuple[str, str]]) -> dict[str, Any]:
    rows = [
        [InlineButton(label, encode_bind_callback(repo_id))]
        for repo_id, label in options
    ]
    return build_inline_keyboard(rows)


def _validate_callback_data(data: str) -> None:
    if len(data.encode("utf-8")) > TELEGRAM_CALLBACK_DATA_LIMIT:
        raise ValueError("callback_data exceeds Telegram limit")


def next_update_offset(updates: Iterable[dict[str, Any]], current: Optional[int]) -> Optional[int]:
    max_update_id = None
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            if max_update_id is None or update_id > max_update_id:
                max_update_id = update_id
    if max_update_id is None:
        return current
    return max_update_id + 1


class TelegramBotClient:
    def __init__(
        self,
        bot_token: str,
        *,
        timeout_seconds: float = 30.0,
        logger: Optional[logging.Logger] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._logger = logger or logging.getLogger(__name__)
        if client is None:
            self._client = httpx.AsyncClient(timeout=timeout_seconds)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "TelegramBotClient":
        return self

    async def __aexit__(self, *_exc_info) -> None:
        await self.close()

    async def get_updates(
        self,
        *,
        offset: Optional[int] = None,
        timeout: int = 30,
        allowed_updates: Optional[Sequence[str]] = None,
    ) -> list[dict[str, Any]]:
        log_event(
            self._logger,
            logging.DEBUG,
            "telegram.request",
            method="getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=list(allowed_updates) if allowed_updates else None,
        )
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        if allowed_updates:
            payload["allowed_updates"] = list(allowed_updates)
        result = await self._request("getUpdates", payload)
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    async def send_message(
        self,
        chat_id: Union[int, str],
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.send_message",
            chat_id=chat_id,
            thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            text_len=len(text),
            has_markup=reply_markup is not None,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        result = await self._request("sendMessage", payload)
        return result if isinstance(result, dict) else {}

    async def get_me(self) -> dict[str, Any]:
        log_event(self._logger, logging.DEBUG, "telegram.request", method="getMe")
        result = await self._request("getMe", {})
        return result if isinstance(result, dict) else {}

    async def send_message_chunks(
        self,
        chat_id: Union[int, str],
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
        max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH,
    ) -> list[dict[str, Any]]:
        chunks = chunk_message(text, max_len=max_len, with_numbering=True)
        if not chunks:
            return []
        responses: list[dict[str, Any]] = []
        log_event(
            self._logger,
            logging.INFO,
            "telegram.send_message.chunks",
            chat_id=chat_id,
            thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            parts=len(chunks),
            total_len=len(text),
        )
        for idx, chunk in enumerate(chunks):
            response = await self.send_message(
                chat_id,
                chunk,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id if idx == 0 else None,
                reply_markup=reply_markup if idx == 0 else None,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            responses.append(response)
        return responses

    async def edit_message_text(
        self,
        chat_id: Union[int, str],
        message_id: int,
        text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.edit_message",
            chat_id=chat_id,
            message_id=message_id,
            text_len=len(text),
            has_markup=reply_markup is not None,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        result = await self._request("editMessageText", payload)
        return result if isinstance(result, dict) else {}

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.answer_callback",
            callback_query_id=callback_query_id,
            text_len=len(text) if text else 0,
            show_alert=show_alert,
        )
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True
        result = await self._request("answerCallbackQuery", payload)
        return result if isinstance(result, dict) else {}

    async def _request(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{self._base_url}/{method}"
        try:
            response = await self._client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.request.failed",
                method=method,
                exc=exc,
            )
            raise TelegramAPIError("Telegram request failed") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            description = data.get("description") if isinstance(data, dict) else None
            raise TelegramAPIError(description or "Telegram API returned error")
        return data.get("result")


class TelegramUpdatePoller:
    def __init__(
        self,
        client: TelegramBotClient,
        *,
        allowed_updates: Optional[Sequence[str]] = None,
    ) -> None:
        self._client = client
        self._offset: Optional[int] = None
        self._allowed_updates = list(allowed_updates) if allowed_updates else None

    async def poll(self, *, timeout: int = 30) -> list[TelegramUpdate]:
        updates = await self._client.get_updates(
            offset=self._offset,
            timeout=timeout,
            allowed_updates=self._allowed_updates,
        )
        self._offset = next_update_offset(updates, self._offset)
        parsed: list[TelegramUpdate] = []
        for update in updates:
            parsed_update = parse_update(update)
            if parsed_update is not None:
                parsed.append(parsed_update)
        return parsed
