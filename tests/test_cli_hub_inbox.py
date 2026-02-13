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


def test_hub_inbox_resolve_posts_action(hub_root_only) -> None:
    calls: list[tuple[str, str, Any]] = []

    def _mock_request(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if method == "POST" and url.endswith("/hub/messages/resolve"):
            payload = kwargs.get("json") or {}
            assert payload["repo_id"] == "repo-a"
            assert payload["run_id"] == "11111111-1111-1111-1111-111111111111"
            assert payload["action"] == "dismiss"
            assert payload["seq"] == 3
            return _json_response(method, url, {"status": "ok", "resolved": payload})
        raise AssertionError(f"unexpected request: {method} {url}")

    with patch("httpx.request", side_effect=_mock_request):
        result = runner.invoke(
            app,
            [
                "hub",
                "inbox",
                "resolve",
                "--path",
                str(hub_root_only),
                "--repo-id",
                "repo-a",
                "--run-id",
                "11111111-1111-1111-1111-111111111111",
                "--seq",
                "3",
                "--action",
                "dismiss",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["resolved"]["seq"] == 3
    assert len(calls) == 1


def test_hub_inbox_clear_stale_dry_run_filters_items(hub_root_only) -> None:
    calls: list[tuple[str, str]] = []

    def _mock_request(method: str, url: str, **kwargs):
        calls.append((method, url))
        if method == "GET" and "/hub/messages?limit=2000" in url:
            return _json_response(
                method,
                url,
                {
                    "items": [
                        {
                            "repo_id": "repo-a",
                            "run_id": "run-1",
                            "item_type": "run_failed",
                            "seq": None,
                        },
                        {
                            "repo_id": "repo-a",
                            "run_id": "run-2",
                            "item_type": "run_dispatch",
                            "seq": 2,
                        },
                        {
                            "repo_id": "repo-b",
                            "run_id": "run-3",
                            "item_type": "run_state_attention",
                            "seq": 1,
                        },
                    ]
                },
            )
        raise AssertionError(f"unexpected request: {method} {url}")

    with patch("httpx.request", side_effect=_mock_request):
        result = runner.invoke(
            app,
            [
                "hub",
                "inbox",
                "clear",
                "--path",
                str(hub_root_only),
                "--stale",
                "--dry-run",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["selected_count"] == 2
    selected_types = {item["item_type"] for item in payload["selected"]}
    assert selected_types == {"run_failed", "run_state_attention"}
    assert payload["resolved"] == []
    assert len(calls) == 1


def test_hub_inbox_clear_targeted_apply(hub_root_only) -> None:
    calls: list[tuple[str, str, Any]] = []

    def _mock_request(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if method == "GET" and "/hub/messages?limit=2000" in url:
            return _json_response(
                method,
                url,
                {
                    "items": [
                        {
                            "repo_id": "repo-a",
                            "run_id": "run-7",
                            "item_type": "run_state_attention",
                            "seq": 5,
                        }
                    ]
                },
            )
        if method == "POST" and url.endswith("/hub/messages/resolve"):
            payload = kwargs.get("json") or {}
            return _json_response(
                method,
                url,
                {
                    "status": "ok",
                    "resolved": {
                        "repo_id": payload.get("repo_id"),
                        "run_id": payload.get("run_id"),
                        "item_type": payload.get("item_type"),
                        "seq": payload.get("seq"),
                    },
                },
            )
        raise AssertionError(f"unexpected request: {method} {url}")

    with patch("httpx.request", side_effect=_mock_request):
        result = runner.invoke(
            app,
            [
                "hub",
                "inbox",
                "clear",
                "--path",
                str(hub_root_only),
                "--repo-id",
                "repo-a",
                "--run-id",
                "run-7",
                "--seq",
                "5",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["selected_count"] == 1
    assert len(payload["resolved"]) == 1
    assert payload["errors"] == []
    assert len(calls) == 2
