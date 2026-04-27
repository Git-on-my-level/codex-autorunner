"""Shared PMA control-plane client, capability checks, and managed-thread helpers.

This module owns HTTP transport, agent capability resolution, managed-thread
send/response parsing, and send-timeout recovery semantics used by the PMA CLI
and potentially other PMA surfaces.  CLI-only text rendering and command wiring
remain in ``pma_cli.py``.
"""

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
import typer

from ...agents.registry import get_registered_agents
from ...core.config import load_hub_config
from ...core.config_contract import ConfigError
from ...core.text_utils import _redacted_prompt_preview_for_match

logger = logging.getLogger(__name__)

MANAGED_THREAD_SEND_PREVIEW_LIMIT = 120
MANAGED_THREAD_SEND_REQUEST_TIMEOUT_SECONDS = 30.0
MANAGED_THREAD_SEND_TIMEOUT_STATUS_LIMIT = 50
MANAGED_THREAD_SEND_TIMEOUT_RECOVERY_WINDOW_SECONDS = 15.0
MANAGED_THREAD_SEND_TIMEOUT_RECOVERY_POLL_SECONDS = 0.25

CAPABILITY_REQUIREMENTS = {
    "models": "model_listing",
    "interrupt": "interrupt",
    "thread_interrupt": "interrupt",
    "thread_send": "message_turns",
    "thread_turns": "transcript_history",
    "thread_output": "transcript_history",
    "thread_tail": "event_streaming",
    "thread_compact": "message_turns",
    "thread_resume": "durable_threads",
    "thread_archive": "durable_threads",
    "thread_spawn": "durable_threads",
    "review": "review",
}


def coerce_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def build_pma_url(config, path: str) -> str:
    base_path = config.server_base_path or ""
    if base_path.endswith("/") and path.startswith("/"):
        base_path = base_path[:-1]
    return f"http://{config.server_host}:{config.server_port}{base_path}/hub/pma{path}"


def auth_headers_from_env(token_env: Optional[str]) -> Optional[dict[str, str]]:
    if not token_env:
        return None
    token = os.environ.get(token_env)
    if token and token.strip():
        return {"Authorization": f"Bearer {token.strip()}"}
    return None


