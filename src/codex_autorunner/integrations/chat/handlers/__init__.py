"""Platform-agnostic chat handler building blocks.

These handlers belong to `integrations/chat` and operate on normalized
`ChatEvent` models plus `ChatContext`.
"""

from .approvals import ChatApprovalHandlers
from .models import ChatContext
from .questions import ChatQuestionHandlers, handle_custom_text_input
from .selections import ChatSelectionHandlers

__all__ = [
    "ChatApprovalHandlers",
    "ChatContext",
    "ChatQuestionHandlers",
    "ChatSelectionHandlers",
    "handle_custom_text_input",
]
