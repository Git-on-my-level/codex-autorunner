from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence, cast

from ...core.flows import FlowRunStatus
from ...core.utils import canonicalize_path
from ...integrations.chat.command_diagnostics import (
    ActiveFlowInfo,
    build_status_text,
)
from ...integrations.chat.help_catalog import build_discord_help_lines
from ...manifest import load_manifest
from ..chat.approval_modes import (
    normalize_approval_mode,
    resolve_approval_mode_policies,
)
from ..chat.picker_filter import resolve_picker_query
from .car_autocomplete import (
    agent_workspace_autocomplete_value,
    repo_autocomplete_value,
    resolve_workspace_from_token,
    workspace_autocomplete_value,
)
from .collaboration_helpers import (
    build_collaboration_snippet_lines,
    collaboration_summary_lines,
    evaluate_collaboration_summary,
)
from .components import (
    DISCORD_SELECT_OPTION_MAX_OPTIONS,
    build_action_row,
    build_bind_picker,
    build_button,
)
from .rendering import format_discord_message

BIND_PAGE_CUSTOM_ID_PREFIX = "bind_page"


def _list_manifest_repos(
    service: Any,
) -> list[tuple[str, str]]:
    if not service._manifest_path or not service._manifest_path.exists():
        return []
    try:
        manifest = load_manifest(service._manifest_path, service._config.root)
        ordered: list[tuple[int, int, str, str]] = []
        for index, repo in enumerate(manifest.repos):
            if not repo.id:
                continue
            worktree_priority = 0 if repo.kind == "worktree" else 1
            ordered.append(
                (
                    worktree_priority,
                    -index,
                    repo.id,
                    str(service._config.root / repo.path),
                )
            )
        ordered.sort(key=lambda item: (item[0], item[1], item[2]))
        return [(repo_id, path) for _, _, repo_id, path in ordered]
    except Exception:
        return []


def _list_agent_workspaces(service: Any) -> list[tuple[str, str, str]]:
    supervisor = getattr(service, "_hub_supervisor", None)
    if supervisor is None:
        return []
    try:
        snapshots = supervisor.list_agent_workspaces()
    except Exception:
        return []
    workspaces: list[tuple[str, str, str]] = []
    for snapshot in snapshots:
        workspace_id = str(getattr(snapshot, "id", "") or "").strip()
        workspace_path = getattr(snapshot, "path", None)
        if isinstance(workspace_path, str):
            workspace_path = Path(workspace_path)
        if not workspace_id or not isinstance(workspace_path, Path):
            continue
        display_name = str(
            getattr(snapshot, "display_name", "") or workspace_id
        ).strip()
        workspaces.append(
            (workspace_id, str(canonicalize_path(workspace_path)), display_name)
        )
    workspaces.sort(key=lambda item: (item[2].lower(), item[0]))
    return workspaces


