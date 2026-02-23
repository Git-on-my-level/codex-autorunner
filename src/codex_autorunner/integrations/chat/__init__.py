"""Platform-agnostic chat adapter contracts (adapter layer)."""

from .adapter import ChatAdapter, SendAttachmentRequest, SendTextRequest
from .bootstrap import ChatBootstrapStep, run_chat_bootstrap_steps
from .callbacks import (
    CALLBACK_AGENT,
    CALLBACK_APPROVAL,
    CALLBACK_BIND,
    CALLBACK_CANCEL,
    CALLBACK_COMPACT,
    CALLBACK_EFFORT,
    CALLBACK_FLOW,
    CALLBACK_FLOW_RUN,
    CALLBACK_MODEL,
    CALLBACK_PAGE,
    CALLBACK_QUESTION_CANCEL,
    CALLBACK_QUESTION_CUSTOM,
    CALLBACK_QUESTION_DONE,
    CALLBACK_QUESTION_OPTION,
    CALLBACK_RESUME,
    CALLBACK_REVIEW_COMMIT,
    CALLBACK_UPDATE,
    CALLBACK_UPDATE_CONFIRM,
    CallbackCodec,
    LogicalCallback,
    decode_logical_callback,
    encode_logical_callback,
)
from .capabilities import ChatCapabilities
from .command_contract import COMMAND_CONTRACT, CommandContractEntry, CommandStatus
from .commands import ChatCommand, parse_chat_command
from .dispatcher import (
    ChatDispatcher,
    DispatchContext,
    DispatchResult,
    build_dispatch_context,
    conversation_id_for,
    is_bypass_event,
)
from .doctor import chat_doctor_checks
from .errors import (
    ChatAdapterError,
    ChatAdapterPermanentError,
    ChatAdapterTimeoutError,
    ChatAdapterTransientError,
)
from .handlers import (
    ChatApprovalHandlers,
    ChatContext,
    ChatQuestionHandlers,
    ChatSelectionHandlers,
    handle_custom_text_input,
)
from .media import (
    ChatVoiceInput,
    format_media_batch_failure,
    is_image_mime_or_path,
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
from .parity_checker import ParityCheckResult, run_parity_checks
from .renderer import RenderedText, TextRenderer
from .runtime import iter_exception_chain
from .service import ChatBotServiceCore
from .state_store import ChatOutboxRecord, ChatPendingApprovalRecord, ChatStateStore
from .text_chunking import chunk_text
from .transport import ChatTransport
from .turn_policy import (
    PlainTextTurnContext,
    TurnTriggerMode,
    should_trigger_plain_text_turn,
)

__all__ = [
    "ChatAction",
    "ChatAdapter",
    "ChatAdapterError",
    "ChatAdapterPermanentError",
    "ChatAdapterTimeoutError",
    "ChatAdapterTransientError",
    "ChatAttachment",
    "ChatCapabilities",
    "CommandContractEntry",
    "CommandStatus",
    "ChatCommand",
    "ChatContext",
    "ChatBotServiceCore",
    "ChatBootstrapStep",
    "ChatApprovalHandlers",
    "CallbackCodec",
    "ChatDispatcher",
    "ChatEvent",
    "ChatInteractionEvent",
    "ChatInteractionRef",
    "ChatMessageEvent",
    "ChatMessageRef",
    "ChatOutboxRecord",
    "ChatPendingApprovalRecord",
    "COMMAND_CONTRACT",
    "ParityCheckResult",
    "ChatQuestionHandlers",
    "ChatSelectionHandlers",
    "ChatStateStore",
    "ChatThreadRef",
    "ChatTransport",
    "ChatVoiceInput",
    "DispatchContext",
    "DispatchResult",
    "LogicalCallback",
    "RenderedText",
    "CALLBACK_AGENT",
    "CALLBACK_APPROVAL",
    "CALLBACK_BIND",
    "CALLBACK_CANCEL",
    "CALLBACK_COMPACT",
    "CALLBACK_EFFORT",
    "CALLBACK_FLOW",
    "CALLBACK_FLOW_RUN",
    "CALLBACK_MODEL",
    "CALLBACK_PAGE",
    "CALLBACK_QUESTION_CANCEL",
    "CALLBACK_QUESTION_CUSTOM",
    "CALLBACK_QUESTION_DONE",
    "CALLBACK_QUESTION_OPTION",
    "CALLBACK_RESUME",
    "CALLBACK_REVIEW_COMMIT",
    "CALLBACK_UPDATE",
    "CALLBACK_UPDATE_CONFIRM",
    "SendAttachmentRequest",
    "SendTextRequest",
    "TextRenderer",
    "build_dispatch_context",
    "conversation_id_for",
    "decode_logical_callback",
    "encode_logical_callback",
    "format_media_batch_failure",
    "handle_custom_text_input",
    "is_image_mime_or_path",
    "is_bypass_event",
    "iter_exception_chain",
    "parse_chat_command",
    "chunk_text",
    "chat_doctor_checks",
    "PlainTextTurnContext",
    "run_parity_checks",
    "TurnTriggerMode",
    "run_chat_bootstrap_steps",
    "should_trigger_plain_text_turn",
]
