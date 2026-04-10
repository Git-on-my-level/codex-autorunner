from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ...chat.agents import (
    build_agent_switch_state,
    chat_hermes_profile_options,
    normalize_chat_agent,
)
from ...chat.model_selection import (
    REASONING_EFFORT_VALUES,
    _coerce_model_entries,
    _display_name_is_model_alias,
    _is_valid_opencode_model_name,
    format_model_set_message,
)
from ..components import (
    DISCORD_SELECT_OPTION_MAX_OPTIONS,
    build_agent_picker,
    build_agent_profile_picker,
    build_model_picker,
)
from ..rendering import format_discord_message

MODEL_SEARCH_FETCH_LIMIT = 200
AGENT_PROFILE_SELECT_ID = "agent_profile_select"

_VALID_REASONING_EFFORTS = REASONING_EFFORT_VALUES


class _AppServerUnavailableError(Exception):
    pass


async def _interaction_deferred(
    service: Any,
    interaction_id: str,
    interaction_token: str,
) -> bool:
    if service._interaction_has_initial_response(interaction_token):
        return True
    return bool(
        await service._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
    )


def _coerce_model_picker_items(
    result: Any,
    *,
    limit: Optional[int] = None,
) -> list[tuple[str, str]]:
    entries = _coerce_model_entries(result)
    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    item_limit = max(
        1,
        limit if isinstance(limit, int) else DISCORD_SELECT_OPTION_MAX_OPTIONS - 1,
    )
    for entry in entries:
        model_id = entry.get("model") or entry.get("id")
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        display_name = entry.get("displayName")
        label = model_id
        if (
            isinstance(display_name, str)
            and display_name
            and not _display_name_is_model_alias(model_id, display_name)
        ):
            label = f"{model_id} ({display_name})"
        options.append((model_id, label))
        if len(options) >= item_limit:
            break
    return options


async def handle_car_agent(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    options: dict[str, Any],
) -> None:
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        text = format_discord_message(
            "This channel is not bound. Run `/car bind path:<...>` first."
        )
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            text,
        )
        return

    current_agent, current_profile = service._resolve_agent_state(binding)
    agent_name = options.get("name")
    profile_name = options.get("profile")

    def _agent_picker_components(
        *,
        agent: str,
        profile: Optional[str],
    ) -> list[dict[str, Any]]:
        components = [build_agent_picker(current_agent=agent, context=service)]
        if agent == "hermes" and chat_hermes_profile_options(service):
            components.append(
                build_agent_profile_picker(
                    current_profile=profile,
                    context=service,
                    custom_id=AGENT_PROFILE_SELECT_ID,
                )
            )
        return components

    if not agent_name and not profile_name:
        lines = [f"Current agent: {current_agent}"]
        if current_agent == "hermes":
            lines.append(f"Hermes profile: {current_profile or '(default)'}")
        lines.extend(["", "Select an agent:"])
        await service._respond_with_components(
            interaction_id,
            interaction_token,
            format_discord_message("\n".join(lines)),
            _agent_picker_components(
                agent=current_agent,
                profile=current_profile,
            ),
        )
        return

    desired_agent = current_agent
    if agent_name:
        desired_agent = normalize_chat_agent(agent_name, context=service) or ""
    if not desired_agent:
        available = ", ".join(service._known_agent_values())
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Invalid agent '{agent_name}'. Valid options: {available}",
        )
        return

    desired_profile = current_profile if desired_agent == "hermes" else None
    if profile_name is not None:
        if desired_agent != "hermes":
            await service._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Hermes profiles can only be selected when the active agent is Hermes.",
            )
            return
        normalized_profile_name = str(profile_name).strip().lower()
        if normalized_profile_name in {"", "clear", "reset", "default"}:
            desired_profile = None
        else:
            desired_profile = service._normalize_agent_profile(profile_name)
            if desired_profile is None:
                available_profiles = ", ".join(
                    option.profile for option in chat_hermes_profile_options(service)
                )
                suffix = (
                    f" Known Hermes profiles: {available_profiles}."
                    if available_profiles
                    else ""
                )
                await service._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    f"Unknown Hermes profile '{profile_name}'.{suffix}",
                )
                return

    if desired_agent == current_agent and desired_profile == current_profile:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            (
                f"Agent already set to {service._format_agent_state(current_agent, current_profile)}."
            ),
        )
        return

    switch_state = build_agent_switch_state(
        desired_agent,
        desired_profile,
        model_reset="clear",
        context=service,
    )
    agent_changed = switch_state.agent != current_agent
    if agent_changed:
        await service._store.update_agent_state(
            channel_id=channel_id,
            agent=switch_state.agent,
            agent_profile=switch_state.profile,
            model_override=switch_state.model,
            reasoning_effort=switch_state.effort,
        )
        await service._store.clear_pending_compact_seed(channel_id=channel_id)
    else:
        await service._store.update_agent_state(
            channel_id=channel_id,
            agent=switch_state.agent,
            agent_profile=switch_state.profile,
        )

    selection_label = service._format_agent_state(
        switch_state.agent,
        switch_state.profile,
    )
    if (
        switch_state.agent == "hermes"
        and agent_changed
        and profile_name is None
        and chat_hermes_profile_options(service)
    ):
        await service._respond_with_components(
            interaction_id,
            interaction_token,
            format_discord_message(
                "\n".join(
                    [
                        f"Agent set to {selection_label}.",
                        "",
                        "Select a Hermes profile to override the default runtime, or leave it at `(default profile)`.",
                    ]
                )
            ),
            _agent_picker_components(
                agent=switch_state.agent,
                profile=switch_state.profile,
            ),
        )
        return

    await service._respond_ephemeral(
        interaction_id,
        interaction_token,
        f"Agent set to {selection_label}. Will apply on the next turn.",
    )