def _resource_owner_for_workspace(
    service: Any,
    workspace_root: Path,
    *,
    repo_id: Optional[str] = None,
    resource_kind: Optional[str] = None,
    resource_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    normalized_repo_id = (
        repo_id.strip() if isinstance(repo_id, str) and repo_id.strip() else None
    )
    normalized_resource_kind = (
        resource_kind.strip()
        if isinstance(resource_kind, str) and resource_kind.strip()
        else None
    )
    normalized_resource_id = (
        resource_id.strip()
        if isinstance(resource_id, str) and resource_id.strip()
        else None
    )
    if normalized_resource_kind == "repo" and normalized_resource_id:
        return "repo", normalized_resource_id, normalized_resource_id
    if normalized_resource_kind == "agent_workspace" and normalized_resource_id:
        return "agent_workspace", normalized_resource_id, None
    if normalized_repo_id:
        return "repo", normalized_repo_id, normalized_repo_id

    canonical_workspace = str(canonicalize_path(workspace_root))
    for (
        workspace_id,
        workspace_path,
        _display_name,
    ) in service._list_agent_workspaces():
        if workspace_path == canonical_workspace:
            return "agent_workspace", workspace_id, None
    for listed_repo_id, listed_path in service._list_manifest_repos():
        if str(canonicalize_path(Path(listed_path))) == canonical_workspace:
            return "repo", listed_repo_id, listed_repo_id
    return None, None, None


def _list_bind_workspace_candidates(
    service: Any,
) -> list[tuple[Optional[str], Optional[str], str]]:
    candidates: list[tuple[Optional[str], Optional[str], str]] = []
    manifest_paths: set[str] = set()

    for repo_id, path in service._list_manifest_repos():
        normalized_path = str(canonicalize_path(Path(path)))
        candidates.append(("repo", repo_id, normalized_path))
        manifest_paths.add(normalized_path)

    for (
        workspace_id,
        workspace_path,
        _display_name,
    ) in service._list_agent_workspaces():
        if workspace_path in manifest_paths:
            continue
        candidates.append(("agent_workspace", workspace_id, workspace_path))
        manifest_paths.add(workspace_path)

    seen_paths: set[str] = set(manifest_paths)
    try:
        for child in sorted(
            service._config.root.iterdir(),
            key=lambda entry: entry.name.lower(),
        ):
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            normalized_path = str(canonicalize_path(child))
            if normalized_path in seen_paths:
                continue
            seen_paths.add(normalized_path)
            candidates.append((None, None, normalized_path))
    except Exception:
        pass

    return candidates


def _bind_candidate_value(
    resource_kind: Optional[str],
    resource_id: Optional[str],
    workspace_path: str,
) -> str:
    if resource_kind == "repo" and isinstance(resource_id, str) and resource_id:
        return repo_autocomplete_value(resource_id)
    if (
        resource_kind == "agent_workspace"
        and isinstance(resource_id, str)
        and resource_id
    ):
        return agent_workspace_autocomplete_value(resource_id)
    return workspace_autocomplete_value(workspace_path)


def _bind_candidate_label(
    resource_kind: Optional[str],
    resource_id: Optional[str],
    workspace_path: str,
) -> str:
    if isinstance(resource_id, str) and resource_id:
        return resource_id
    return Path(workspace_path).name or workspace_path


def _build_bind_picker_items(
    service: Any,
    candidates: list[tuple[Optional[str], Optional[str], str]],
) -> list[tuple[str, str] | tuple[str, str, Optional[str]]]:
    items: list[tuple[str, str] | tuple[str, str, Optional[str]]] = []
    for resource_kind, resource_id, workspace_path in candidates:
        value = _bind_candidate_value(resource_kind, resource_id, workspace_path)
        label = _bind_candidate_label(resource_kind, resource_id, workspace_path)
        description = workspace_path
        if resource_kind == "agent_workspace":
            description = f"agent workspace \u00b7 {workspace_path}"
        items.append((value, label, description))
    return items


def _build_bind_search_items(
    service: Any,
    candidates: list[tuple[Optional[str], Optional[str], str]],
) -> tuple[
    list[tuple[str, str]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
]:
    search_items: list[tuple[str, str]] = []
    exact_aliases: dict[str, tuple[str, ...]] = {}
    filter_aliases: dict[str, tuple[str, ...]] = {}
    for resource_kind, resource_id, workspace_path in candidates:
        value = _bind_candidate_value(resource_kind, resource_id, workspace_path)
        label = (
            resource_id
            if isinstance(resource_id, str) and resource_id
            else workspace_path
        )
        search_items.append((value, label))
        exact_aliases[value] = (workspace_path,)
        alias_values = [workspace_path]
        if isinstance(resource_id, str) and resource_id:
            alias_values.append(resource_id)
        if isinstance(resource_kind, str) and resource_kind:
            alias_values.append(resource_kind.replace("_", " "))
        basename = Path(workspace_path).name
        if basename:
            alias_values.append(basename)
        filter_aliases[value] = tuple(alias_values)
    return search_items, exact_aliases, filter_aliases


async def _resolve_picker_query_or_prompt(
    service: Any,
    *,
    query: str,
    items: list[tuple[str, str]],
    limit: int,
    prompt_filtered_items: Callable[
        [str, list[tuple[str, str]]],
        Awaitable[None],
    ],
    exact_aliases: Optional[Mapping[str, Sequence[str]]] = None,
    aliases: Optional[Mapping[str, Sequence[str]]] = None,
) -> Optional[str]:
    normalized_query = query.strip()
    if not normalized_query:
        return None

    resolution = resolve_picker_query(
        items,
        normalized_query,
        limit=limit,
        exact_aliases=exact_aliases,
        aliases=aliases,
    )
    if resolution.selected_value is not None:
        return resolution.selected_value
    if resolution.filtered_items:
        await prompt_filtered_items(normalized_query, resolution.filtered_items)
        return None
    return normalized_query


def _build_bind_page_prompt_and_components(
    service: Any,
    candidates: list[tuple[Optional[str], Optional[str], str]],
    *,
    page: int,
) -> tuple[str, list[dict[str, Any]]]:
    page_size = DISCORD_SELECT_OPTION_MAX_OPTIONS
    total = len(candidates)
    total_pages = max(1, (total + page_size - 1) // page_size)
    bounded_page = max(0, min(page, total_pages - 1))
    start = bounded_page * page_size
    end = start + page_size
    page_candidates = candidates[start:end]

    prompt = "Select a workspace to bind:"
    if total > page_size:
        prompt = (
            "Select a workspace to bind "
            f"(page {bounded_page + 1}/{total_pages}, {total} total; "
            "recent worktrees first). Use `/car bind workspace:<repo_id>` "
            "or `/car bind workspace:<path>` for any repo not listed."
        )

    components: list[dict[str, Any]] = [
        build_bind_picker(_build_bind_picker_items(service, page_candidates))
    ]
    if total_pages > 1:
        components.append(
            build_action_row(
                [
                    build_button(
                        "Prev",
                        f"{BIND_PAGE_CUSTOM_ID_PREFIX}:{bounded_page - 1}",
                        disabled=bounded_page <= 0,
                    ),
                    build_button(
                        f"Page {bounded_page + 1}/{total_pages}",
                        f"{BIND_PAGE_CUSTOM_ID_PREFIX}:noop",
                        disabled=True,
                    ),
                    build_button(
                        "Next",
                        f"{BIND_PAGE_CUSTOM_ID_PREFIX}:{bounded_page + 1}",
                        disabled=bounded_page >= total_pages - 1,
                    ),
                ]
            )
        )

    return prompt, components


def _resolve_workspace_from_token(
    token: str,
    candidates: list[tuple[Optional[str], Optional[str], str]],
) -> Optional[tuple[Optional[str], Optional[str], str]]:
    return resolve_workspace_from_token(token, candidates)


async def handle_bind(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
    options: dict[str, Any],
) -> None:
    raw_path = options.get("workspace")
    if isinstance(raw_path, str) and raw_path.strip():
        await _bind_with_path(
            service,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            raw_path=raw_path.strip(),
        )
        return

    candidates = _list_bind_workspace_candidates(service)
    if not candidates:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "No workspaces found. Use /car bind workspace:<workspace> to bind manually.",
        )
        return

    prompt, components = _build_bind_page_prompt_and_components(
        service, candidates, page=0
    )
    await service._respond_with_components(
        interaction_id,
        interaction_token,
        prompt,
        components,
    )


async def _bind_with_path(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
    raw_path: str,
) -> None:
    token = raw_path.strip()
    candidates = _list_bind_workspace_candidates(service)
    resolved_workspace = _resolve_workspace_from_token(token, candidates)
    if resolved_workspace is None:
        search_items, exact_aliases, filter_aliases = _build_bind_search_items(
            service, candidates
        )

        async def _prompt_bind_matches(
            query_text: str,
            filtered_items: list[tuple[str, str]],
        ) -> None:
            filtered_values = {value for value, _label in filtered_items}
            filtered_candidates = [
                candidate
                for candidate in candidates
                if _bind_candidate_value(
                    candidate[0],
                    candidate[1],
                    candidate[2],
                )
                in filtered_values
            ]
            await service._respond_with_components(
                interaction_id,
                interaction_token,
                (
                    f"Matched {len(filtered_candidates)} workspaces for `{query_text}`. "
                    "Select a workspace to bind:"
                ),
                [
                    build_bind_picker(
                        _build_bind_picker_items(service, filtered_candidates)
                    )
                ],
            )

        resolved_value = await _resolve_picker_query_or_prompt(
            service,
            query=token,
            items=search_items,
            limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
            exact_aliases=exact_aliases,
            aliases=filter_aliases,
            prompt_filtered_items=_prompt_bind_matches,
        )
        if resolved_value is None:
            return
        resolved_workspace = _resolve_workspace_from_token(
            resolved_value,
            candidates,
        )

    if resolved_workspace is not None:
        await _bind_to_workspace_candidate(
            service,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            selected_resource_kind=resolved_workspace[0],
            selected_resource_id=resolved_workspace[1],
            workspace_path=resolved_workspace[2],
        )
        return

    candidate = Path(token)
    if not candidate.is_absolute():
        candidate = service._config.root / candidate
    workspace = canonicalize_path(candidate)

    if not workspace.exists() or not workspace.is_dir():
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Workspace path does not exist: {workspace}",
        )
        return

    await _bind_to_workspace_candidate(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        guild_id=guild_id,
        selected_resource_kind=None,
        selected_resource_id=None,
        workspace_path=str(workspace),
    )


async def _bind_to_workspace_candidate(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
    selected_resource_kind: Optional[str],
    selected_resource_id: Optional[str],
    workspace_path: str,
) -> None:
    workspace = canonicalize_path(Path(workspace_path))
    if not workspace.exists() or not workspace.is_dir():
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Workspace path does not exist: {workspace}",
        )
        return

    await service._store.upsert_binding(
        channel_id=channel_id,
        guild_id=guild_id,
        workspace_path=str(workspace),
        repo_id=(selected_resource_id if selected_resource_kind == "repo" else None),
        resource_kind=selected_resource_kind,
        resource_id=selected_resource_id,
    )
    await service._store.clear_pending_compact_seed(channel_id=channel_id)

    if selected_resource_kind == "agent_workspace" and selected_resource_id:
        message = (
            f"Bound this channel to agent workspace: "
            f"{selected_resource_id} ({workspace})"
        )
    elif selected_resource_id:
        message = f"Bound this channel to: {selected_resource_id} ({workspace})"
    else:
        message = f"Bound this channel to workspace: {workspace}"
    await service._respond_ephemeral(
        interaction_id,
        interaction_token,
        message,
    )


