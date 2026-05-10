from __future__ import annotations

import pytest

from codex_autorunner.core.document_file_intents import (
    document_reference_intent,
    normalize_document_file_intent,
    normalize_document_file_intents,
)


def test_file_upload_intent_preserves_legacy_attachment_fields() -> None:
    [intent] = normalize_document_file_intents(
        [
            {
                "id": "att-1",
                "kind": "image",
                "title": "screen.png",
                "uploadedName": "screen.png",
                "sizeLabel": "8 KB",
                "url": "/hub/pma/files/inbox/screen.png",
            }
        ]
    )

    assert intent == {
        "intent": "attach_uploaded_file",
        "source": "upload",
        "id": "att-1",
        "title": "screen.png",
        "url": "/hub/pma/files/inbox/screen.png",
        "size_label": "8 KB",
        "metadata": {
            "uploaded_name": "screen.png",
            "uploadedName": "screen.png",
            "kind": "image",
        },
        "kind": "image",
        "sizeLabel": "8 KB",
        "uploaded_name": "screen.png",
        "uploadedName": "screen.png",
    }


def test_link_intent_uses_shared_vocabulary() -> None:
    intent = normalize_document_file_intent(
        {
            "id": "link-1",
            "kind": "link",
            "title": "Preview",
            "url": "https://example.test",
        }
    )

    assert intent is not None
    assert intent["intent"] == "include_link"
    assert intent["source"] == "link"
    assert intent["url"] == "https://example.test"
    assert intent["kind"] == "link"


def test_contextspace_reference_intent() -> None:
    intent = document_reference_intent(
        source="contextspace",
        document_id="spec",
        title="Spec",
        rel_path=".codex-autorunner/contextspace/spec.md",
    ).to_payload()

    assert intent == {
        "intent": "reference_path",
        "source": "contextspace",
        "id": "spec",
        "title": "Spec",
        "path": ".codex-autorunner/contextspace/spec.md",
    }


def test_ticket_reference_intent_infers_source_from_path() -> None:
    intent = normalize_document_file_intent(
        {
            "intent": "reference_path",
            "id": "11",
            "title": "Ticket 11",
            "path": ".codex-autorunner/tickets/TICKET-011.md",
        }
    )

    assert intent is not None
    assert intent["source"] == "tickets"
    assert intent["path"] == ".codex-autorunner/tickets/TICKET-011.md"


def test_invalid_reference_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid path"):
        normalize_document_file_intent(
            {
                "intent": "reference_path",
                "id": "bad",
                "path": "../outside.md",
            }
        )


def test_attachment_removal_and_clear_intents_are_serializable() -> None:
    remove_intent = normalize_document_file_intent(
        {"intent": "remove_pending_attachment", "id": "att-1"}
    )
    clear_intent = normalize_document_file_intent(
        {"intent": "clear_pending_attachments"}
    )

    assert remove_intent == {"intent": "remove_pending_attachment", "id": "att-1"}
    assert clear_intent == {"intent": "clear_pending_attachments"}
