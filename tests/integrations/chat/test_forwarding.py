from __future__ import annotations

from codex_autorunner.integrations.chat.forwarding import compose_inbound_message_text
from codex_autorunner.integrations.chat.models import (
    ChatForwardInfo,
    ChatMessageRef,
    ChatReplyInfo,
    ChatThreadRef,
)


def _reply_info(*, text: str | None, author_label: str | None = None) -> ChatReplyInfo:
    thread = ChatThreadRef(platform="discord", chat_id="channel-1")
    return ChatReplyInfo(
        message=ChatMessageRef(thread=thread, message_id="reply-1"),
        text=text,
        author_label=author_label,
    )


def test_compose_inbound_message_text_includes_reply_context() -> None:
    rendered = compose_inbound_message_text(
        "Can you fix this?",
        reply_context=_reply_info(
            text="The bug is in the adapter.", author_label="alice"
        ),
    )

    assert "Can you fix this?" in rendered
    assert "Replying to message from alice [message reply-1]:" in rendered
    assert "The bug is in the adapter." in rendered


def test_compose_inbound_message_text_handles_forward_and_reply_together() -> None:
    rendered = compose_inbound_message_text(
        "Please investigate",
        forwarded_from=ChatForwardInfo(
            source_label="Build Alerts",
            message_id="77",
            text="deploy failed",
            is_automatic=True,
        ),
        reply_context=_reply_info(text="Look at the previous deploy logs."),
    )

    assert "Please investigate" in rendered
    assert "Forwarded message (automatic) from Build Alerts [message 77]:" in rendered
    assert "deploy failed" in rendered
    assert "Replying to message [message reply-1]:" in rendered
    assert "Look at the previous deploy logs." in rendered


def test_compose_inbound_message_text_uses_unavailable_reply_fallback() -> None:
    rendered = compose_inbound_message_text(
        "follow-up",
        reply_context=_reply_info(text=None),
    )

    assert "(original message text unavailable)" in rendered
