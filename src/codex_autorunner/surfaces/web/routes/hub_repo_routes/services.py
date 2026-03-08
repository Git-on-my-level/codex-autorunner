from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ...app_state import HubAppContext
    from .mount_manager import HubMountManager


class HubRepoEnricher:
    def __init__(self, context: HubAppContext, mount_manager: HubMountManager) -> None:
        self._context = context
        self._mount_manager = mount_manager

    def enrich_repo(
        self,
        snapshot,
        chat_binding_counts: Optional[dict[str, int]] = None,
        chat_binding_counts_by_source: Optional[dict[str, dict[str, int]]] = None,
    ) -> dict:
        from .....core.pma_context import (
            get_latest_ticket_flow_run_state_with_record,
        )
        from .....core.ticket_flow_projection import build_canonical_state_v1
        from .....core.ticket_flow_summary import (
            build_ticket_flow_display,
            build_ticket_flow_summary,
        )

        repo_dict = snapshot.to_dict(self._context.config.root)
        repo_dict = self._mount_manager.add_mount_info(repo_dict)
        binding_count = int((chat_binding_counts or {}).get(snapshot.id, 0))
        source_counts = dict((chat_binding_counts_by_source or {}).get(snapshot.id, {}))
        pma_binding_count = int(source_counts.get("pma", 0))
        discord_binding_count = int(source_counts.get("discord", 0))
        telegram_binding_count = int(source_counts.get("telegram", 0))
        non_pma_binding_count = max(0, binding_count - pma_binding_count)
        repo_dict["chat_bound"] = binding_count > 0
        repo_dict["chat_bound_thread_count"] = binding_count
        repo_dict["pma_chat_bound_thread_count"] = pma_binding_count
        repo_dict["discord_chat_bound_thread_count"] = discord_binding_count
        repo_dict["telegram_chat_bound_thread_count"] = telegram_binding_count
        repo_dict["non_pma_chat_bound_thread_count"] = non_pma_binding_count
        repo_dict["cleanup_blocked_by_chat_binding"] = non_pma_binding_count > 0
        if snapshot.initialized and snapshot.exists_on_disk:
            ticket_flow = build_ticket_flow_summary(snapshot.path, include_failure=True)
            repo_dict["ticket_flow"] = ticket_flow
            if isinstance(ticket_flow, dict):
                repo_dict["ticket_flow_display"] = build_ticket_flow_display(
                    status=(
                        str(ticket_flow.get("status"))
                        if ticket_flow.get("status") is not None
                        else None
                    ),
                    done_count=int(ticket_flow.get("done_count") or 0),
                    total_count=int(ticket_flow.get("total_count") or 0),
                    run_id=(
                        str(ticket_flow.get("run_id"))
                        if ticket_flow.get("run_id")
                        else None
                    ),
                )
            else:
                repo_dict["ticket_flow_display"] = build_ticket_flow_display(
                    status=None,
                    done_count=0,
                    total_count=0,
                    run_id=None,
                )
            run_state, run_record = get_latest_ticket_flow_run_state_with_record(
                snapshot.path, snapshot.id
            )
            repo_dict["run_state"] = run_state
            repo_dict["canonical_state_v1"] = build_canonical_state_v1(
                repo_root=snapshot.path,
                repo_id=snapshot.id,
                run_state=repo_dict["run_state"],
                record=run_record,
                preferred_run_id=(
                    str(snapshot.last_run_id)
                    if snapshot.last_run_id is not None
                    else None
                ),
            )
        else:
            repo_dict["ticket_flow"] = None
            repo_dict["ticket_flow_display"] = None
            repo_dict["run_state"] = None
            repo_dict["canonical_state_v1"] = None
        return repo_dict
