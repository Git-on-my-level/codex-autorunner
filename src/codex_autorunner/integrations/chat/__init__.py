"""Platform-agnostic chat adapter contracts (adapter layer)."""

from .adapter import ChatAdapter, SendAttachmentRequest, SendTextRequest
from .capabilities import ChatCapabilities
from .errors import (
    ChatAdapterError,
    ChatAdapterPermanentError,
    ChatAdapterTimeoutError,
    ChatAdapterTransientError,
)
from .models import (
    ChatAction,
    ChatAttachment,
    ChatEvent,
    ChatInteractionEvent,
    ChatInteractionRef,
    ChatMessageEvent,
    ChatMessageRef,
    ChatThreadRef,
)
from .renderer import RenderedText, TextRenderer

__all__ = [
    "ChatAction",
    "ChatAdapter",
    "ChatAdapterError",
    "ChatAdapterPermanentError",
    "ChatAdapterTimeoutError",
    "ChatAdapterTransientError",
    "ChatAttachment",
    "ChatCapabilities",
    "ChatEvent",
    "ChatInteractionEvent",
    "ChatInteractionRef",
    "ChatMessageEvent",
    "ChatMessageRef",
    "ChatThreadRef",
    "RenderedText",
    "SendAttachmentRequest",
    "SendTextRequest",
    "TextRenderer",
]
