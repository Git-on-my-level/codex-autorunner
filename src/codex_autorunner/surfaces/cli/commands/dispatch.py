import json
from pathlib import Path
from typing import Callable, Optional

import httpx
import typer

from ....core.config import HubConfig
from .utils import raise_exit


def register_dispatch_commands(
    dispatch_app: typer.Typer,
    *,
    require_hub_config_func: Callable[[Optional[Path]], HubConfig],
    build_server_url_func: Callable,
    request_json_func: Callable,
    request_form_json_func: Callable,
) -> None:
    @dispatch_app.command("reply")
    def hub_dispatch_reply(
        repo_id: str = typer.Option(..., "--repo-id", help="Hub repo id"),
        run_id: str = typer.Option(..., "--run-id", help="Flow run id (UUID)"),
        message: Optional[str] = typer.Option(
            None, "--message", help="Reply message body"
        ),
        message_file: Optional[Path] = typer.Option(
            None, "--message-file", help="Read reply message body from file"
        ),
        resume: bool = typer.Option(
            True, "--resume/--no-resume", help="Resume run after posting reply"
        ),
        idempotency_key: Optional[str] = typer.Option(
            None, "--idempotency-key", help="Optional key to avoid duplicate replies"
        ),
        path: Optional[Path] = typer.Option(
            None, "--path", "--hub", help="Hub root path"
        ),
        base_path: Optional[str] = typer.Option(
            None, "--base-path", help="Override hub server base path (e.g. /car)"
        ),
        output_json: bool = typer.Option(
            True, "--json/--no-json", help="Emit JSON output (default: true)"
        ),
        pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output"),
    ):
        config = require_hub_config_func(path)

        if bool(message) == bool(message_file):
            raise_exit("Provide exactly one of --message or --message-file.")

        raw_message = message
        if message_file is not None:
            try:
                raw_message = message_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise_exit(f"Failed to read message file: {exc}", cause=exc)
        body = (raw_message or "").strip()
        if not body:
            raise_exit("Reply message cannot be empty.")

        thread_url = build_server_url_func(
            config,
            f"/repos/{repo_id}/api/messages/threads/{run_id}",
            base_path_override=base_path,
        )
        reply_url = build_server_url_func(
            config,
            f"/repos/{repo_id}/api/messages/{run_id}/reply",
            base_path_override=base_path,
        )
        resume_url = build_server_url_func(
            config,
            f"/repos/{repo_id}/api/flows/{run_id}/resume",
            base_path_override=base_path,
        )
        inbox_url = build_server_url_func(
            config, "/hub/messages?limit=200", base_path_override=base_path
        )

        marker = None
        if idempotency_key:
            marker = f"<!-- car-idempotency-key:{idempotency_key.strip()} -->"

        try:
            thread = request_json_func(
                "GET", thread_url, token_env=config.server_auth_token_env
            )
        except (
            httpx.HTTPError,
            httpx.ConnectError,
            httpx.TimeoutException,
            OSError,
        ) as exc:
            raise_exit(
                "Failed to query run thread via hub server. Ensure 'car hub serve' is running.\n"
                f"Attempted: {thread_url}\n"
                "If the hub UI is served under a base path (commonly /car), either set "
                "`server.base_path` in the hub config or pass `--base-path /car`.",
                cause=exc,
            )

        run_status = (
            (thread.get("run") or {}) if isinstance(thread, dict) else {}
        ).get("status")
        if run_status != "paused":
            fallback_status = None
            try:
                inbox = request_json_func(
                    "GET", inbox_url, token_env=config.server_auth_token_env
                )
                items = inbox.get("items", []) if isinstance(inbox, dict) else []
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("repo_id") or "") != repo_id:
                        continue
                    if str(item.get("run_id") or "") != run_id:
                        continue
                    fallback_status = item.get("status")
                    break
            except Exception:
                fallback_status = None

            if run_status is None and fallback_status == "paused":
                run_status = "paused"
            else:
                hint = ""
                if fallback_status is not None and fallback_status != run_status:
                    hint = f" (hub inbox sees status={fallback_status})"
                raise_exit(
                    f"Run {run_id} is not paused-awaiting-input (status={run_status or 'unknown'}).{hint}"
                )

        duplicate = False
        reply_seq = None
        if marker:
            replies = (
                thread.get("reply_history", []) if isinstance(thread, dict) else []
            )
            for entry in replies if isinstance(replies, list) else []:
                reply = entry.get("reply") if isinstance(entry, dict) else None
                existing_body = (
                    (reply.get("body") or "") if isinstance(reply, dict) else ""
                )
                if marker in existing_body:
                    duplicate = True
                    reply_seq = entry.get("seq") if isinstance(entry, dict) else None
                    break

        if not duplicate:
            post_body = body
            if marker:
                post_body = f"{body}\n\n{marker}"
            try:
                reply_resp = request_form_json_func(
                    "POST",
                    reply_url,
                    form={"body": post_body},
                    token_env=config.server_auth_token_env,
                    force_multipart=True,
                )
                reply_seq = reply_resp.get("seq")
            except (
                httpx.HTTPError,
                httpx.ConnectError,
                httpx.TimeoutException,
                OSError,
            ) as exc:
                raise_exit("Failed to post dispatch reply.", cause=exc)

        resumed = False
        resume_status = None
        if resume:
            try:
                resume_resp = request_json_func(
                    "POST",
                    resume_url,
                    payload={},
                    token_env=config.server_auth_token_env,
                )
                resumed = True
                resume_status = resume_resp.get("status")
            except (
                httpx.HTTPError,
                httpx.ConnectError,
                httpx.TimeoutException,
                OSError,
            ) as exc:
                raise_exit("Reply posted but resume failed.", cause=exc)

        payload = {
            "repo_id": repo_id,
            "run_id": run_id,
            "reply_seq": reply_seq,
            "duplicate": duplicate,
            "resumed": resumed,
            "resume_status": resume_status,
        }

        if output_json:
            typer.echo(json.dumps(payload, indent=2 if pretty else None))
            return

        typer.echo(
            f"Reply {'reused' if duplicate else 'posted'} for run {run_id}"
            + (f" (seq={reply_seq})" if reply_seq else "")
        )
        if resume:
            typer.echo(f"Run resumed: status={resume_status or 'unknown'}")
