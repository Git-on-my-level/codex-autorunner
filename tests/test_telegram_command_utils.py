from __future__ import annotations

import json

import httpx
import pytest

from codex_autorunner.agents.opencode.client import OpenCodeProtocolError
from codex_autorunner.agents.opencode.supervisor import OpenCodeSupervisorError
from codex_autorunner.integrations.telegram.handlers.commands.command_utils import (
    _format_httpx_exception,
    _format_opencode_exception,
    _issue_only_link,
    _opencode_review_arguments,
)


def test_issue_only_link_matches_single_link_wrappers() -> None:
    link = "https://example.com/issue/1"
    assert _issue_only_link(link, [link]) == link
    assert _issue_only_link(f"<{link}>", [link]) == link
    assert _issue_only_link(f"({link})", [link]) == link


def test_issue_only_link_ignores_non_wrapper_text() -> None:
    assert _issue_only_link("", ["https://example.com"]) is None
    assert _issue_only_link("check this", ["https://example.com"]) is None
    assert _issue_only_link("https://example.com", ["one", "two"]) is None


def test_opencode_review_arguments_reduces_known_target_types() -> None:
    assert _opencode_review_arguments({"type": "uncommittedChanges"}) == ""
    assert (
        _opencode_review_arguments({"type": "baseBranch", "branch": "feature/ci"})
        == "feature/ci"
    )
    assert _opencode_review_arguments({"type": "commit", "sha": "abc123"}) == "abc123"
    assert (
        _opencode_review_arguments({"type": "custom", "instructions": "add tests"})
        == "uncommitted\n\nadd tests"
    )
    assert (
        _opencode_review_arguments({"type": "custom", "instructions": "   "})
        == "uncommitted"
    )


def test_opencode_review_arguments_falls_back_to_json_payload() -> None:
    target = {"type": "other", "foo": "bar"}
    assert _opencode_review_arguments(target) == json.dumps(target, sort_keys=True)


def test_format_httpx_exception_uses_http_payload_detail() -> None:
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(
        502,
        request=request,
        json={"message": "temporary outage"},
    )
    exc = httpx.HTTPStatusError("server error", request=request, response=response)
    assert _format_httpx_exception(exc) == "temporary outage"


def test_format_opencode_exception_formats_backend_unavailable() -> None:
    result = _format_opencode_exception(OpenCodeSupervisorError("service offline"))
    assert result == "OpenCode backend unavailable (service offline)."


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            OpenCodeProtocolError("invalid protocol"),
            "OpenCode protocol error: invalid protocol",
        ),
        (OpenCodeProtocolError(""), "OpenCode protocol error."),
    ],
)
def test_format_opencode_exception_formats_protocol_error(
    exc: Exception, expected: str
) -> None:
    assert _format_opencode_exception(exc) == expected
