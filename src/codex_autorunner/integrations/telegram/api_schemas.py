from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

__all__ = [
    "BaseTelegramSchema",
    "TelegramPhotoSizeSchema",
    "TelegramDocumentSchema",
    "TelegramAudioSchema",
    "TelegramVoiceSchema",
    "TelegramMessageEntitySchema",
    "TelegramMessageSchema",
    "TelegramCallbackQuerySchema",
    "TelegramUpdateSchema",
    "parse_update_payload",
    "parse_message_payload",
    "parse_callback_query_payload",
]


class BaseTelegramSchema(BaseModel):
    model_config = {"extra": "ignore", "validate_assignment": False}


class TelegramPhotoSizeSchema(BaseTelegramSchema):
    file_id: str
    file_unique_id: Optional[str] = None
    width: int
    height: int
    file_size: Optional[int] = None


class TelegramDocumentSchema(BaseTelegramSchema):
    file_id: str
    file_unique_id: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None


class TelegramAudioSchema(BaseTelegramSchema):
    file_id: str
    file_unique_id: Optional[str] = None
    duration: Optional[int] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None


class TelegramVoiceSchema(BaseTelegramSchema):
    file_id: str
    file_unique_id: Optional[str] = None
    duration: Optional[int] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None


class TelegramMessageEntitySchema(BaseTelegramSchema):
    type: str
    offset: int
    length: int


class TelegramMessageSchema(BaseTelegramSchema):
    message_id: int
    chat: dict[str, Any] = Field(default_factory=dict)
    from_user: Optional[dict[str, Any]] = Field(default=None, alias="from")
    message_thread_id: Optional[int] = None
    date: Optional[int] = None
    text: Optional[str] = None
    caption: Optional[str] = None
    entities: Optional[list[dict[str, Any]]] = None
    caption_entities: Optional[list[dict[str, Any]]] = None
    photo: Optional[list[dict[str, Any]]] = None
    document: Optional[dict[str, Any]] = None
    audio: Optional[dict[str, Any]] = None
    voice: Optional[dict[str, Any]] = None
    media_group_id: Optional[str] = None
    is_topic_message: bool = False
    reply_to_message: Optional[dict[str, Any]] = None


class TelegramCallbackQuerySchema(BaseTelegramSchema):
    id: str
    from_user: dict[str, Any] = Field(alias="from")
    data: Optional[str] = None
    message: Optional[dict[str, Any]] = None


class TelegramUpdateSchema(BaseTelegramSchema):
    update_id: int
    message: Optional[dict[str, Any]] = None
    edited_message: Optional[dict[str, Any]] = Field(
        default=None, alias="edited_message"
    )
    callback_query: Optional[dict[str, Any]] = Field(
        default=None, alias="callback_query"
    )


def parse_update_payload(payload: dict[str, Any]) -> TelegramUpdateSchema:
    return TelegramUpdateSchema.model_validate(payload)


def parse_message_payload(payload: dict[str, Any]) -> Optional[TelegramMessageSchema]:
    try:
        return TelegramMessageSchema.model_validate(payload)
    except Exception:
        return None


def parse_callback_query_payload(
    payload: dict[str, Any],
) -> Optional[TelegramCallbackQuerySchema]:
    try:
        return TelegramCallbackQuerySchema.model_validate(payload)
    except Exception:
        return None