async def handle_car_model(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str],
    options: dict[str, Any],
) -> None:
    from ..service import log_event

    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        text = format_discord_message(
            "This channel is not bound. Run `/car bind path:<...>` first."
        )
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            text,
        )
        return

    current_agent, _current_profile = service._resolve_agent_state(binding)
    current_model = binding.get("model_override")
    if not isinstance(current_model, str) or not current_model.strip():
        current_model = None
    current_effort = binding.get("reasoning_effort")
    model_name = options.get("name")
    effort = options.get("effort")

    if not model_name:
        deferred = await _interaction_deferred(
            service,
            interaction_id,
            interaction_token,
        )

        def _fallback_model_text(note: Optional[str] = None) -> str:
            lines = [
                f"Current agent: {current_agent}",
                f"Current model: {current_model or '(default)'}",
            ]
            if isinstance(current_effort, str) and current_effort.strip():
                lines.append(f"Reasoning effort: {current_effort}")
            if note:
                lines.extend(["", note])
            lines.extend(
                [
                    "",
                    "Use `/car model name:<id>` to set a model.",
                    "Use `/car model name:<id> effort:<value>` when the current agent supports reasoning effort.",
                    "",
                    f"Valid efforts: {', '.join(_VALID_REASONING_EFFORTS)}",
                ]
            )
            return format_discord_message("\n".join(lines))

        async def _send_model_picker_or_fallback(
            text: str,
            *,
            components: Optional[list[dict[str, Any]]] = None,
        ) -> None:
            if deferred:
                sent = await service._send_followup_ephemeral(
                    interaction_token=interaction_token,
                    content=text,
                    components=components,
                )
                if sent:
                    return
            if components:
                await service._respond_with_components(
                    interaction_id,
                    interaction_token,
                    text,
                    components,
                )
                return
            await service._respond_ephemeral(
                interaction_id,
                interaction_token,
                text,
            )

        supports_model_listing = service._agent_supports_capability(
            current_agent, "model_listing"
        )
        if not supports_model_listing:
            supported_agents = service._agents_supporting_capability("model_listing")
            supported_hint = (
                f" Switch to an agent with model picker support: {', '.join(supported_agents)}."
                if supported_agents
                else ""
            )
            await _send_model_picker_or_fallback(
                _fallback_model_text(
                    f"{service._agent_display_name(current_agent)} does not expose a model catalog in Discord.{supported_hint}"
                ),
            )
            return

        if current_agent == "opencode":
            try:
                model_items = await service._list_opencode_models_for_picker(
                    workspace_path=binding.get("workspace_path")
                )
            except Exception as exc:  # intentional: external API call for model listing
                log_event(
                    service._logger,
                    logging.WARNING,
                    "discord.model.list.failed",
                    channel_id=channel_id,
                    agent=current_agent,
                    exc=exc,
                )
                await _send_model_picker_or_fallback(
                    _fallback_model_text("Failed to list models for picker."),
                )
                return
            if model_items is None:
                await _send_model_picker_or_fallback(
                    _fallback_model_text(
                        "Workspace unavailable for model picker. Re-bind this channel with `/car bind` and try again."
                    ),
                )
                return
            if not model_items and not current_model:
                await _send_model_picker_or_fallback(
                    _fallback_model_text("No models found from OpenCode."),
                )
                return
        else:
            try:
                client = await service._client_for_workspace(
                    binding.get("workspace_path")
                )
            except _AppServerUnavailableError as exc:
                log_event(
                    service._logger,
                    logging.WARNING,
                    "discord.model.list.failed",
                    channel_id=channel_id,
                    agent=current_agent,
                    exc=exc,
                )
                await _send_model_picker_or_fallback(
                    _fallback_model_text(
                        "Model picker unavailable right now (app server unavailable)."
                    ),
                )
                return
            if client is None:
                await _send_model_picker_or_fallback(
                    _fallback_model_text(
                        "Workspace unavailable for model picker. Re-bind this channel with `/car bind` and try again."
                    ),
                )
                return
            try:
                from ...chat.model_selection import _model_list_with_agent_compat

                result = await _model_list_with_agent_compat(
                    client,
                    params={
                        "cursor": None,
                        "limit": DISCORD_SELECT_OPTION_MAX_OPTIONS,
                        "agent": current_agent,
                    },
                )
                model_items = _coerce_model_picker_items(result)
            except Exception as exc:  # intentional: external API call for model listing
                log_event(
                    service._logger,
                    logging.WARNING,
                    "discord.model.list.failed",
                    channel_id=channel_id,
                    agent=current_agent,
                    exc=exc,
                )
                await _send_model_picker_or_fallback(
                    _fallback_model_text("Failed to list models for picker."),
                )
                return

            if not model_items and not current_model:
                await _send_model_picker_or_fallback(
                    _fallback_model_text("No models found from the app server."),
                )
                return

        lines = [
            f"Current agent: {current_agent}",
            f"Current model: {current_model or '(default)'}",
        ]
        if isinstance(current_effort, str) and current_effort.strip():
            lines.append(f"Reasoning effort: {current_effort}")
        lines.extend(
            [
                "",
                "Select a model override:",
                "(default model) clears the override.",
                "Use `/car model name:<id> effort:<value>` when the current agent supports reasoning effort.",
            ]
        )
        await _send_model_picker_or_fallback(
            format_discord_message("\n".join(lines)),
            components=[
                build_model_picker(
                    model_items,
                    current_model=current_model,
                )
            ],
        )
        return

    model_name = model_name.strip()
    if model_name.lower() in ("clear", "reset"):
        await service._store.update_model_state(channel_id=channel_id, clear_model=True)
        await service._respond_ephemeral(
            interaction_id, interaction_token, "Model override cleared."
        )
        return

    available_model_items: Optional[list[tuple[str, str]]] = None
    try:
        available_model_items = await service._list_model_items_for_binding(
            binding=binding,
            agent=current_agent,
            limit=MODEL_SEARCH_FETCH_LIMIT,
        )
    except (
        Exception
    ):  # intentional: best-effort model listing, gracefully degrades to None
        available_model_items = None

    if available_model_items:

        async def _prompt_model_matches(
            query_text: str,
            filtered_items: list[tuple[str, str]],
        ) -> None:
            lines = [
                f"Current agent: {current_agent}",
                f"Current model: {current_model or '(default)'}",
                "",
                f"Matched {len(filtered_items)} models for `{query_text}`:",
                "Select a model override:",
                "(default model) clears the override.",
            ]
            if isinstance(current_effort, str) and current_effort.strip():
                lines.insert(2, f"Reasoning effort: {current_effort}")
            await service._respond_with_components(
                interaction_id,
                interaction_token,
                format_discord_message("\n".join(lines)),
                [
                    build_model_picker(
                        filtered_items,
                        current_model=current_model,
                    )
                ],
            )

        resolved_model_name = await service._resolve_picker_query_or_prompt(
            query=model_name,
            items=available_model_items,
            limit=max(1, DISCORD_SELECT_OPTION_MAX_OPTIONS - 1),
            prompt_filtered_items=_prompt_model_matches,
        )
        if resolved_model_name is None:
            return
        model_name = resolved_model_name

    if current_agent == "opencode" and not _is_valid_opencode_model_name(model_name):
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "OpenCode model must be in `provider/model` format.",
        )
        return

    if effort:
        if not service._agent_supports_effort(current_agent):
            await service._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Reasoning effort is not supported for {service._agent_display_name(current_agent)}.",
            )
            return
        effort = effort.lower().strip()
        if effort not in _VALID_REASONING_EFFORTS:
            await service._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Invalid effort '{effort}'. Valid options: {', '.join(_VALID_REASONING_EFFORTS)}",
            )
            return

    await service._store.update_model_state(
        channel_id=channel_id,
        model_override=model_name,
        reasoning_effort=effort,
    )

    await service._respond_ephemeral(
        interaction_id,
        interaction_token,
        format_model_set_message(model_name, effort=effort),
    )