async def handle_bind_selection(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
    selected_workspace_value: str,
) -> None:
    if selected_workspace_value == "none":
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "No workspace selected.",
        )
        return

    candidates = _list_bind_workspace_candidates(service)
    resolved_workspace = _resolve_workspace_from_token(
        selected_workspace_value,
        candidates,
    )
    if resolved_workspace is None:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Workspace not found: {selected_workspace_value}",
        )
        return

    await _bind_to_workspace_candidate(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        guild_id=guild_id,
        selected_resource_kind=resolved_workspace[0],
        selected_resource_id=resolved_workspace[1],
        workspace_path=resolved_workspace[2],
    )


async def handle_bind_page_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    page_token: str,
) -> None:
    if page_token == "noop":
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Already on this page.",
        )
        return
    try:
        requested_page = int(page_token)
    except (TypeError, ValueError):
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Invalid bind page selection.",
        )
        return

    candidates = _list_bind_workspace_candidates(service)
    if not candidates:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "No workspaces available to bind.",
        )
        return

    prompt, components = _build_bind_page_prompt_and_components(
        service, candidates, page=requested_page
    )
    await service._update_component_message(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        text=prompt,
        components=components,
    )


async def handle_status(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    binding = await service._store.get_binding(channel_id=channel_id)
    command_result, plain_text_result = cast(
        tuple[Any, Any],
        evaluate_collaboration_summary(
            service,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
        ),
    )
    active_flow = None
    workspace_path = None
    if isinstance(binding, dict):
        workspace_raw = binding.get("workspace_path")
        if isinstance(workspace_raw, str) and workspace_raw.strip():
            workspace_path = workspace_raw.strip()
            active_flow = await service._get_active_flow_info(workspace_path)
    lines = build_status_text(
        binding,
        collaboration_summary_lines(
            channel_id=channel_id,
            command_result=command_result,
            plain_text_result=plain_text_result,
            binding=binding,
        ),
        active_flow,
        channel_id,
        include_flow_hint=False,
    )
    if binding is None:
        await service._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )
        return

    agent, agent_profile = service._resolve_agent_state(binding)
    rate_limits = await service._read_status_rate_limits(workspace_path, agent=agent)
    approval_mode = normalize_approval_mode(
        binding.get("approval_mode"),
        default="yolo",
        include_command_aliases=True,
    )
    if approval_mode is None:
        approval_mode = "yolo"
    approval_policy, sandbox_policy = resolve_approval_mode_policies(approval_mode)
    explicit_approval_policy = binding.get("approval_policy")
    if isinstance(explicit_approval_policy, str) and explicit_approval_policy.strip():
        approval_policy = explicit_approval_policy.strip()
    explicit_sandbox_policy = binding.get("sandbox_policy")
    if explicit_sandbox_policy is not None:
        sandbox_policy = explicit_sandbox_policy
    model_label = service._status_model_label(binding)
    effort_label = service._status_effort_label(binding, agent)
    dispatcher = getattr(service, "_dispatcher", None)
    pending_queue = 0
    if dispatcher is not None:
        pending_queue = await dispatcher.pending(
            service._dispatcher_conversation_id(
                channel_id=channel_id, guild_id=guild_id
            )
        )
    extra_lines = [f"Queued requests: {pending_queue}"]
    if pending_queue:
        extra_lines.append(
            "Queued messages include Cancel and Interrupt + Send buttons."
        )
    from ...integrations.chat.status_diagnostics import (
        StatusBlockContext,
        build_status_block_lines,
    )

    status_block = StatusBlockContext(
        agent=agent,
        resume="supported" if service._agent_supports_resume(agent) else "unsupported",
        model=model_label,
        effort=effort_label,
        approval_mode=approval_mode,
        approval_policy=approval_policy or "default",
        sandbox_policy=sandbox_policy,
        rate_limits=rate_limits,
        extra_lines=tuple(extra_lines),
    )
    lines.extend(build_status_block_lines(status_block))
    lines.append("Use /flow status for ticket flow details.")
    await service._respond_ephemeral(
        interaction_id, interaction_token, "\n".join(lines)
    )


