from __future__ import annotations

from typing import Any

from ..chat.automation_surface import (
    automation_status_for_chat,
    list_automations_for_chat,
    run_automation_for_chat,
    set_automation_enabled_for_chat,
)


async def handle_automation_list(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    options: dict[str, Any],
) -> None:
    _ = options
    await service.send_or_respond_ephemeral(
        interaction_id,
        interaction_token,
        list_automations_for_chat(service._config.root),
    )


async def handle_automation_status(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    options: dict[str, Any],
) -> None:
    await _send_result(
        service,
        interaction_id,
        interaction_token,
        lambda: automation_status_for_chat(service._config.root, _option_id(options)),
    )


async def handle_automation_run(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    options: dict[str, Any],
) -> None:
    await _send_result(
        service,
        interaction_id,
        interaction_token,
        lambda: run_automation_for_chat(
            service._config.root,
            _option_id(options),
            source="discord",
            supervisor=getattr(service, "_hub_supervisor", None),
        ),
    )


async def handle_automation_pause(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    options: dict[str, Any],
) -> None:
    await _send_result(
        service,
        interaction_id,
        interaction_token,
        lambda: set_automation_enabled_for_chat(
            service._config.root,
            _option_id(options),
            enabled=False,
        ),
    )


async def handle_automation_resume(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    options: dict[str, Any],
) -> None:
    await _send_result(
        service,
        interaction_id,
        interaction_token,
        lambda: set_automation_enabled_for_chat(
            service._config.root,
            _option_id(options),
            enabled=True,
        ),
    )


async def _send_result(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    build_text: Any,
) -> None:
    try:
        text = build_text()
    except KeyError as exc:
        text = f"Automation not found: {exc.args[0]}"
    except ValueError as exc:
        text = str(exc)
    await service.send_or_respond_ephemeral(interaction_id, interaction_token, text)


def _option_id(options: dict[str, Any]) -> str:
    value = options.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("automation id is required")
    return value.strip()


__all__ = [
    "handle_automation_list",
    "handle_automation_pause",
    "handle_automation_resume",
    "handle_automation_run",
    "handle_automation_status",
]
