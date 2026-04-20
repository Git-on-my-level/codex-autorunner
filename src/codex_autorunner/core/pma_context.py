from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, cast

from .config_contract import ConfigError
from .hub import HubSupervisor
from .pma_active_context import (
    PMA_ACTIVE_CONTEXT_MAX_LINES,
    get_active_context_auto_prune_meta,
    maybe_auto_prune_active_context,
)
from .pma_automation_snapshot import snapshot_pma_automation as _snapshot_pma_automation
from .pma_context_shared import (
    PMA_CONTEXT_LOG_TAIL_LINES,
    PMA_DOCS_MAX_CHARS,
    PMA_MAX_AUTOMATION_ITEMS,
    PMA_MAX_LIFECYCLE_EVENTS,
    PMA_MAX_MESSAGES,
    PMA_MAX_PMA_FILES,
    PMA_MAX_PMA_THREADS,
    PMA_MAX_REPOS,
    PMA_MAX_TEMPLATE_FIELD_CHARS,
    PMA_MAX_TEMPLATE_REPOS,
    PMA_MAX_TEXT,
    _tail_lines,
    _trim_extra,
    _truncate,
)
from .pma_file_inbox import enrich_pma_file_inbox_entry
from .pma_prompt_builder import (
    PMA_FASTPATH,
    PMA_PROMPT_SECTION_META,
    PmaPromptRenderLimits,
    _build_prompt_sections,
    _render_pma_actionable_state,
    render_pma_discoverability_preamble,
    render_pma_prompt,
)
from .pma_snapshot_builder import (
    _build_snapshot_freshness_summary,
    _build_templates_snapshot,
    _resolve_pma_freshness_threshold_seconds,
    _snapshot_pma_files,
    build_hub_snapshot_payload,
)
from .pma_thread_snapshot import snapshot_pma_threads as _snapshot_pma_threads
from .pma_workspace_docs import load_pma_prompt, load_pma_workspace_docs
from .state import now_iso

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PmaPromptVariants:
    """Prompt variants for new-session bootstrap versus same-session continuation."""

    new_session_prompt: str
    existing_session_prompt: str


def format_pma_discoverability_preamble(
    *,
    hub_root: Optional[Path] = None,
    pma_docs: Optional[Mapping[str, Any]] = None,
) -> str:
    resolved_docs = pma_docs
    if resolved_docs is None and hub_root is not None:
        try:
            resolved_docs = load_pma_workspace_docs(hub_root)
        except (OSError, ValueError, TypeError, RuntimeError, ConfigError) as exc:
            _logger.warning("Could not load PMA workspace docs: %s", exc)
    return render_pma_discoverability_preamble(resolved_docs)


def format_pma_prompt(
    base_prompt: str,
    snapshot: dict[str, Any],
    message: str,
    hub_root: Optional[Path] = None,
    *,
    prompt_state_key: Optional[str] = None,
    force_full_context: bool = False,
    force_full_base_prompt: bool = False,
) -> str:
    limits = PmaPromptRenderLimits.from_snapshot(snapshot)
    snapshot_text = _render_hub_snapshot(
        snapshot,
        max_repos=limits.max_repos,
        max_messages=limits.max_messages,
        max_text_chars=limits.max_text_chars,
    )
    actionable_state_text = _render_pma_actionable_state(
        snapshot,
        render_hub_snapshot=_render_hub_snapshot,
        limits=limits,
    )
    discoverability_text = format_pma_discoverability_preamble(hub_root=None)
    pma_docs: Optional[Mapping[str, Any]] = None
    if hub_root is not None:
        try:
            pma_docs = load_pma_workspace_docs(hub_root)
        except (OSError, ValueError, TypeError, RuntimeError, ConfigError) as exc:
            _logger.warning("Could not load PMA workspace docs: %s", exc)

    sections = _build_prompt_sections(
        base_prompt=base_prompt,
        discoverability_text=discoverability_text,
        pma_docs=pma_docs,
        snapshot_text=snapshot_text,
    )
    prompt_sections = cast(Mapping[str, Mapping[str, str]], sections)
    use_delta = False
    delta_reason = "state_key_missing"
    prior_sections: Optional[Mapping[str, Any]] = None
    prior_updated_at: Optional[str] = None

    if hub_root is not None and prompt_state_key:
        (
            use_delta,
            delta_reason,
            prior_sections,
            prior_updated_at,
        ) = _merge_prompt_session_state(
            hub_root,
            prompt_state_key=prompt_state_key,
            sections=prompt_sections,
            force_full_context=force_full_context,
        )

    return render_pma_prompt(
        base_prompt=base_prompt,
        message=message,
        snapshot_text=snapshot_text,
        actionable_state_text=actionable_state_text,
        discoverability_text=discoverability_text,
        pma_docs=pma_docs,
        sections=prompt_sections,
        prompt_state_key=prompt_state_key,
        use_delta=use_delta,
        delta_reason=delta_reason,
        prior_sections=prior_sections,
        prior_updated_at=prior_updated_at,
        force_full_base_prompt=force_full_base_prompt,
    )