async def _get_active_flow_info(
    service: Any, workspace_path: str
) -> Optional[ActiveFlowInfo]:
    if not workspace_path or workspace_path == "unknown":
        return None
    try:
        workspace_root = canonicalize_path(Path(workspace_path))
        if not workspace_root.exists():
            return None
        store = service._open_flow_store(workspace_root)
        try:
            runs = store.list_flow_runs(flow_type="ticket_flow")
            for record in runs:
                if record.status == FlowRunStatus.RUNNING:
                    return ActiveFlowInfo(flow_id=record.id, status="running")
                if record.status == FlowRunStatus.PAUSED:
                    return ActiveFlowInfo(flow_id=record.id, status="paused")
        finally:
            store.close()
    except Exception:
        pass
    return None


async def handle_debug(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    binding = await service._store.get_binding(channel_id=channel_id)
    command_result, plain_text_result = cast(
        tuple[Any, Any],
        evaluate_collaboration_summary(
            service,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
        ),
    )
    lines = [
        f"Channel ID: {channel_id}",
    ]
    if binding is None:
        lines.append("Binding: none (unbound)")
        lines.append("Use /car bind path:<workspace> to bind this channel.")
        lines.extend(
            collaboration_summary_lines(
                channel_id=channel_id,
                command_result=command_result,
                plain_text_result=plain_text_result,
                binding=None,
            )
        )
        await service._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )
        return

    workspace_path = binding.get("workspace_path", "unknown")
    lines.extend(
        [
            f"Guild ID: {binding.get('guild_id') or 'none'}",
            f"Workspace: {workspace_path}",
            f"Repo ID: {binding.get('repo_id') or 'none'}",
            f"PMA enabled: {binding.get('pma_enabled', False)}",
            f"PMA prev workspace: {binding.get('pma_prev_workspace_path') or 'none'}",
            f"Updated at: {binding.get('updated_at', 'unknown')}",
        ]
    )

    if workspace_path and workspace_path != "unknown":
        try:
            workspace_root = canonicalize_path(Path(workspace_path))
            lines.append(f"Canonical path: {workspace_root}")
            lines.append(f"Path exists: {workspace_root.exists()}")
            if workspace_root.exists():
                car_dir = workspace_root / ".codex-autorunner"
                lines.append(f".codex-autorunner exists: {car_dir.exists()}")
                flows_db = car_dir / "flows.db"
                lines.append(f"flows.db exists: {flows_db.exists()}")
        except Exception as exc:
            lines.append(f"Path resolution error: {exc}")

    outbox_items = await service._store.list_outbox()
    pending_outbox = [r for r in outbox_items if r.channel_id == channel_id]
    lines.append(f"Pending outbox items: {len(pending_outbox)}")
    lines.extend(
        collaboration_summary_lines(
            channel_id=channel_id,
            command_result=command_result,
            plain_text_result=plain_text_result,
            binding=binding,
        )
    )

    await service._respond_ephemeral(
        interaction_id, interaction_token, "\n".join(lines)
    )


