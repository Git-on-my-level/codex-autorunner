from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .context_capsules import (
    ContextCapsule,
    ContextCapsuleExpiry,
    ContextCapsuleRenderDecision,
    ContextCapsuleScope,
    ContextCapsuleVisibility,
    stable_json_digest,
)
from .orchestration.context_capsule_ledger import SQLiteContextCapsuleLedger
from .orchestration.sqlite import open_orchestration_sqlite
from .time_utils import now_iso

_logger = logging.getLogger(__name__)

PMA_PROMPT_STATE_FILENAME = "prompt_state.json"
PMA_PROMPT_STATE_VERSION = 1
PMA_PROMPT_STATE_MAX_SESSIONS = 200
PMA_PROMPT_DIGEST_PREVIEW = 12

PMA_PROMPT_SECTION_ORDER = (
    "prompt",
    "discoverability",
    "fastpath",
    "agents",
    "active_context",
    "context_log_tail",
    "hub_snapshot",
)
PMA_PROMPT_TURN_SECTION = "current_actionable_state"
PMA_PROMPT_SECTION_CAPSULE_IDS = {
    "prompt": "pma.base_prompt",
    "discoverability": "pma.discoverability",
    "fastpath": "pma.fastpath",
    "agents": "pma.docs.agents",
    "active_context": "pma.docs.active_context",
    "context_log_tail": "pma.docs.context_log_tail",
    "hub_snapshot": "pma.hub_snapshot",
    PMA_PROMPT_TURN_SECTION: "pma.current_actionable_state",
}
PMA_PROMPT_LEDGER_SURFACE_KIND = "pma"


def default_pma_prompt_state_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / PMA_PROMPT_STATE_FILENAME


def _digest_text(value: Any) -> str:
    raw = value if isinstance(value, str) else str(value or "")
    return stable_json_digest(raw)


def _digest_preview(digest: Any) -> str:
    if not isinstance(digest, str):
        return ""
    return digest[:PMA_PROMPT_DIGEST_PREVIEW]


def _is_digest(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(ch in "0123456789abcdef" for ch in value)


def _legacy_prompt_state_reset_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.major_version_reset")


def _reset_legacy_prompt_state_if_present(hub_root: Path) -> None:
    path = default_pma_prompt_state_path(hub_root)
    if not path.exists():
        return
    reset_path = _legacy_prompt_state_reset_path(path)
    try:
        reset_path.unlink(missing_ok=True)
        path.replace(reset_path)
    except OSError as exc:
        _logger.warning(
            "Could not reset legacy PMA prompt_state.json during capsule-ledger cutover: %s",
            exc,
        )
        return
    _logger.warning(
        "Reset legacy PMA prompt_state.json during capsule-ledger cutover; archived at %s",
        reset_path,
    )


def _pma_capsule_for_section(
    name: str,
    section: Mapping[str, Any],
) -> ContextCapsule:
    capsule_id = PMA_PROMPT_SECTION_CAPSULE_IDS[name]
    content = str(section.get("content") or "")
    source_digest = str(section.get("digest") or "") or _digest_text(content)
    is_turn = name == PMA_PROMPT_TURN_SECTION
    return ContextCapsule(
        capsule_id=capsule_id,
        version=PMA_PROMPT_STATE_VERSION,
        scope=(
            ContextCapsuleScope.TURN if is_turn else ContextCapsuleScope.BACKEND_SESSION
        ),
        visibility=ContextCapsuleVisibility.MODEL_ONLY,
        source_digest=source_digest,
        expiry=(
            ContextCapsuleExpiry.EVERY_TURN
            if is_turn
            else ContextCapsuleExpiry.WHEN_SOURCE_CHANGES
        ),
        reason="pma_prompt_section",
        payload={
            "section": name,
            "label": str(section.get("label") or name),
            "tag": str(section.get("tag") or name),
            "content": content,
        },
    )


def _section_from_observation(observation: Any) -> dict[str, Any]:
    if observation is None:
        return {}
    return {
        "digest": str(observation.source_digest or ""),
        "capsule_id": observation.key.capsule_id,
        "capsule_version": observation.key.capsule_version,
        "payload_digest": str(observation.payload_digest or ""),
        "decision": ContextCapsuleRenderDecision.SKIP_DUPLICATE.value,
    }


def clear_pma_prompt_state_sessions(
    hub_root: Path,
    *,
    keys: Sequence[str] = (),
    prefixes: Sequence[str] = (),
    exclude_prefixes: Sequence[str] = (),
) -> list[str]:
    normalized_keys = {
        str(key).strip() for key in keys if isinstance(key, str) and key.strip()
    }
    normalized_prefixes = tuple(
        str(prefix).strip()
        for prefix in prefixes
        if isinstance(prefix, str) and prefix.strip()
    )
    normalized_excludes = tuple(
        str(prefix).strip()
        for prefix in exclude_prefixes
        if isinstance(prefix, str) and prefix.strip()
    )
    if not normalized_keys and not normalized_prefixes:
        return []

    cleared_keys: list[str] = []

    def _is_excluded(session_key: str) -> bool:
        return any(
            session_key == excluded.rstrip(".") or session_key.startswith(excluded)
            for excluded in normalized_excludes
        )

    _reset_legacy_prompt_state_if_present(hub_root)
    with open_orchestration_sqlite(hub_root) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT managed_thread_id
              FROM orch_context_capsule_ledger
             WHERE surface_kind = ?
               AND capsule_id LIKE 'pma.%'
            """,
            (PMA_PROMPT_LEDGER_SURFACE_KIND,),
        ).fetchall()
        for row in rows:
            session_key = str(row["managed_thread_id"] or "")
            if not session_key:
                continue
            key_match = session_key in normalized_keys
            prefix_match = bool(normalized_prefixes) and any(
                session_key.startswith(prefix) for prefix in normalized_prefixes
            )
            if (key_match or prefix_match) and not _is_excluded(session_key):
                cleared_keys.append(session_key)
        if cleared_keys:
            conn.execute(
                """
                DELETE FROM orch_context_capsule_ledger
                 WHERE surface_kind = ?
                   AND capsule_id LIKE 'pma.%'
                   AND managed_thread_id IN ({})
                """.format(
                    ",".join("?" for _ in cleared_keys)
                ),
                (PMA_PROMPT_LEDGER_SURFACE_KIND, *cleared_keys),
            )

    return sorted(cleared_keys)


def list_pma_prompt_state_session_keys(hub_root: Path) -> list[str]:
    _reset_legacy_prompt_state_if_present(hub_root)
    with open_orchestration_sqlite(hub_root) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT managed_thread_id
              FROM orch_context_capsule_ledger
             WHERE surface_kind = ?
               AND capsule_id LIKE 'pma.%'
             ORDER BY managed_thread_id
            """,
            (PMA_PROMPT_LEDGER_SURFACE_KIND,),
        ).fetchall()
    return [str(row["managed_thread_id"]) for row in rows if row["managed_thread_id"]]


