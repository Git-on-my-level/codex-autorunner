import json
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import typer


def register_inbox_commands(
    app: typer.Typer,
    *,
    require_hub_config: Callable[[Optional[Path]], Any],
    build_server_url: Callable[..., str],
    request_json: Callable[..., dict],
    raise_exit: Callable[..., None],
) -> None:
    @app.command("resolve")
    def hub_inbox_resolve(
        repo_id: str = typer.Option(..., "--repo-id", help="Hub repo id"),
        run_id: str = typer.Option(..., "--run-id", help="Flow run id"),
        seq: Optional[int] = typer.Option(
            None, "--seq", help="Dispatch sequence number"
        ),
        item_type: Optional[str] = typer.Option(
            None, "--item-type", help="Inbox item type (auto-detected when omitted)"
        ),
        action: str = typer.Option("dismiss", "--action", help="Resolution action"),
        reason: Optional[str] = typer.Option(
            None, "--reason", help="Resolution reason"
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
        config = require_hub_config(path)
        if action != "dismiss":
            raise_exit("Only --action dismiss is currently supported.")

        payload: dict[str, Any] = {
            "repo_id": repo_id,
            "run_id": run_id,
            "action": action,
        }
        if seq is not None:
            payload["seq"] = seq
        if item_type:
            payload["item_type"] = item_type
        if reason:
            payload["reason"] = reason

        resolve_url = build_server_url(
            config, "/hub/messages/resolve", base_path_override=base_path
        )
        try:
            resolved = request_json(
                "POST",
                resolve_url,
                payload=payload,
                token_env=config.server_auth_token_env,
            )
        except (
            httpx.HTTPError,
            httpx.ConnectError,
            httpx.TimeoutException,
            OSError,
        ) as exc:
            raise_exit("Failed to resolve hub inbox item.", cause=exc)

        if output_json:
            typer.echo(json.dumps(resolved, indent=2 if pretty else None))
            return
        resolved_payload = (
            resolved.get("resolved", {}) if isinstance(resolved, dict) else {}
        )
        typer.echo(
            "Resolved inbox item: "
            f"repo={resolved_payload.get('repo_id')} run={resolved_payload.get('run_id')} "
            f"type={resolved_payload.get('item_type')} seq={resolved_payload.get('seq')}"
        )

    @app.command("clear")
    def hub_inbox_clear(
        stale: bool = typer.Option(
            False, "--stale", help="Clear stale non-dispatch attention items"
        ),
        repo_id: Optional[str] = typer.Option(None, "--repo-id", help="Hub repo id"),
        run_id: Optional[str] = typer.Option(None, "--run-id", help="Flow run id"),
        seq: Optional[int] = typer.Option(
            None, "--seq", help="Dispatch sequence number"
        ),
        dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
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
        if not stale and not (repo_id and run_id):
            raise_exit("Pass either --stale or both --repo-id and --run-id.")

        config = require_hub_config(path)
        message_limit = 0 if (repo_id and run_id) else 2000
        list_url = build_server_url(
            config, f"/hub/messages?limit={message_limit}", base_path_override=base_path
        )
        resolve_url = build_server_url(
            config, "/hub/messages/resolve", base_path_override=base_path
        )

        try:
            messages_payload = request_json(
                "GET", list_url, token_env=config.server_auth_token_env
            )
        except (
            httpx.HTTPError,
            httpx.ConnectError,
            httpx.TimeoutException,
            OSError,
        ) as exc:
            raise_exit("Failed to list hub inbox items.", cause=exc)

        items = (
            messages_payload.get("items", [])
            if isinstance(messages_payload, dict)
            else []
        )
        selected = _filter_inbox_items_for_clear(
            items if isinstance(items, list) else [],
            stale=stale,
            repo_id=repo_id,
            run_id=run_id,
            seq=seq,
        )

        result_payload: dict[str, Any] = {
            "dry_run": dry_run,
            "requested": {
                "stale": stale,
                "repo_id": repo_id,
                "run_id": run_id,
                "seq": seq,
            },
            "selected_count": len(selected),
            "selected": [
                {
                    "repo_id": item.get("repo_id"),
                    "run_id": item.get("run_id"),
                    "item_type": item.get("item_type"),
                    "seq": item.get("seq"),
                }
                for item in selected
            ],
            "resolved": [],
            "errors": [],
        }

        if not dry_run:
            for item in selected:
                payload = {
                    "repo_id": item.get("repo_id"),
                    "run_id": item.get("run_id"),
                    "item_type": item.get("item_type"),
                    "action": "dismiss",
                    "reason": "cleared via car hub inbox clear",
                    "actor": "cli:hub_inbox_clear",
                }
                if item.get("seq") is not None:
                    payload["seq"] = item.get("seq")
                try:
                    resolved = request_json(
                        "POST",
                        resolve_url,
                        payload=payload,
                        token_env=config.server_auth_token_env,
                    )
                    result_payload["resolved"].append(resolved.get("resolved"))
                except Exception as exc:
                    result_payload["errors"].append(
                        {
                            "repo_id": item.get("repo_id"),
                            "run_id": item.get("run_id"),
                            "item_type": item.get("item_type"),
                            "seq": item.get("seq"),
                            "error": str(exc),
                        }
                    )

        if output_json:
            typer.echo(json.dumps(result_payload, indent=2 if pretty else None))
            if result_payload["errors"]:
                raise typer.Exit(code=1)
            return

        typer.echo(
            f"Inbox clear selected={result_payload['selected_count']} "
            f"resolved={len(result_payload['resolved'])} "
            f"errors={len(result_payload['errors'])} dry_run={dry_run}"
        )
        if result_payload["errors"]:
            raise_exit("hub inbox clear encountered errors.")


def _filter_inbox_items_for_clear(
    items: list[dict[str, Any]],
    *,
    stale: bool,
    repo_id: Optional[str],
    run_id: Optional[str],
    seq: Optional[int],
) -> list[dict[str, Any]]:
    stale_types = {"run_failed", "run_stopped", "run_state_attention"}
    selected: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if repo_id and str(item.get("repo_id") or "") != repo_id:
            continue
        if run_id and str(item.get("run_id") or "") != run_id:
            continue
        item_seq = item.get("seq")
        if seq is not None and item_seq != seq:
            continue
        if stale and str(item.get("item_type") or "") not in stale_types:
            continue
        selected.append(item)
    return selected