def format_pma_prompt_variants(
    base_prompt: str,
    snapshot: dict[str, Any],
    message: str,
    hub_root: Optional[Path] = None,
    *,
    prompt_state_key: Optional[str] = None,
    force_full_context: bool = False,
) -> PmaPromptVariants:
    """Build prompt variants for fresh-session bootstrap and existing sessions.

    The first render intentionally primes prompt-state tracking. A second render
    with the same state key then produces the compact existing-session variant,
    which is safe to use when the backend session already has the full PMA
    bootstrap in context.
    """

    new_session_prompt = format_pma_prompt(
        base_prompt,
        snapshot,
        message,
        hub_root=hub_root,
        prompt_state_key=prompt_state_key,
        force_full_context=force_full_context,
    )
    existing_session_prompt = new_session_prompt
    if hub_root is not None and prompt_state_key and not force_full_context:
        existing_session_prompt = format_pma_prompt(
            base_prompt,
            snapshot,
            message,
            hub_root=hub_root,
            prompt_state_key=prompt_state_key,
        )
    return PmaPromptVariants(
        new_session_prompt=new_session_prompt,
        existing_session_prompt=existing_session_prompt,
    )


def build_hub_unavailable_snapshot(
    *,
    detail: str,
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "generated_at": generated_at or now_iso(),
        "availability": {
            "status": "hub_unavailable",
            "detail": detail,
            "note": (
                "Shared PMA state could not be loaded from the hub control plane. "
                "Do not infer hub-root queue, thread, inbox, or automation state "
                "from local files."
            ),
        },
    }


async def build_hub_snapshot(
    supervisor: Optional[HubSupervisor],
    hub_root: Optional[Path] = None,
) -> dict[str, Any]:
    return await build_hub_snapshot_payload(
        supervisor,
        hub_root=hub_root,
        gather_inbox=_gather_inbox,
    )


from .pma_action_queue import build_pma_action_queue  # noqa: E402
from .pma_inbox import _gather_inbox  # noqa: E402
from .pma_prompt_state import (  # noqa: E402, F401
    _merge_prompt_session_state,
    clear_pma_prompt_state_sessions,
    default_pma_prompt_state_path,
    list_pma_prompt_state_session_keys,
)
from .pma_rendering import _render_hub_snapshot  # noqa: E402
from .pma_ticket_flow_state import (  # noqa: E402, F401
    TicketFlowRunState,
    TicketFlowWorkerCrash,
    _dispatch_is_actionable,
    _latest_dispatch,
    _latest_reply_history_seq,
    _resolve_paused_dispatch_state,
    _ticket_flow_inbox_item_type_and_next_action,
    build_ticket_flow_run_state,
    get_latest_ticket_flow_run_state_with_record,
)

__all__ = [
    "PMA_ACTIVE_CONTEXT_MAX_LINES",
    "PMA_CONTEXT_LOG_TAIL_LINES",
    "PMA_DOCS_MAX_CHARS",
    "PMA_FASTPATH",
    "PMA_MAX_AUTOMATION_ITEMS",
    "PMA_MAX_LIFECYCLE_EVENTS",
    "PMA_MAX_MESSAGES",
    "PMA_MAX_PMA_FILES",
    "PMA_MAX_PMA_THREADS",
    "PMA_MAX_REPOS",
    "PMA_MAX_TEMPLATE_FIELD_CHARS",
    "PMA_MAX_TEMPLATE_REPOS",
    "PmaPromptVariants",
    "PMA_MAX_TEXT",
    "PMA_PROMPT_SECTION_META",
    "TicketFlowRunState",
    "TicketFlowWorkerCrash",
    "_build_snapshot_freshness_summary",
    "_build_templates_snapshot",
    "_dispatch_is_actionable",
    "_gather_inbox",
    "_latest_dispatch",
    "_latest_reply_history_seq",
    "_merge_prompt_session_state",
    "_render_hub_snapshot",
    "_resolve_paused_dispatch_state",
    "_resolve_pma_freshness_threshold_seconds",
    "_snapshot_pma_automation",
    "_snapshot_pma_files",
    "_snapshot_pma_threads",
    "_tail_lines",
    "_ticket_flow_inbox_item_type_and_next_action",
    "_trim_extra",
    "_truncate",
    "build_hub_unavailable_snapshot",
    "build_hub_snapshot",
    "build_hub_snapshot_payload",
    "build_pma_action_queue",
    "build_ticket_flow_run_state",
    "clear_pma_prompt_state_sessions",
    "default_pma_prompt_state_path",
    "enrich_pma_file_inbox_entry",
    "format_pma_discoverability_preamble",
    "format_pma_prompt",
    "format_pma_prompt_variants",
    "get_active_context_auto_prune_meta",
    "get_latest_ticket_flow_run_state_with_record",
    "list_pma_prompt_state_session_keys",
    "load_pma_prompt",
    "load_pma_workspace_docs",
    "maybe_auto_prune_active_context",
]