def _merge_prompt_session_state(
    hub_root: Path,
    *,
    prompt_state_key: str,
    sections: Mapping[str, Mapping[str, str]],
    force_full_context: bool,
) -> tuple[bool, str, Optional[Mapping[str, Any]], Optional[str]]:
    _reset_legacy_prompt_state_if_present(hub_root)
    use_delta = False
    delta_reason = "first_turn"
    prior_sections: dict[str, Any] = {}
    prior_updated_at: Optional[str] = None
    saw_prior = False
    saw_digest_mismatch = False
    timestamp = now_iso()

    with open_orchestration_sqlite(hub_root) as conn:
        ledger = SQLiteContextCapsuleLedger(conn)
        planned_section_order = (
            *PMA_PROMPT_SECTION_ORDER,
            *(
                (PMA_PROMPT_TURN_SECTION,)
                if PMA_PROMPT_TURN_SECTION in sections
                else ()
            ),
        )
        for name in planned_section_order:
            section = sections.get(name) or {}
            capsule = _pma_capsule_for_section(name, section)
            plan = ledger.plan_render(
                capsule,
                surface_kind=PMA_PROMPT_LEDGER_SURFACE_KIND,
                surface_key=prompt_state_key,
                managed_thread_id=prompt_state_key,
                backend_thread_id=prompt_state_key,
                scope_id=prompt_state_key,
                force_refresh=force_full_context,
            )
            prior = ledger.get_observation(plan.key)
            if prior is not None:
                saw_prior = True
                if not _is_digest(prior.payload_digest) or not _is_digest(
                    prior.source_digest
                ):
                    saw_digest_mismatch = True
                prior_sections[name] = _section_from_observation(prior)
                prior_updated_at = timestamp
            prior_sections.setdefault(name, {})["decision"] = plan.decision.value
            prior_sections[name]["capsule_id"] = capsule.capsule_id
            prior_sections[name]["capsule_version"] = capsule.capsule_version
            prior_sections[name]["payload_digest"] = plan.payload_digest
            if plan.should_render:
                ledger.record_render(plan)

    if saw_digest_mismatch:
        delta_reason = "digest_mismatch"
    elif saw_prior:
        if force_full_context:
            delta_reason = "explicit_refresh"
        else:
            use_delta = True
            delta_reason = "cached_context"

    return use_delta, delta_reason, prior_sections, prior_updated_at
