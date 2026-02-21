"""Platform-agnostic chat adapter contracts (adapter layer)."""

from .adapter import ChatAdapter, SendAttachmentRequest, SendTextRequest
from .capabilities import ChatCapabilities
from .dispatcher import (
    ChatDispatcher,
    DispatchContext,
    DispatchResult,
    build_dispatch_context,
    conversation_id_for,
    is_bypass_event,
)
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
    "ChatDispatcher",
    "ChatEvent",
    "ChatInteractionEvent",
    "ChatInteractionRef",
    "ChatMessageEvent",
    "ChatMessageRef",
    "ChatThreadRef",
    "DispatchContext",
    "DispatchResult",
    "RenderedText",
    "SendAttachmentRequest",
    "SendTextRequest",
    "TextRenderer",
    "build_dispatch_context",
    "conversation_id_for",
    "is_bypass_event",
]