async def handle_car_experimental(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
) -> None:
    action = options.get("action", "")
    feature = options.get("feature", "")

    if not isinstance(action, str):
        action = ""
    action = action.strip().lower()

    if not isinstance(feature, str):
        feature = ""
    feature = feature.strip()

    usage_text = (
        "Usage:\n"
        "- `/car admin experimental action:list`\n"
        "- `/car admin experimental action:enable feature:<feature>`\n"
        "- `/car admin experimental action:disable feature:<feature>`"
    )

    if not action or action in ("list", "ls", "all"):
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Experimental features listing requires the app server client.\n\n"
            f"{usage_text}",
        )
        return

    if action in ("enable", "on", "true"):
        if not feature:
            await service._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Missing feature for `enable`.\n\n{usage_text}",
            )
            return
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Feature `{feature}` enable requested.\n\n"
            "Note: Feature toggling requires the app server client.",
        )
        return

    if action in ("disable", "off", "false"):
        if not feature:
            await service._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Missing feature for `disable`.\n\n{usage_text}",
            )
            return
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Feature `{feature}` disable requested.\n\n"
            "Note: Feature toggling requires the app server client.",
        )
        return

    await service._respond_ephemeral(
        interaction_id,
        interaction_token,
        f"Unknown action: {action}.\n\nValid actions: list, enable, disable.\n\n{usage_text}",
    )


async def handle_car_rollout(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
) -> None:
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Channel not bound. Use /car bind first.",
        )
        return

    rollout_path = binding.get("rollout_path")
    workspace_path = binding.get("workspace_path", "unknown")

    if rollout_path:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Rollout path: {rollout_path}\nWorkspace: {workspace_path}",
        )
    else:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"No rollout path available.\nWorkspace: {workspace_path}\n\n"
            "The rollout path is set after a conversation turn completes.",
        )