async def handle_help(
    service: Any,
    interaction_id: str,
    interaction_token: str,
) -> None:
    lines = build_discord_help_lines()
    content = format_discord_message("\n".join(lines))
    await service._respond_ephemeral(interaction_id, interaction_token, content)


async def handle_ids(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    lines = [
        f"Channel ID: {channel_id}",
        f"Guild ID: {guild_id or 'none'}",
        f"User ID: {user_id or 'unknown'}",
        "",
        "Allowlist example:",
        f"discord_bot.allowed_channel_ids: [{channel_id}]",
    ]
    if guild_id:
        lines.append(f"discord_bot.allowed_guild_ids: [{guild_id}]")
    if user_id:
        lines.append(f"discord_bot.allowed_user_ids: [{user_id}]")
    command_result, plain_text_result = cast(
        tuple[Any, Any],
        evaluate_collaboration_summary(
            service,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
        ),
    )
    binding = await service._store.get_binding(channel_id=channel_id)
    lines.extend(
        [
            "",
            *collaboration_summary_lines(
                channel_id=channel_id,
                command_result=command_result,
                plain_text_result=plain_text_result,
                binding=binding,
            ),
            "",
            *build_collaboration_snippet_lines(
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
            ),
        ]
    )
    await service._respond_ephemeral(
        interaction_id, interaction_token, "\n".join(lines)
    )


def _status_model_label(binding: dict[str, Any]) -> str:
    model = binding.get("model_override")
    if isinstance(model, str):
        model = model.strip()
        if model:
            return model
    return "default"


def _status_effort_label(service: Any, binding: dict[str, Any], agent: str) -> str:
    if not service._agent_supports_effort(agent):
        return "n/a"
    effort = binding.get("reasoning_effort")
    if isinstance(effort, str):
        effort = effort.strip()
        if effort:
            return effort
    return "default"


async def _read_status_rate_limits(
    service: Any,
    workspace_path: Optional[str],
    *,
    agent: str,
) -> Optional[dict[str, Any]]:
    if not service._agent_supports_effort(agent):
        return None
    try:
        client = await service._client_for_workspace(workspace_path)
    except Exception:
        return None
    if client is None:
        return None
    from ...integrations.chat.status_diagnostics import extract_rate_limits

    for method in ("account/rateLimits/read", "account/read"):
        try:
            result = await client.request(method, params=None, timeout=5.0)
        except Exception:
            continue
        rate_limits = extract_rate_limits(result)
        if rate_limits:
            return rate_limits
    return None
