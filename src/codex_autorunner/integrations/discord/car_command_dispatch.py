from __future__ import annotations

from typing import Any, Optional


async def handle_car_command(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
    user_id: Optional[str],
    command_path: tuple[str, ...],
    options: dict[str, Any],
) -> None:
    primary = command_path[1] if len(command_path) > 1 else ""

    if command_path == ("car", "bind"):
        await service._handle_bind(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            options=options,
        )
        return
    if command_path == ("car", "status"):
        await service._handle_status(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
        )
        return
    if command_path == ("car", "new"):
        await service._handle_car_new(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )
        return
    if command_path == ("car", "newt"):
        await service._handle_car_newt(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
        )
        return
    if command_path == ("car", "debug"):
        await service._handle_debug(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
        )
        return
    if command_path == ("car", "help"):
        await service._handle_help(
            interaction_id,
            interaction_token,
        )
        return
    if command_path == ("car", "ids"):
        await service._handle_ids(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
        )
        return
    if command_path == ("car", "agent"):
        await service._handle_car_agent(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            options=options,
        )
        return
    if command_path == ("car", "model"):
        await service._handle_car_model(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            options=options,
        )
        return
    if command_path == ("car", "update"):
        await service._handle_car_update(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            options=options,
        )
        return
    if command_path == ("car", "repos"):
        await service._handle_repos(
            interaction_id,
            interaction_token,
        )
        return
    if command_path == ("car", "diff"):
        workspace_root = await service._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if workspace_root is None:
            return
        await service._handle_diff(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
        )
        return
    if command_path == ("car", "skills"):
        workspace_root = await service._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if workspace_root is None:
            return
        await service._handle_skills(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
        )
        return
    if command_path == ("car", "tickets"):
        workspace_root = await service._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if workspace_root is None:
            return
        await service._handle_tickets(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            workspace_root=workspace_root,
            options=options,
        )
        return
    if command_path == ("car", "mcp"):
        workspace_root = await service._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if workspace_root is None:
            return
        await service._handle_mcp(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
        )
        return
    if command_path == ("car", "init"):
        workspace_root = await service._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if workspace_root is None:
            return
        await service._handle_init(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
        )
        return
    if command_path == ("car", "review"):
        workspace_root = await service._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if workspace_root is None:
            return
        await service._handle_car_review(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            workspace_root=workspace_root,
            options=options,
        )
        return
    if command_path == ("car", "approvals"):
        await service._handle_car_approvals(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            options=options,
        )
        return
    if command_path == ("car", "mention"):
        workspace_root = await service._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if workspace_root is None:
            return
        await service._handle_car_mention(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
        )
        return
    if command_path == ("car", "experimental"):
        workspace_root = await service._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if workspace_root is None:
            return
        await service._handle_car_experimental(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
        )
        return
    if command_path == ("car", "rollout"):
        await service._handle_car_rollout(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )
        return
    if command_path == ("car", "feedback"):
        workspace_root = await service._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if workspace_root is None:
            return
        await service._handle_car_feedback(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
            channel_id=channel_id,
        )
        return
    if command_path == ("car", "archive"):
        await service._handle_car_archive(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )
        return

    if command_path[:2] == ("car", "session"):
        if command_path == ("car", "session", "resume"):
            await service._handle_car_resume(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                options=options,
            )
            return
        if command_path == ("car", "session", "reset"):
            await service._handle_car_reset(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
            )
            return
        if command_path == ("car", "session", "compact"):
            await service._handle_car_compact(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
            )
            return
        if command_path == ("car", "session", "interrupt"):
            await service._handle_car_interrupt(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                source="slash_command",
                source_command="car session interrupt",
                source_user_id=user_id,
            )
            return
        if command_path == ("car", "session", "logout"):
            workspace_root = await service._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await service._handle_car_logout(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
            )
            return
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Unknown car session subcommand: {primary}",
        )
        return

    if command_path[:2] == ("car", "flow"):
        if command_path in {("car", "flow", "status"), ("car", "flow", "runs")}:
            action = command_path[2]
            workspace_root = await service._resolve_workspace_for_flow_read(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                action=action,
            )
            if workspace_root is None:
                return
            if action == "status":
                await service._handle_flow_status(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                )
            else:
                await service._handle_flow_runs(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                )
            return
        workspace_root = await service._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if workspace_root is None:
            return
        if command_path == ("car", "flow", "issue"):
            await service._handle_flow_issue(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
                channel_id=channel_id,
                guild_id=guild_id,
            )
            return
        if command_path == ("car", "flow", "plan"):
            await service._handle_flow_plan(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
                channel_id=channel_id,
                guild_id=guild_id,
            )
            return
        if command_path == ("car", "flow", "start"):
            await service._handle_flow_start(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
            )
            return
        if command_path == ("car", "flow", "restart"):
            await service._handle_flow_restart(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
            )
            return
        if command_path == ("car", "flow", "resume"):
            await service._handle_flow_resume(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
                channel_id=channel_id,
                guild_id=guild_id,
            )
            return
        if command_path == ("car", "flow", "stop"):
            await service._handle_flow_stop(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
                channel_id=channel_id,
                guild_id=guild_id,
            )
            return
        if command_path == ("car", "flow", "archive"):
            await service._handle_flow_archive(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
                channel_id=channel_id,
                guild_id=guild_id,
            )
            return
        if command_path == ("car", "flow", "recover"):
            await service._handle_flow_recover(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
            )
            return
        if command_path == ("car", "flow", "reply"):
            await service._handle_flow_reply(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
            )
            return
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Unknown car flow subcommand: {primary}",
        )
        return

    if command_path[:2] == ("car", "files"):
        workspace_root = await service._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if workspace_root is None:
            return

        if command_path == ("car", "files", "inbox"):
            await service._handle_files_inbox(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
            )
            return
        if command_path == ("car", "files", "outbox"):
            await service._handle_files_outbox(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
            )
            return
        if command_path == ("car", "files", "clear"):
            await service._handle_files_clear(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
            )
            return
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Unknown car files subcommand: {primary}",
        )
        return

    await service._respond_ephemeral(
        interaction_id,
        interaction_token,
        f"Unknown car subcommand: {primary}",
    )
