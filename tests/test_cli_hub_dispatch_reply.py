from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from codex_autorunner.cli import app

runner = CliRunner()


def _json_response(method: str, url: str, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request(method, url))


def test_hub_dispatch_reply_posts_and_resumes(hub_root_only) -> None:
    run_id = "11111111-1111-1111-1111-111111111111"
    calls: list[tuple[str, str, Any, Any]] = []

    def _mock_request(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs.get("json"), kwargs.get("data")))
        if method == "GET" and url.endswith(f"/api/messages/threads/{run_id}"):
            return _json_response(
                method,
                url,
                {
                    "run": {"id": run_id, "status": "paused"},
                    "reply_history": [],
                },
            )
        if method == "POST" and url.endswith(f"/api/messages/{run_id}/reply"):
            assert kwargs.get("data", {}).get("body") == "LGTM"
            return _json_response(method, url, {"status": "ok", "seq": 3})
        if method == "POST" and url.endswith(f"/api/flows/{run_id}/resume"):
            return _json_response(method, url, {"status": "running"})
        raise AssertionError(f"unexpected request: {method} {url}")

    with patch("httpx.request", side_effect=_mock_request):
        result = runner.invoke(
            app,
            [
                "hub",
                "dispatch",
                "reply",
                "--path",
                str(hub_root_only),
                "--repo-id",
                "repo-a",
                "--run-id",
                run_id,
                "--message",
                "LGTM",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["repo_id"] == "repo-a"
    assert payload["run_id"] == run_id
    assert payload["reply_seq"] == 3
    assert payload["duplicate"] is False
    assert payload["resumed"] is True
    assert payload["resume_status"] == "running"
    assert len(calls) == 3


def test_hub_dispatch_reply_detects_duplicate_by_idempotency_key(hub_root_only) -> None:
    run_id = "22222222-2222-2222-2222-222222222222"
    marker = "<!-- car-idempotency-key:abc123 -->"
    calls: list[tuple[str, str]] = []

    def _mock_request(method: str, url: str, **kwargs):
        calls.append((method, url))
        if method == "GET" and url.endswith(f"/api/messages/threads/{run_id}"):
            return _json_response(
                method,
                url,
                {
                    "run": {"id": run_id, "status": "paused"},
                    "reply_history": [
                        {
                            "seq": 7,
                            "reply": {"title": None, "body": f"Done\n\n{marker}"},
                        }
                    ],
                },
            )
        if method == "POST" and url.endswith(f"/api/flows/{run_id}/resume"):
            return _json_response(method, url, {"status": "running"})
        raise AssertionError(f"unexpected request: {method} {url}")

    with patch("httpx.request", side_effect=_mock_request):
        result = runner.invoke(
            app,
            [
                "hub",
                "dispatch",
                "reply",
                "--path",
                str(hub_root_only),
                "--repo-id",
                "repo-a",
                "--run-id",
                run_id,
                "--message",
                "LGTM",
                "--idempotency-key",
                "abc123",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["duplicate"] is True
    assert payload["reply_seq"] == 7
    # GET thread + POST resume; no POST reply when duplicate.
    assert len(calls) == 2
    assert calls[0][0] == "GET"
    assert calls[1][0] == "POST"
    assert f"/api/messages/{run_id}/reply" not in calls[1][1]


def test_hub_dispatch_reply_requires_exactly_one_message_input(hub_root_only) -> None:
    result = runner.invoke(
        app,
        [
            "hub",
            "dispatch",
            "reply",
            "--path",
            str(hub_root_only),
            "--repo-id",
            "repo-a",
            "--run-id",
            "33333333-3333-3333-3333-333333333333",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert "Provide exactly one" in result.output