def request_json(
    method: str,
    url: str,
    payload: Optional[dict] = None,
    token_env: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
) -> dict:
    headers = None
    if token_env:
        token = os.environ.get(token_env)
        if token and token.strip():
            headers = {"Authorization": f"Bearer {token.strip()}"}
    response = httpx.request(
        method,
        url,
        json=payload,
        params=params,
        timeout=30.0,
        headers=headers,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def request_json_with_status(
    method: str,
    url: str,
    payload: Optional[dict[str, Any]] = None,
    token_env: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    headers = auth_headers_from_env(token_env)
    response = httpx.request(
        method,
        url,
        json=payload,
        params=params,
        headers=headers,
        timeout=timeout,
    )
    data: dict[str, Any] = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            data = parsed
    except ValueError:
        data = {}
    return response.status_code, data


def fetch_agent_capabilities(
    config, path: Optional[Path] = None
) -> dict[str, list[str]]:
    url = build_pma_url(config, "/agents")
    try:
        data = request_json("GET", url, token_env=config.server_auth_token_env)
    except Exception:  # best-effort capability fetch
        return {}
    agents = data.get("agents", []) if isinstance(data, dict) else []
    return {
        agent.get("id", ""): agent.get("capabilities", [])
        for agent in agents
        if isinstance(agent, dict)
    }


def check_capability(
    agent_id: str,
    capability: str,
    capabilities: dict[str, list[str]],
) -> bool:
    agent_caps = capabilities.get(agent_id, [])
    return capability in agent_caps


def normalize_agent_option(agent: Optional[str]) -> Optional[str]:
    if agent is None:
        return None
    normalized = agent.strip().lower()
    if not normalized:
        return None
    registered = get_registered_agents()
    if normalized not in registered:
        available = ", ".join(sorted(registered.keys()))
        typer.echo(
            f"--agent must be a registered agent. Available: {available}",
            err=True,
        )
        raise typer.Exit(code=1) from None
    return normalized


def normalize_resource_owner_options(
    *,
    repo_id: Optional[str],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    workspace_root: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    normalized_repo_id = (
        repo_id.strip() if isinstance(repo_id, str) and repo_id.strip() else None
    )
    normalized_resource_kind = (
        resource_kind.strip().lower()
        if isinstance(resource_kind, str) and resource_kind.strip()
        else None
    )
    normalized_resource_id = (
        resource_id.strip()
        if isinstance(resource_id, str) and resource_id.strip()
        else None
    )
    normalized_workspace_root = (
        workspace_root.strip()
        if isinstance(workspace_root, str) and workspace_root.strip()
        else None
    )
    repo_present = normalized_repo_id is not None
    resource_present = (
        normalized_resource_kind is not None or normalized_resource_id is not None
    )
    workspace_present = normalized_workspace_root is not None

    if normalized_resource_id and normalized_resource_kind is None:
        typer.echo(
            "--resource-kind is required when --resource-id is provided", err=True
        )
        raise typer.Exit(code=1) from None
    if normalized_resource_kind and normalized_resource_id is None:
        typer.echo(
            "--resource-id is required when --resource-kind is provided", err=True
        )
        raise typer.Exit(code=1) from None
    if normalized_resource_kind not in {None, "repo", "agent_workspace"}:
        typer.echo("--resource-kind must be one of: repo, agent_workspace", err=True)
        raise typer.Exit(code=1) from None
    if normalized_repo_id and normalized_resource_kind not in {None, "repo"}:
        typer.echo(
            "--repo cannot be combined with a non-repo --resource-kind",
            err=True,
        )
        raise typer.Exit(code=1) from None
    if (
        normalized_repo_id
        and normalized_resource_id
        and normalized_resource_id != normalized_repo_id
    ):
        typer.echo("--repo must match --resource-id for repo-backed requests", err=True)
        raise typer.Exit(code=1) from None
    if (
        sum(
            1
            for present in (repo_present, resource_present, workspace_present)
            if present
        )
        > 1
    ):
        typer.echo(
            "Choose exactly one of --repo, --resource-kind/--resource-id, or --workspace-root",
            err=True,
        )
        raise typer.Exit(code=1) from None
    if normalized_repo_id and normalized_resource_kind is None:
        normalized_resource_kind = "repo"
        normalized_resource_id = normalized_repo_id

    return (
        normalized_resource_kind,
        normalized_resource_id,
        normalized_workspace_root,
    )


def format_resource_owner_label(item: dict[str, Any]) -> str:
    resource_kind = str(item.get("resource_kind") or "").strip()
    resource_id = str(item.get("resource_id") or "").strip()
    repo_id = str(item.get("repo_id") or "").strip()
    if resource_kind and resource_id:
        if resource_kind == "repo":
            return f"repo={resource_id}"
        return f"owner={resource_kind}:{resource_id}"
    if repo_id:
        return f"repo={repo_id}"
    workspace_root = str(item.get("workspace_root") or "").strip()
    if workspace_root:
        return f"workspace={workspace_root}"
    return "owner=-"


def normalize_notify_on(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip().lower()
    if not text:
        return None
    if text != "terminal":
        raise typer.BadParameter("notify-on must be 'terminal'")
    return text


def resolve_hub_path(path: Optional[Path]) -> Path:
    start = path or Path.cwd()
    try:
        return load_hub_config(start).root
    except (OSError, ValueError, ConfigError, AttributeError):
        candidate = start.resolve()
        if candidate.is_file():
            parent = candidate.parent
            if parent.name == ".codex-autorunner":
                return parent.parent.resolve()
            return parent.resolve()
        return candidate


@dataclass(frozen=True)
class ManagedThreadSendRequest:
    message: str
    busy_policy: str
    defer_execution: bool
    wait_for_confirmation: bool = True
    model: Optional[str] = None
    reasoning: Optional[str] = None
    notify_on: Optional[str] = None
    notify_lane: Optional[str] = None
    notify_once: bool = True

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "message": self.message,
            "busy_policy": self.busy_policy,
            "defer_execution": self.defer_execution,
            "wait_for_confirmation": self.wait_for_confirmation,
        }
        if self.model:
            payload["model"] = self.model
        if self.reasoning:
            payload["reasoning"] = self.reasoning
        if self.notify_on:
            payload["notify_on"] = self.notify_on
            payload["notify_lane"] = self.notify_lane
            payload["notify_once"] = self.notify_once
        return payload


@dataclass(frozen=True)
class ManagedThreadSendResponse:
    status_code: int
    status: str
    send_state: str
    execution_state: str
    managed_turn_id: str
    active_managed_turn_id: str
    queue_depth: Optional[int]
    delivered_message: str
    assistant_text: str
    detail: str
    error: str
    next_step: str
    notification_mode: str
    notification_subscription_id: str
    notification_lane: str

    @classmethod
    def from_http(
        cls, status_code: int, data: Any, *, default_message: str
    ) -> "ManagedThreadSendResponse":
        payload = data if isinstance(data, dict) else {}
        return cls(
            status_code=status_code,
            status=str(payload.get("status") or ""),
            send_state=str(payload.get("send_state") or "").strip().lower(),
            execution_state=str(payload.get("execution_state") or "").strip().lower(),
            managed_turn_id=str(payload.get("managed_turn_id") or ""),
            active_managed_turn_id=str(
                payload.get("active_managed_turn_id") or ""
            ).strip(),
            queue_depth=coerce_optional_int(payload.get("queue_depth")),
            delivered_message=str(payload.get("delivered_message") or default_message),
            assistant_text=str(payload.get("assistant_text") or ""),
            detail=str(payload.get("detail") or "").strip(),
            error=str(payload.get("error") or "").strip(),
            next_step=str(payload.get("next_step") or "").strip(),
            notification_mode=str(
                ((payload.get("notification") or {}).get("mode") or "")
            ).strip(),
            notification_subscription_id=str(
                (
                    ((payload.get("notification") or {}).get("subscription") or {}).get(
                        "subscription_id"
                    )
                    or ""
                )
            ).strip(),
            notification_lane=str(
                (
                    ((payload.get("notification") or {}).get("subscription") or {}).get(
                        "lane_id"
                    )
                    or ""
                )
            ).strip(),
        )

    @property
    def is_ok(self) -> bool:
        return self.status_code < 400 and self.status == "ok"

    def error_detail(self) -> str:
        return self.detail or self.error or "Managed thread send failed"

    def accepted_line(self) -> str:
        line = (
            f"send_state={self.send_state or 'accepted'} "
            f"managed_turn_id={self.managed_turn_id}"
        )
        if self.active_managed_turn_id:
            line += f" active_managed_turn_id={self.active_managed_turn_id}"
        if self.queue_depth is not None:
            line += f" queue_depth={self.queue_depth}"
        if self.notification_mode:
            line += f" subscription={self.notification_mode}"
            if self.notification_subscription_id:
                line += f" subscription_id={self.notification_subscription_id}"
            if self.notification_lane:
                line += f" lane={self.notification_lane}"
        return line

    def completion_line(self) -> str:
        return (
            f"send_state={self.send_state or 'accepted'} "
            f"managed_turn_id={self.managed_turn_id} "
            f"execution_state={self.execution_state or 'completed'}"
        ).strip()


@dataclass(frozen=True)
class ManagedThreadSendTimeoutProbe:
    last_turn_id: str
    last_message_preview: str
    active_managed_turn_id: str
    active_turn_status: str
    active_prompt_preview: str
    queue_depth: int
    queued_turn_ids: tuple[str, ...]
    queued_prompt_previews: tuple[str, ...]

    @classmethod
    def from_status(cls, data: Any) -> "ManagedThreadSendTimeoutProbe":
        payload = data if isinstance(data, dict) else {}
        raw_thread = payload.get("thread")
        thread: dict[str, Any] = raw_thread if isinstance(raw_thread, dict) else {}
        raw_turn = payload.get("turn")
        turn: dict[str, Any] = raw_turn if isinstance(raw_turn, dict) else {}
        raw_diagnostics = payload.get("active_turn_diagnostics")
        diagnostics = raw_diagnostics if isinstance(raw_diagnostics, dict) else {}
        raw_queued_turns = payload.get("queued_turns")
        queued_turns = raw_queued_turns if isinstance(raw_queued_turns, list) else []
        normalized_queued_turns = tuple(
            (
                str(item.get("managed_turn_id") or "").strip(),
                _redacted_prompt_preview_for_match(item.get("prompt_preview")),
            )
            for item in queued_turns
            if isinstance(item, dict) and str(item.get("managed_turn_id") or "").strip()
        )
        return cls(
            last_turn_id=str(
                thread.get("last_turn_id")
                or thread.get("latest_turn_id")
                or payload.get("latest_turn_id")
                or ""
            ).strip(),
            last_message_preview=_redacted_prompt_preview_for_match(
                thread.get("last_message_preview")
            ),
            active_managed_turn_id=str(turn.get("managed_turn_id") or "").strip(),
            active_turn_status=str(turn.get("status") or "").strip(),
            active_prompt_preview=_redacted_prompt_preview_for_match(
                diagnostics.get("prompt_preview")
            ),
            queue_depth=coerce_optional_int(payload.get("queue_depth")) or 0,
            queued_turn_ids=tuple(
                managed_turn_id for managed_turn_id, _ in normalized_queued_turns
            ),
            queued_prompt_previews=tuple(
                prompt_preview for _, prompt_preview in normalized_queued_turns
            ),
        )


def fetch_managed_thread_status_payload(
    config,
    *,
    managed_thread_id: str,
) -> dict[str, Any]:
    return request_json(
        "GET",
        build_pma_url(config, f"/threads/{managed_thread_id}/status"),
        token_env=config.server_auth_token_env,
        params={"limit": MANAGED_THREAD_SEND_TIMEOUT_STATUS_LIMIT},
    )


def capture_managed_thread_send_timeout_probe(
    config,
    *,
    managed_thread_id: str,
) -> Optional[ManagedThreadSendTimeoutProbe]:
    try:
        return ManagedThreadSendTimeoutProbe.from_status(
            fetch_managed_thread_status_payload(
                config,
                managed_thread_id=managed_thread_id,
            )
        )
    except (httpx.HTTPError, ValueError, OSError, TypeError):
        return None


def recover_managed_thread_send_timeout(
    config,
    *,
    managed_thread_id: str,
    message_body: str,
    baseline: Optional[ManagedThreadSendTimeoutProbe],
) -> Optional[ManagedThreadSendResponse]:
    if baseline is None:
        return None

    expected_preview = _redacted_prompt_preview_for_match(
        message_body,
        max_length=MANAGED_THREAD_SEND_PREVIEW_LIMIT,
    ).strip()
    if not expected_preview:
        return None

    deadline = time.monotonic() + MANAGED_THREAD_SEND_TIMEOUT_RECOVERY_WINDOW_SECONDS
    baseline_queued_ids = set(baseline.queued_turn_ids)
    while True:
        try:
            current = ManagedThreadSendTimeoutProbe.from_status(
                fetch_managed_thread_status_payload(
                    config,
                    managed_thread_id=managed_thread_id,
                )
            )
        except (httpx.HTTPError, ValueError, OSError, TypeError):
            if time.monotonic() >= deadline:
                return None
            time.sleep(MANAGED_THREAD_SEND_TIMEOUT_RECOVERY_POLL_SECONDS)
            continue

        recovered_turn_id = ""
        queued = False
        active_prompt_matches = (
            current.queue_depth == 0
            and bool(baseline.active_prompt_preview)
            and current.active_prompt_preview == expected_preview
            and (
                current.active_managed_turn_id != baseline.active_managed_turn_id
                or baseline.active_prompt_preview != expected_preview
            )
        )
        if current.last_turn_id != baseline.last_turn_id and current.last_turn_id:
            recovered_turn_id = current.last_turn_id
            queued = recovered_turn_id in current.queued_turn_ids or (
                bool(current.active_managed_turn_id)
                and current.active_managed_turn_id != recovered_turn_id
            )
        elif active_prompt_matches and current.active_managed_turn_id:
            recovered_turn_id = current.active_managed_turn_id
        elif (
            current.last_message_preview == expected_preview
            and baseline.last_message_preview != expected_preview
        ):
            preview_queued_match_id = next(
                (
                    managed_turn_id
                    for managed_turn_id, prompt_preview in zip(
                        current.queued_turn_ids, current.queued_prompt_previews
                    )
                    if prompt_preview == expected_preview
                    and managed_turn_id not in baseline_queued_ids
                ),
                "",
            )
            if preview_queued_match_id:
                recovered_turn_id = preview_queued_match_id
                queued = True
            else:
                recovered_turn_id = (
                    current.active_managed_turn_id or current.last_turn_id
                )
                queued = recovered_turn_id in current.queued_turn_ids or (
                    bool(current.active_managed_turn_id)
                    and current.active_managed_turn_id != recovered_turn_id
                )
        else:
            queued_match_id = next(
                (
                    managed_turn_id
                    for managed_turn_id, prompt_preview in zip(
                        current.queued_turn_ids, current.queued_prompt_previews
                    )
                    if prompt_preview == expected_preview
                    and managed_turn_id not in baseline_queued_ids
                ),
                "",
            )
            if (
                not queued_match_id
                and current.queue_depth > baseline.queue_depth
                and expected_preview in current.queued_prompt_previews
                and expected_preview not in baseline.queued_prompt_previews
            ):
                queued_match_id = next(
                    (
                        managed_turn_id
                        for managed_turn_id, prompt_preview in zip(
                            current.queued_turn_ids, current.queued_prompt_previews
                        )
                        if prompt_preview == expected_preview
                    ),
                    "",
                )
            if queued_match_id:
                recovered_turn_id = queued_match_id
                queued = True

        if recovered_turn_id:
            payload: dict[str, Any] = {
                "status": "ok",
                "send_state": "queued" if queued else "accepted",
                "execution_state": (
                    "queued" if queued else (current.active_turn_status or "running")
                ),
                "managed_turn_id": recovered_turn_id,
                "active_managed_turn_id": (
                    current.active_managed_turn_id if queued else recovered_turn_id
                ),
                "queue_depth": current.queue_depth,
                "delivered_message": message_body,
                "assistant_text": "",
                "detail": (
                    "Timed out waiting for send confirmation; recovered delivery "
                    "from thread status."
                ),
                "error": "",
                "next_step": (
                    "Use `car pma thread status` or `car pma thread tail` if you "
                    "want to watch execution progress."
                ),
            }
            return ManagedThreadSendResponse.from_http(
                200, payload, default_message=message_body
            )

        if time.monotonic() >= deadline:
            return None
        time.sleep(MANAGED_THREAD_SEND_TIMEOUT_RECOVERY_POLL_SECONDS)
