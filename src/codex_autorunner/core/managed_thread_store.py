from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import load_hub_config
from .domain.refs import AgentRef, ScopeRef, SurfaceRef
from .freshness import resolve_stale_threshold_seconds
from .managed_thread_status import (
    ManagedThreadLifecycleTransition,
    ManagedThreadStatusReason,
    ManagedThreadStatusSnapshot,
    build_managed_thread_status_snapshot,
    normalize_managed_thread_lifecycle_status,
    transition_managed_thread_lifecycle_status,
    transition_managed_thread_status,
)
from .managed_thread_store_bootstrap import (
    PMA_THREADS_DB_FILENAME,
    ManagedThreadStoreBootstrap,
    default_managed_threads_db_path,
    managed_threads_db_lock,
    managed_threads_db_lock_path,
)
from .managed_thread_store_lifecycle import (
    ManagedThreadStoreLifecycle,
    thread_queue_lane_id,
)
from .managed_thread_store_rows import (
    ManagedThreadRecord,
    PmaExecutionRecord,
)
from .managed_thread_store_rows import (
    coerce_text as _coerce_text,
)
from .managed_thread_store_rows import (
    complete_thread_execution_queue_item as _complete_thread_execution_queue_item,
)
from .managed_thread_store_rows import (
    enrich_thread_metadata_for_workspace as _enrich_thread_metadata_for_workspace,
)
from .managed_thread_store_rows import (
    fail_thread_execution_pending_items as _fail_thread_execution_pending_items,
)
from .managed_thread_store_rows import (
    fail_thread_execution_running_items as _fail_thread_execution_running_items,
)
from .managed_thread_store_rows import (
    insert_thread_execution_queue_item as _insert_thread_execution_queue_item,
)
from .managed_thread_store_rows import (
    normalize_request_kind as _normalize_request_kind,
)
from .managed_thread_store_rows import (
    sanitize_thread_metadata as _sanitize_thread_metadata,
)
from .managed_thread_store_rows import (
    workspace_head_branch as _workspace_head_branch,
)
from .orchestration.chat_surface_emitters import emit_chat_surface_event
from .orchestration.models import (
    BackendBinding,
    normalize_resource_owner_fields,
    owner_fields_from_scope_ref,
)
from .orchestration.runtime_bindings import (
    BACKEND_BINDING_BOUND,
    RuntimeThreadBinding,
    clear_runtime_thread_binding,
    get_runtime_thread_binding,
    mark_runtime_thread_binding_state,
    normalize_backend_binding_state,
    set_runtime_thread_binding,
)
from .orchestration.thread_titles import (
    EXPLICIT_TITLE_SOURCE,
    FIRST_MESSAGE_TITLE_SOURCE,
    PROVIDER_TITLE_SOURCE,
    choose_owned_thread_title,
    is_deprioritized_thread_title,
    is_generic_thread_title,
    normalize_thread_title,
    normalize_thread_title_source,
    thread_title_source_allows_replacement,
)
from .orchestration.turn_assistant_output import TurnAssistantOutput
from .orchestration.turn_execution_contract import (
    TurnExecutionOrigin,
    TurnExecutionRecord,
    TurnExecutionRequest,
)
from .orchestration.turn_execution_storage import (
    TURN_EXECUTION_CONTRACT_VERSION,
    build_turn_execution_record_from_storage,
)
from .ports.thread_store import ThreadRecord, ThreadStatus
from .runtime_identity import (
    RUNTIME_STAGE_EFFECTIVE,
    RUNTIME_STAGE_LAUNCH,
    RUNTIME_STAGE_PROJECTED,
    RUNTIME_STAGE_REQUESTED,
    RUNTIME_STAGE_RESOLVED,
    RuntimeIdentityEnvelope,
    RuntimeIdentityStage,
)
from .text_utils import _json_dumps, _json_loads_object
from .time_utils import now_iso

_BACKEND_RUNTIME_INSTANCE_ID_KEY = "backend_runtime_instance_id"
_RUNTIME_STARTED_AT_KEY = "runtime_started_at"
_CLIENT_MANAGED_THREAD_ID_PATTERN = re.compile(
    r"^(?:pma:)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class ManagedThreadAlreadyHasRunningTurnError(RuntimeError):
    def __init__(self, managed_thread_id: str) -> None:
        super().__init__(
            f"Managed thread '{managed_thread_id}' already has a running turn"
        )
        self.managed_thread_id = managed_thread_id


class ManagedThreadNotActiveError(RuntimeError):
    def __init__(self, managed_thread_id: str, status: Optional[str]) -> None:
        detail = (
            f"Managed thread '{managed_thread_id}' is not active"
            if not status
            else f"Managed thread '{managed_thread_id}' is not active (status={status})"
        )
        super().__init__(detail)
        self.managed_thread_id = managed_thread_id
        self.status = status


def _resolve_stale_running_threshold_seconds(
    hub_root: Path, *, override: Optional[int]
) -> int:
    if isinstance(override, int) and override > 0:
        return override
    try:
        config = load_hub_config(hub_root)
        pma_cfg = getattr(config, "pma", None)
        return resolve_stale_threshold_seconds(
            getattr(pma_cfg, "freshness_stale_threshold_seconds", None)
        )
    except Exception:
        return resolve_stale_threshold_seconds(None)


def _opencode_model_payload(model: Optional[str]) -> dict[str, str]:
    if model is None or "/" not in model:
        return {}
    provider_id, model_id = (part.strip() for part in model.split("/", 1))
    if not provider_id or not model_id:
        return {}
    return {"providerID": provider_id, "modelID": model_id}


def _turn_request_for_direct_create(
    *,
    managed_turn_id: str,
    managed_thread_id: str,
    thread: dict[str, Any],
    prompt: str,
    request_kind: str,
    busy_policy: str,
    model: Optional[str],
    reasoning: Optional[str],
    client_turn_id: Optional[str],
    metadata: dict[str, Any],
) -> TurnExecutionRequest:
    agent = str(thread.get("agent_id") or thread.get("agent") or "codex")
    resolved_model = model
    if agent == "opencode" and resolved_model is None:
        resolved_model = "openai/gpt-5"
    return TurnExecutionRequest(
        request_id=managed_turn_id,
        target_id=managed_thread_id,
        target_kind="thread",
        workspace_root=_coerce_text(thread.get("workspace_root")),
        request_kind=request_kind,  # type: ignore[arg-type]
        busy_policy=busy_policy,  # type: ignore[arg-type]
        prompt_text=prompt,
        agent=agent,
        model=resolved_model,
        model_payload=(
            _opencode_model_payload(resolved_model) if agent == "opencode" else {}
        ),
        reasoning=reasoning,
        approval_policy=str(metadata.get("approval_policy") or "never"),
        approval_mode=_coerce_text(metadata.get("approval_mode")),
        sandbox_policy=metadata.get("sandbox_policy") or "dangerFullAccess",
        client_request_id=client_turn_id,
        idempotency_key=client_turn_id or managed_turn_id,
        origin=TurnExecutionOrigin(
            kind="system",
            source_id="managed-thread-store",
            metadata={"source": "direct_create_turn"},
        ),
        metadata=dict(metadata),
    )


def _thread_row_to_record(row: Any) -> dict[str, Any]:
    return ManagedThreadRecord.from_orchestration_row(row).to_dict()


def _thread_row_to_thread(row: Any) -> Any:
    return ManagedThreadRecord.from_orchestration_row(row).to_thread()


def _thread_model_to_port_record(thread: Any) -> ThreadRecord:
    payload = thread.to_dict()
    lifecycle_status = normalize_managed_thread_lifecycle_status(
        thread.lifecycle_status
    )
    return ThreadRecord(
        thread_id=thread.id,
        scope=thread.scope,
        agent=thread.agent,
        surface=thread.surface,
        backend_binding=thread.backend_binding.to_dict(),
        status=ThreadStatus(lifecycle_status.value),
        display_name=thread.display_name or "",
        last_turn_id=thread.last_execution_id,
        last_execution_id=thread.last_execution_id,
        metadata=payload,
    )


def _execution_row_to_record(row: Any) -> dict[str, Any]:
    return PmaExecutionRecord.from_orchestration_row(row).to_dict()


def _runtime_identity_from_json(payload: Any) -> RuntimeIdentityEnvelope:
    text = _coerce_text(payload)
    if text is None:
        return RuntimeIdentityEnvelope()
    return RuntimeIdentityEnvelope.from_json(text)


def _runtime_identity_with_stage(
    envelope: RuntimeIdentityEnvelope,
    stage: RuntimeIdentityStage,
) -> RuntimeIdentityEnvelope:
    stage_name = stage.stage
    return RuntimeIdentityEnvelope(
        requested=(
            stage if stage_name == RUNTIME_STAGE_REQUESTED else envelope.requested
        ),
        resolved=(stage if stage_name == RUNTIME_STAGE_RESOLVED else envelope.resolved),
        launch=stage if stage_name == RUNTIME_STAGE_LAUNCH else envelope.launch,
        effective=(
            stage if stage_name == RUNTIME_STAGE_EFFECTIVE else envelope.effective
        ),
        projected=(
            stage if stage_name == RUNTIME_STAGE_PROJECTED else envelope.projected
        ),
        metadata=envelope.metadata,
    )


def _resolved_runtime_identity(
    request: TurnExecutionRequest,
) -> RuntimeIdentityEnvelope:
    return RuntimeIdentityEnvelope(
        resolved=RuntimeIdentityStage.from_turn_execution_request(
            request.to_dict(), stage=RUNTIME_STAGE_RESOLVED
        )
    )


def _launch_runtime_stage(
    request: TurnExecutionRequest,
    *,
    backend_turn_id: Optional[str],
) -> RuntimeIdentityStage:
    provider_payload: dict[str, Any] = {}
    if request.model_payload:
        provider_payload = dict(request.model_payload)
    elif request.model is not None:
        provider_payload["model"] = request.model
    if request.reasoning is not None:
        provider_payload.setdefault("reasoning", request.reasoning)
        provider_payload.setdefault("effort", request.reasoning)
    return RuntimeIdentityStage(
        stage=RUNTIME_STAGE_LAUNCH,
        logical_agent=request.agent,
        runtime_agent=request.agent,
        canonical_model_label=request.model,
        profile=request.profile,
        reasoning=request.reasoning,
        approval_policy=request.approval_policy,
        sandbox_policy=request.sandbox_policy,
        workspace_scope={
            "target_kind": request.target_kind,
            "target_id": request.target_id,
            "workspace_root": request.workspace_root,
        },
        prompt_ref={
            "kind": "turn_execution_request",
            "request_id": request.request_id,
        },
        input_ref=(
            {"kind": "turn_execution_input_items"} if request.input_items else None
        ),
        backend_runtime_id=backend_turn_id,
        provider_payload=provider_payload or None,
        source="managed_thread_launch",
        observed_at=now_iso(),
        provenance={"request_id": request.request_id},
    )


def _effective_runtime_stage(
    request: TurnExecutionRequest,
    *,
    backend_turn_id: Optional[str],
    observed_at: Optional[str] = None,
    effective_runtime: Optional[RuntimeIdentityStage | dict[str, Any]] = None,
) -> RuntimeIdentityStage:
    if isinstance(effective_runtime, RuntimeIdentityStage):
        return effective_runtime
    if isinstance(effective_runtime, dict):
        return RuntimeIdentityStage.from_mapping(
            effective_runtime,
            stage=RUNTIME_STAGE_EFFECTIVE,
            field_name="effective_runtime",
        )
    return RuntimeIdentityStage(
        stage=RUNTIME_STAGE_EFFECTIVE,
        logical_agent=request.agent,
        runtime_agent=request.agent,
        canonical_model_label=request.model,
        profile=request.profile,
        reasoning=request.reasoning,
        approval_policy=request.approval_policy,
        sandbox_policy=request.sandbox_policy,
        backend_runtime_id=backend_turn_id,
        provider_payload=dict(request.model_payload) or None,
        source="managed_thread_execution",
        observed_at=observed_at or now_iso(),
        provenance={"request_id": request.request_id},
    )


def prepare_managed_thread_store(hub_root: Path, *, durable: bool = False) -> None:
    bootstrap = ManagedThreadStoreBootstrap(
        hub_root=hub_root,
        db_path=default_managed_threads_db_path(hub_root),
        durable=durable,
    )
    bootstrap.prepare()


class ManagedThreadStore:
    """Current PMA-backed persistence for runtime thread targets and executions.

    Orchestration services may use this as an implementation dependency during
    the migration window, but callers should not treat its row shape as the
    long-term orchestration API surface.
    """

    def __init__(
        self,
        hub_root: Path,
        *,
        durable: bool = False,
        stale_running_threshold_seconds: Optional[int] = None,
        bootstrap_on_init: bool = True,
    ) -> None:
        self._hub_root = hub_root
        self._path = default_managed_threads_db_path(hub_root)
        self._durable = durable
        self._bootstrap_on_init = bool(bootstrap_on_init)
        self._stale_running_threshold_seconds = (
            _resolve_stale_running_threshold_seconds(
                hub_root, override=stale_running_threshold_seconds
            )
        )
        self._bootstrap = ManagedThreadStoreBootstrap(
            hub_root=self._hub_root,
            db_path=self._path,
            durable=self._durable,
        )
        self._lifecycle = ManagedThreadStoreLifecycle(
            stale_running_threshold_seconds=self._stale_running_threshold_seconds,
            execution_row_to_record=_execution_row_to_record,
            transition_thread_status=self._transition_thread_status,
            transition_thread_status_in_transaction=(
                self._transition_thread_status_in_transaction
            ),
        )
        self._initialize()

    @classmethod
    def connect_readonly(
        cls,
        hub_root: Path,
        *,
        durable: bool = False,
        stale_running_threshold_seconds: Optional[int] = None,
    ) -> "ManagedThreadStore":
        return cls(
            hub_root,
            durable=durable,
            stale_running_threshold_seconds=stale_running_threshold_seconds,
            bootstrap_on_init=False,
        )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def hub_root(self) -> Path:
        return self._hub_root

    def _initialize(self) -> None:
        if not self._bootstrap_on_init:
            return
        self._bootstrap.initialize()

    @contextmanager
    def _read_conn(self) -> Iterator[Any]:
        with self._bootstrap.read_conn() as conn:
            yield conn

    @contextmanager
    def _write_conn(self) -> Iterator[Any]:
        with self._bootstrap.write_conn() as conn:
            yield conn

    def _chat_surface_rows_for_thread(
        self, managed_thread_id: str
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [
            {"surface_kind": "pma", "surface_key": managed_thread_id}
        ]
        with self._read_conn() as conn:
            binding_rows = conn.execute(
                """
                SELECT surface_kind, surface_key, binding_id
                  FROM orch_bindings
                 WHERE target_kind = 'thread'
                   AND target_id = ?
                   AND disabled_at IS NULL
                 ORDER BY updated_at DESC, created_at DESC
                """,
                (managed_thread_id,),
            ).fetchall()
        for row in binding_rows:
            surface_kind = _coerce_text(row["surface_kind"])
            surface_key = _coerce_text(row["surface_key"])
            if surface_kind is None or surface_key is None:
                continue
            rows.append(
                {
                    "surface_kind": surface_kind,
                    "surface_key": surface_key,
                    "binding_id": _coerce_text(row["binding_id"]),
                }
            )
        return rows

    def _emit_thread_event(
        self,
        managed_thread_id: str,
        *,
        idempotency_action: str,
        event_type: str,
        status: str,
        lifecycle_status: Optional[str] = "active",
        source_kind: str = "managed_thread.lifecycle",
        source_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        occurred_at: Optional[str] = None,
        include_bindings: bool = True,
    ) -> None:
        thread = self.get_thread(managed_thread_id)
        if thread is None:
            return
        surfaces = (
            self._chat_surface_rows_for_thread(managed_thread_id)
            if include_bindings
            else [{"surface_kind": "pma", "surface_key": managed_thread_id}]
        )
        for surface in surfaces:
            surface_kind = str(surface["surface_kind"])
            surface_key = str(surface["surface_key"])
            emit_chat_surface_event(
                self._hub_root,
                durable=self._durable,
                idempotency_key=(
                    f"thread:{managed_thread_id}:{surface_kind}:"
                    f"{surface_key}:{idempotency_action}"
                ),
                event_type=event_type,  # type: ignore[arg-type]
                surface_kind=surface_kind,
                surface_key=surface_key,
                managed_thread_id=managed_thread_id,
                repo_id=_coerce_text(thread.get("repo_id")),
                resource_kind=_coerce_text(thread.get("resource_kind")),
                resource_id=_coerce_text(thread.get("resource_id")),
                workspace_root=_coerce_text(thread.get("workspace_root")),
                lifecycle_status=lifecycle_status,
                status=status,
                source_kind=source_kind,
                source_id=source_id or managed_thread_id,
                payload={"thread": thread, "surface": surface, **dict(payload or {})},
                occurred_at=occurred_at,
            )

    def get_thread_runtime_binding(
        self, managed_thread_id: str
    ) -> Optional[RuntimeThreadBinding]:
        return get_runtime_thread_binding(self._hub_root, managed_thread_id)

    def mark_thread_runtime_binding_state(
        self,
        managed_thread_id: str,
        *,
        binding_state: str,
        state_reason: Optional[str] = None,
    ) -> Optional[RuntimeThreadBinding]:
        return mark_runtime_thread_binding_state(
            self._hub_root,
            managed_thread_id,
            binding_state=binding_state,
            state_reason=state_reason,
        )

    def _fetch_thread(
        self, conn: Any, managed_thread_id: str
    ) -> Optional[dict[str, Any]]:
        row = conn.execute(
            """
            SELECT *
              FROM orch_thread_targets
             WHERE thread_target_id = ?
            """,
            (managed_thread_id,),
        ).fetchone()
        if row is None:
            return None
        return _thread_row_to_record(row)

    def _transition_thread_status(
        self,
        conn: Any,
        managed_thread_id: str,
        *,
        reason: str | ManagedThreadStatusReason,
        changed_at: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        with conn:
            return self._transition_thread_status_in_transaction(
                conn,
                managed_thread_id,
                reason=reason,
                changed_at=changed_at,
                turn_id=turn_id,
            )

    def _transition_thread_status_in_transaction(
        self,
        conn: Any,
        managed_thread_id: str,
        *,
        reason: str | ManagedThreadStatusReason,
        changed_at: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        thread = self._fetch_thread(conn, managed_thread_id)
        if thread is None:
            return None
        current = ManagedThreadStatusSnapshot.from_mapping(thread)
        resolved_changed_at = changed_at or now_iso()
        snapshot = transition_managed_thread_status(
            current,
            reason=reason,
            changed_at=resolved_changed_at,
            turn_id=turn_id,
        )
        if snapshot == current:
            return thread
        conn.execute(
            """
            UPDATE orch_thread_targets
               SET runtime_status = ?,
                   status_reason = ?,
                   status_updated_at = ?,
                   status_terminal = ?,
                   status_turn_id = ?,
                   updated_at = ?
             WHERE thread_target_id = ?
            """,
            (
                snapshot.status,
                snapshot.reason_code,
                snapshot.changed_at,
                1 if snapshot.terminal else 0,
                snapshot.turn_id,
                resolved_changed_at,
                managed_thread_id,
            ),
        )
        return self._fetch_thread(conn, managed_thread_id)

    def _transition_thread_lifecycle_status_in_transaction(
        self,
        conn: Any,
        managed_thread_id: str,
        *,
        transition: ManagedThreadLifecycleTransition,
        changed_at: str,
    ) -> Optional[dict[str, Any]]:
        thread = self._fetch_thread(conn, managed_thread_id)
        if thread is None:
            return None
        next_status = transition_managed_thread_lifecycle_status(
            thread.get("lifecycle_status"),
            transition=transition,
        )
        if thread.get("lifecycle_status") == next_status.value:
            return thread
        conn.execute(
            """
            UPDATE orch_thread_targets
               SET lifecycle_status = ?,
                   updated_at = ?
             WHERE thread_target_id = ?
            """,
            (next_status.value, changed_at, managed_thread_id),
        )
        return self._fetch_thread(conn, managed_thread_id)

    def _recover_stale_running_turns(
        self,
        conn: Any,
        managed_thread_id: str,
        *,
        include_status_turn_age_recovery: bool = True,
    ) -> int:
        return self._lifecycle.recover_stale_running_turns(
            conn,
            managed_thread_id,
            include_status_turn_age_recovery=include_status_turn_age_recovery,
        )

    def create_thread(
        self,
        agent: str | AgentRef,
        workspace_root: Optional[Path] = None,
        *,
        managed_thread_id: Optional[str] = None,
        scope: Optional[ScopeRef] = None,
        surface: Optional[SurfaceRef] = None,
        backend_binding: Optional[BackendBinding] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        managed_thread_id = _coerce_text(managed_thread_id) or str(uuid.uuid4())
        if not _CLIENT_MANAGED_THREAD_ID_PATTERN.fullmatch(managed_thread_id):
            raise ValueError("managed_thread_id must be a UUID or pma:<UUID>")
        if self.get_thread(managed_thread_id) is not None:
            raise ValueError(f"Managed thread '{managed_thread_id}' already exists")
        now = now_iso()
        resolved_agent = agent.agent_id if isinstance(agent, AgentRef) else agent
        incoming_metadata = dict(metadata or {})
        if isinstance(agent, AgentRef) and agent.profile is not None:
            incoming_metadata.setdefault("agent_profile", agent.profile)
        if backend_binding is not None:
            if backend_thread_id is not None:
                raise ValueError(
                    "backend_binding and backend_thread_id cannot both be provided"
                )
            backend_thread_id = backend_binding.backend_thread_id
            if backend_binding.backend_runtime_instance_id is not None:
                incoming_metadata.setdefault(
                    _BACKEND_RUNTIME_INSTANCE_ID_KEY,
                    backend_binding.backend_runtime_instance_id,
                )
        if scope is not None:
            if (
                repo_id is not None
                or resource_kind is not None
                or resource_id is not None
            ):
                raise ValueError(
                    "scope cannot be combined with repo_id/resource_kind/resource_id"
                )
            (
                scope_repo_id,
                scope_resource_kind,
                scope_resource_id,
                scope_workspace,
            ) = owner_fields_from_scope_ref(scope)
            repo_id = scope_repo_id
            resource_kind = scope_resource_kind
            resource_id = scope_resource_id
            if scope_workspace is not None:
                if (
                    workspace_root is not None
                    and str(workspace_root) != scope_workspace
                ):
                    raise ValueError(
                        "filesystem scope path must match workspace_root when both are provided"
                    )
                workspace_root = Path(scope_workspace)
        if workspace_root is None:
            raise ValueError("workspace_root is required for managed PMA threads")
        workspace = workspace_root
        if not workspace.is_absolute():
            raise ValueError("workspace_root must be absolute")
        (
            normalized_resource_kind,
            normalized_resource_id,
            normalized_repo_id,
        ) = normalize_resource_owner_fields(
            resource_kind=resource_kind,
            resource_id=resource_id,
            repo_id=repo_id,
        )
        normalized_backend_thread_id = _coerce_text(backend_thread_id)
        metadata_payload = _enrich_thread_metadata_for_workspace(
            incoming_metadata,
            workspace_root=workspace,
        )
        backend_runtime_instance_id = _coerce_text(
            incoming_metadata.get(_BACKEND_RUNTIME_INSTANCE_ID_KEY)
        )
        normalized_scope_urn = (
            scope.to_urn()
            if scope is not None
            else ScopeRef(kind="filesystem", path=str(workspace)).to_urn()
        )
        if scope is None and normalized_resource_kind == "repo":
            normalized_scope_urn = ScopeRef(
                kind=normalized_resource_kind,
                id=normalized_resource_id,
            ).to_urn()
        normalized_surface_urn = surface.to_urn() if surface is not None else None
        backend_binding_json = _json_dumps(
            BackendBinding(
                backend_thread_id=normalized_backend_thread_id,
                backend_runtime_instance_id=backend_runtime_instance_id,
                binding_state=BACKEND_BINDING_BOUND,
            ).to_dict()
        )

        lifecycle_status = transition_managed_thread_lifecycle_status(
            None,
            transition=ManagedThreadLifecycleTransition.THREAD_CREATED,
        )
        snapshot = build_managed_thread_status_snapshot(
            reason=ManagedThreadStatusReason.THREAD_CREATED,
            changed_at=now,
        )
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_thread_targets (
                        thread_target_id,
                        agent_id,
                        backend_thread_id,
                        repo_id,
                        resource_kind,
                        resource_id,
                        workspace_root,
                        scope_urn,
                        surface_urn,
                        backend_binding_json,
                        display_name,
                        lifecycle_status,
                        runtime_status,
                        status_reason,
                        status_turn_id,
                        last_execution_id,
                        last_message_preview,
                        compact_seed,
                        metadata_json,
                        created_at,
                        updated_at,
                        status_updated_at,
                        status_terminal
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        managed_thread_id,
                        resolved_agent,
                        normalized_backend_thread_id,
                        normalized_repo_id,
                        normalized_resource_kind,
                        normalized_resource_id,
                        str(workspace),
                        normalized_scope_urn,
                        normalized_surface_urn,
                        backend_binding_json,
                        name,
                        lifecycle_status.value,
                        snapshot.status,
                        snapshot.reason_code,
                        snapshot.turn_id,
                        None,
                        None,
                        None,
                        _json_dumps(metadata_payload),
                        now,
                        now,
                        snapshot.changed_at,
                        1 if snapshot.terminal else 0,
                    ),
                )
            if normalized_backend_thread_id is not None:
                set_runtime_thread_binding(
                    self._hub_root,
                    managed_thread_id,
                    backend_thread_id=normalized_backend_thread_id,
                    backend_runtime_instance_id=backend_runtime_instance_id,
                    binding_state=BACKEND_BINDING_BOUND,
                )
            created = self._fetch_thread(conn, managed_thread_id)
        if created is None:
            raise RuntimeError("Failed to create managed PMA thread")
        self._emit_thread_event(
            managed_thread_id,
            idempotency_action="created",
            event_type="lifecycle.status_changed",
            status=str(snapshot.status),
            lifecycle_status=lifecycle_status.value,
            source_id=managed_thread_id,
            occurred_at=now,
            include_bindings=False,
        )
        return created

    def get_thread_model(self, managed_thread_id: str) -> Optional[Any]:
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_targets
                 WHERE thread_target_id = ?
                """,
                (managed_thread_id,),
            ).fetchone()
        return _thread_row_to_thread(row) if row is not None else None

    async def create(self, record: ThreadRecord) -> ThreadRecord:
        workspace_root = record.metadata.get("workspace_root")
        if record.scope.kind == "filesystem":
            workspace_root = record.scope.path
        if not isinstance(workspace_root, str):
            raise ValueError("ThreadRecord metadata requires workspace_root")
        created = self.create_thread(
            record.agent,
            Path(workspace_root),
            scope=record.scope,
            surface=record.surface,
            backend_binding=BackendBinding.from_mapping(record.backend_binding),
            name=record.display_name or None,
            metadata=record.metadata,
        )
        thread = self.get_thread_model(created["managed_thread_id"])
        if thread is None:
            raise RuntimeError("Failed to hydrate created thread")
        return _thread_model_to_port_record(thread)

    async def get(self, thread_id: str) -> Optional[ThreadRecord]:
        thread = self.get_thread_model(thread_id)
        return _thread_model_to_port_record(thread) if thread is not None else None

    async def list_by_scope(self, scope: ScopeRef) -> list[ThreadRecord]:
        if scope.kind == "repo":
            rows = self.list_threads(repo_id=scope.id)
        elif scope.kind == "worktree":
            rows = self.list_threads(resource_kind="worktree", resource_id=scope.id)
        else:
            rows = self.list_threads()
        records: list[ThreadRecord] = []
        for row in rows:
            thread = self.get_thread_model(str(row["managed_thread_id"]))
            if thread is not None and thread.scope == scope:
                records.append(_thread_model_to_port_record(thread))
        return records

    async def update_status(
        self, thread_id: str, status: ThreadStatus
    ) -> Optional[ThreadRecord]:
        if status == ThreadStatus.ARCHIVED:
            self.archive_thread(thread_id)
        elif status in {ThreadStatus.ACTIVE, ThreadStatus.IDLE, ThreadStatus.PENDING}:
            self.activate_thread(thread_id)
        else:
            raise ValueError(f"Unsupported managed-thread lifecycle status: {status}")
        if self.get_thread(thread_id) is None:
            return None
        model = self.get_thread_model(thread_id)
        return _thread_model_to_port_record(model) if model is not None else None

    async def delete(self, thread_id: str) -> bool:
        return await self.update_status(thread_id, ThreadStatus.ARCHIVED) is not None

    def get_thread(self, managed_thread_id: str) -> Optional[dict[str, Any]]:
        with self._read_conn() as conn:
            return self._fetch_thread(conn, managed_thread_id)

    def _queue_payload_for_execution(
        self, conn: Any, managed_turn_id: str
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT payload_json
              FROM orch_queue_items
             WHERE source_kind = 'thread_execution'
               AND source_key = ?
             ORDER BY rowid DESC
             LIMIT 1
            """,
            (managed_turn_id,),
        ).fetchone()
        return _json_loads_object(row["payload_json"]) if row is not None else {}

    def _queue_item_for_execution(
        self, conn: Any, managed_turn_id: str
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT *
              FROM orch_queue_items
             WHERE source_kind = 'thread_execution'
               AND source_key = ?
             ORDER BY rowid DESC
             LIMIT 1
            """,
            (managed_turn_id,),
        ).fetchone()
        return {key: row[key] for key in row.keys()} if row is not None else {}

    def _refresh_turn_execution_envelopes(
        self, conn: Any, managed_turn_id: str
    ) -> None:
        row = conn.execute(
            """
            SELECT *
              FROM orch_thread_executions
             WHERE execution_id = ?
            """,
            (managed_turn_id,),
        ).fetchone()
        if row is None:
            return
        execution = {key: row[key] for key in row.keys()}
        thread = self._fetch_thread(conn, str(execution["thread_target_id"]))
        if thread is None:
            return
        existing_request = _coerce_text(execution.get("turn_request_json"))
        if existing_request is None:
            raise RuntimeError(
                f"Managed turn '{managed_turn_id}' is missing canonical turn request"
            )
        request = TurnExecutionRequest.from_json(existing_request)
        record = build_turn_execution_record_from_storage(
            execution=execution,
            thread=thread,
            request=request,
            queue_item=self._queue_item_for_execution(conn, managed_turn_id),
        )
        runtime_identity = _runtime_identity_from_json(
            execution.get("runtime_identity_json")
        )
        if runtime_identity.resolved is None:
            runtime_identity = RuntimeIdentityEnvelope(
                requested=runtime_identity.requested,
                resolved=RuntimeIdentityStage.from_turn_execution_request(
                    request.to_dict(), stage=RUNTIME_STAGE_RESOLVED
                ),
                launch=runtime_identity.launch,
                effective=runtime_identity.effective,
                projected=runtime_identity.projected,
                metadata=runtime_identity.metadata,
            )
        conn.execute(
            """
            UPDATE orch_thread_executions
               SET turn_contract_version = ?,
                   turn_request_json = ?,
                   turn_record_json = ?,
                   runtime_identity_json = ?
             WHERE execution_id = ?
            """,
            (
                TURN_EXECUTION_CONTRACT_VERSION,
                request.to_json(),
                record.to_json(),
                runtime_identity.to_json(),
                managed_turn_id,
            ),
        )

    def get_turn_execution_request(
        self, managed_thread_id: str, managed_turn_id: str
    ) -> Optional[TurnExecutionRequest]:
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT turn_request_json
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND execution_id = ?
                """,
                (managed_thread_id, managed_turn_id),
            ).fetchone()
        if row is None:
            return None
        payload = _coerce_text(row["turn_request_json"])
        if payload is None:
            raise RuntimeError(
                f"Managed turn '{managed_turn_id}' is missing canonical turn request"
            )
        return TurnExecutionRequest.from_json(payload)

    def get_turn_execution_record(
        self, managed_thread_id: str, managed_turn_id: str
    ) -> Optional[TurnExecutionRecord]:
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT turn_record_json
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND execution_id = ?
                """,
                (managed_thread_id, managed_turn_id),
            ).fetchone()
        if row is None:
            return None
        payload = _coerce_text(row["turn_record_json"])
        if payload is None:
            raise RuntimeError(
                f"Managed turn '{managed_turn_id}' is missing canonical turn record"
            )
        return TurnExecutionRecord.from_json(payload)

    def list_threads(
        self,
        *,
        agent: Optional[str] = None,
        status: Optional[str] = None,
        normalized_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: Optional[int] = 200,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit <= 0:
            return []

        query = """
            SELECT *
              FROM orch_thread_targets
             WHERE 1 = 1
        """
        params: list[Any] = []
        if agent is not None:
            query += " AND agent_id = ?"
            params.append(agent)
        if status is not None:
            query += " AND lifecycle_status = ?"
            params.append(status)
        if normalized_status is not None:
            query += " AND runtime_status = ?"
            params.append(normalized_status)
        (
            normalized_resource_kind,
            normalized_resource_id,
            normalized_repo_id,
        ) = normalize_resource_owner_fields(
            resource_kind=resource_kind,
            resource_id=resource_id,
            repo_id=repo_id,
        )
        if normalized_resource_kind is not None:
            query += " AND resource_kind = ?"
            params.append(normalized_resource_kind)
        if normalized_resource_id is not None:
            query += " AND resource_id = ?"
            params.append(normalized_resource_id)
        if normalized_repo_id is not None and normalized_resource_kind is None:
            query += " AND repo_id = ?"
            params.append(normalized_repo_id)
        query += " ORDER BY updated_at DESC, created_at DESC, thread_target_id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        with self._read_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_thread_row_to_record(row) for row in rows]

    def count_threads_by_repo(
        self, *, agent: Optional[str] = None, status: Optional[str] = None
    ) -> dict[str, int]:
        query = """
            SELECT TRIM(repo_id) AS repo_id, COUNT(*) AS thread_count
              FROM orch_thread_targets
             WHERE repo_id IS NOT NULL
               AND TRIM(repo_id) != ''
        """
        params: list[Any] = []
        if agent is not None:
            query += " AND agent_id = ?"
            params.append(agent)
        if status is not None:
            query += " AND lifecycle_status = ?"
            params.append(status)
        query += " GROUP BY TRIM(repo_id)"

        with self._read_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            repo_id = row["repo_id"]
            if not isinstance(repo_id, str) or not repo_id:
                continue
            counts[repo_id] = int(row["thread_count"] or 0)
        return counts

    def set_thread_backend_binding(
        self,
        managed_thread_id: str,
        backend_thread_id: Optional[str],
        *,
        backend_runtime_instance_id: Optional[str] = None,
        binding_state: str = BACKEND_BINDING_BOUND,
        state_reason: Optional[str] = None,
    ) -> None:
        normalized_backend_thread_id = _coerce_text(backend_thread_id)
        normalized_binding_state = normalize_backend_binding_state(binding_state)
        normalized_state_reason = _coerce_text(state_reason)
        current_binding = get_runtime_thread_binding(self._hub_root, managed_thread_id)
        resolved_runtime_instance_id = _coerce_text(backend_runtime_instance_id)
        if (
            normalized_backend_thread_id is not None
            and resolved_runtime_instance_id is None
        ):
            resolved_runtime_instance_id = (
                current_binding.backend_runtime_instance_id
                if current_binding is not None
                else None
            )
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT backend_thread_id
                  FROM orch_thread_targets
                 WHERE thread_target_id = ?
                """,
                (managed_thread_id,),
            ).fetchone()
        current_backend_thread_id = (
            _coerce_text(row["backend_thread_id"]) if row is not None else None
        )
        binding_matches = (
            normalized_backend_thread_id is None and current_binding is None
        ) or (
            current_binding is not None
            and current_binding.backend_thread_id == normalized_backend_thread_id
            and current_binding.backend_runtime_instance_id
            == resolved_runtime_instance_id
            and current_binding.binding_state == normalized_binding_state
            and current_binding.state_reason == normalized_state_reason
        )
        if (
            row is not None
            and current_backend_thread_id == normalized_backend_thread_id
            and binding_matches
        ):
            return
        with self._write_conn() as conn:
            thread = self._fetch_thread(conn, managed_thread_id)
            metadata = _sanitize_thread_metadata(
                dict((thread or {}).get("metadata") or {})
            )
            with conn:
                conn.execute(
                    """
                    UPDATE orch_thread_targets
                       SET backend_thread_id = ?,
                           backend_binding_json = ?,
                           metadata_json = ?,
                           updated_at = ?
                     WHERE thread_target_id = ?
                    """,
                    (
                        normalized_backend_thread_id,
                        _json_dumps(
                            BackendBinding(
                                backend_thread_id=normalized_backend_thread_id,
                                backend_runtime_instance_id=resolved_runtime_instance_id,
                                binding_state=normalized_binding_state,
                                state_reason=normalized_state_reason,
                            ).to_dict()
                        ),
                        _json_dumps(metadata),
                        now_iso(),
                        managed_thread_id,
                    ),
                )
        set_runtime_thread_binding(
            self._hub_root,
            managed_thread_id,
            backend_thread_id=normalized_backend_thread_id,
            backend_runtime_instance_id=resolved_runtime_instance_id,
            binding_state=normalized_binding_state,
            state_reason=normalized_state_reason,
        )

    def update_thread_metadata(
        self,
        managed_thread_id: str,
        metadata: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        metadata_patch = _sanitize_thread_metadata(metadata)
        if not metadata_patch:
            return self.get_thread(managed_thread_id)
        changed_at = now_iso()
        with self._write_conn() as conn:
            thread = self._fetch_thread(conn, managed_thread_id)
            if thread is None:
                return None
            current_metadata = _sanitize_thread_metadata(
                dict(thread.get("metadata") or {})
            )
            updated_metadata = dict(current_metadata)
            updated_metadata.update(metadata_patch)
            if updated_metadata == current_metadata:
                return thread
            with conn:
                conn.execute(
                    """
                    UPDATE orch_thread_targets
                       SET metadata_json = ?,
                           updated_at = ?
                     WHERE thread_target_id = ?
                    """,
                    (
                        _json_dumps(updated_metadata),
                        changed_at,
                        managed_thread_id,
                    ),
                )
            return self._fetch_thread(conn, managed_thread_id)

    def update_thread_title(
        self,
        managed_thread_id: str,
        title: Optional[str],
        *,
        metadata: Optional[dict[str, Any]] = None,
        only_if_generic: bool = True,
    ) -> Optional[dict[str, Any]]:
        metadata_patch = _sanitize_thread_metadata(metadata or {})
        with self._write_conn() as conn:
            thread = self._fetch_thread(conn, managed_thread_id)
            if thread is None:
                return None
            current_title = _coerce_text(
                thread.get("display_name") or thread.get("name")
            )
            current_metadata = _sanitize_thread_metadata(
                dict(thread.get("metadata") or {})
            )
            current_title_source = _coerce_text(current_metadata.get("title_source"))
            if current_title_source is None:
                current_title_source = _coerce_text(
                    current_metadata.get("car_title_source")
                )
            incoming_title = normalize_thread_title(title)
            may_replace_preview_title = (
                current_title_source in {"message_preview", FIRST_MESSAGE_TITLE_SOURCE}
                and incoming_title is not None
                and not is_generic_thread_title(incoming_title)
            )
            may_replace_sourced_title = (
                thread_title_source_allows_replacement(current_title_source)
                and incoming_title is not None
                and not is_generic_thread_title(incoming_title)
            )
            next_title = (
                incoming_title
                if may_replace_preview_title or may_replace_sourced_title
                else choose_owned_thread_title(
                    current_title,
                    provider_title=title,
                )
            )
            should_update_title = (
                next_title is not None
                and next_title != current_title
                and (
                    not only_if_generic
                    or thread_title_source_allows_replacement(current_title_source)
                    or may_replace_preview_title
                    or may_replace_sourced_title
                    or (
                        current_title_source is None
                        and (
                            is_generic_thread_title(current_title)
                            or is_deprioritized_thread_title(current_title)
                        )
                    )
                )
            )
            if not should_update_title and (
                "car_title_source" in metadata_patch or "title_source" in metadata_patch
            ):
                metadata_patch = dict(metadata_patch)
                metadata_patch.pop("car_title_source", None)
                metadata_patch.pop("title_source", None)
            if should_update_title and "title_source" not in metadata_patch:
                source = normalize_thread_title_source(
                    metadata_patch.get("car_title_source")
                )
                if (
                    source is None
                    and metadata_patch.get("car_title_source") == "message_preview"
                ):
                    source = FIRST_MESSAGE_TITLE_SOURCE
                metadata_patch = dict(metadata_patch)
                metadata_patch["title_source"] = source or (
                    PROVIDER_TITLE_SOURCE if only_if_generic else EXPLICIT_TITLE_SOURCE
                )
                metadata_patch.pop("car_title_source", None)
            updated_metadata = dict(current_metadata)
            updated_metadata.update(metadata_patch)
            should_update_metadata = updated_metadata != current_metadata
            if not should_update_title and not should_update_metadata:
                return thread
            changed_at = now_iso()
            with conn:
                conn.execute(
                    """
                    UPDATE orch_thread_targets
                       SET display_name = CASE WHEN ? THEN ? ELSE display_name END,
                           metadata_json = ?,
                           updated_at = ?
                     WHERE thread_target_id = ?
                    """,
                    (
                        1 if should_update_title else 0,
                        next_title,
                        _json_dumps(updated_metadata),
                        changed_at,
                        managed_thread_id,
                    ),
                )
            return self._fetch_thread(conn, managed_thread_id)

    def refresh_thread_head_branch(
        self,
        managed_thread_id: str,
        *,
        workspace_root: Optional[Path] = None,
    ) -> Optional[str]:
        thread = self.get_thread(managed_thread_id)
        if thread is None:
            return None
        metadata = dict(thread.get("metadata") or {})
        fallback_branch = _coerce_text(metadata.get("head_branch"))
        resolved_workspace = workspace_root
        if resolved_workspace is None:
            workspace_text = _coerce_text(thread.get("workspace_root"))
            if workspace_text is not None:
                resolved_workspace = Path(workspace_text)
        if resolved_workspace is None:
            return fallback_branch
        head_branch = _workspace_head_branch(resolved_workspace)
        if head_branch is None:
            return fallback_branch
        self.update_thread_metadata(
            managed_thread_id,
            {"head_branch": head_branch},
        )
        return head_branch

    def update_thread_after_turn(
        self,
        managed_thread_id: str,
        *,
        last_turn_id: Optional[str],
        last_message_preview: Optional[str],
    ) -> None:
        with self._write_conn() as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE orch_thread_targets
                       SET last_execution_id = ?,
                           last_message_preview = ?,
                           updated_at = ?
                     WHERE thread_target_id = ?
                    """,
                    (
                        last_turn_id,
                        last_message_preview,
                        now_iso(),
                        managed_thread_id,
                    ),
                )

    def set_thread_compact_seed(
        self,
        managed_thread_id: str,
        compact_seed: Optional[str],
        *,
        reset_backend_id: bool = False,
    ) -> None:
        changed_at = now_iso()
        query = """
            UPDATE orch_thread_targets
               SET compact_seed = ?,
                   updated_at = ?
        """
        params: list[Any] = [compact_seed, changed_at]
        if reset_backend_id:
            query += ", backend_thread_id = NULL"
        query += " WHERE thread_target_id = ?"
        params.append(managed_thread_id)

        with self._write_conn() as conn:
            with conn:
                conn.execute(query, params)
            if _coerce_text(compact_seed) is not None:
                self._transition_thread_status(
                    conn,
                    managed_thread_id,
                    reason=ManagedThreadStatusReason.THREAD_COMPACTED,
                    changed_at=changed_at,
                )
        if reset_backend_id:
            clear_runtime_thread_binding(self._hub_root, managed_thread_id)

    def archive_thread(self, managed_thread_id: str) -> None:
        changed_at = now_iso()
        with self._write_conn() as conn:
            with conn:
                transitioned = self._transition_thread_lifecycle_status_in_transaction(
                    conn,
                    managed_thread_id,
                    transition=ManagedThreadLifecycleTransition.THREAD_ARCHIVED,
                    changed_at=changed_at,
                )
                if transitioned is None:
                    return
                self._terminalize_open_turns_for_thread(
                    conn,
                    managed_thread_id,
                    finished_at=changed_at,
                    error_text="thread_archived",
                )
            self._transition_thread_status(
                conn,
                managed_thread_id,
                reason=ManagedThreadStatusReason.THREAD_ARCHIVED,
                changed_at=changed_at,
            )
        clear_runtime_thread_binding(self._hub_root, managed_thread_id)
        self._emit_thread_event(
            managed_thread_id,
            idempotency_action="archived",
            event_type="surface.archived",
            status="archived",
            lifecycle_status="archived",
            source_id=managed_thread_id,
            occurred_at=changed_at,
        )

    def _terminalize_open_turns_for_thread(
        self,
        conn: Any,
        managed_thread_id: str,
        *,
        finished_at: str,
        error_text: str,
    ) -> None:
        rows = conn.execute(
            """
            SELECT execution_id, status
              FROM orch_thread_executions
             WHERE thread_target_id = ?
               AND status IN ('running', 'queued')
            """,
            (managed_thread_id,),
        ).fetchall()
        running_ids = [
            str(row["execution_id"])
            for row in rows
            if isinstance(row["execution_id"], str)
            and row["execution_id"]
            and row["status"] == "running"
        ]
        queued_ids = [
            str(row["execution_id"])
            for row in rows
            if isinstance(row["execution_id"], str)
            and row["execution_id"]
            and row["status"] == "queued"
        ]
        execution_ids = running_ids + queued_ids
        if execution_ids:
            placeholders = ",".join("?" for _ in execution_ids)
            conn.execute(
                f"""
                UPDATE orch_thread_executions
                   SET status = 'interrupted',
                       error_text = COALESCE(error_text, ?),
                       finished_at = COALESCE(finished_at, ?)
                 WHERE thread_target_id = ?
                   AND status IN ('running', 'queued')
                   AND execution_id IN ({placeholders})
                """,
                (error_text, finished_at, managed_thread_id, *execution_ids),
            )
        _fail_thread_execution_running_items(
            conn,
            source_keys=running_ids,
            completed_at=finished_at,
            error_text=error_text,
        )
        _fail_thread_execution_pending_items(
            conn,
            source_keys=queued_ids,
            lane_id=thread_queue_lane_id(managed_thread_id),
            completed_at=finished_at,
            error_text=error_text,
        )

    def activate_thread(self, managed_thread_id: str) -> None:
        changed_at = now_iso()
        with self._write_conn() as conn:
            with conn:
                transitioned = self._transition_thread_lifecycle_status_in_transaction(
                    conn,
                    managed_thread_id,
                    transition=ManagedThreadLifecycleTransition.THREAD_ACTIVATED,
                    changed_at=changed_at,
                )
                if transitioned is None:
                    return
            self._transition_thread_status(
                conn,
                managed_thread_id,
                reason=ManagedThreadStatusReason.THREAD_RESUMED,
                changed_at=changed_at,
            )
        self._emit_thread_event(
            managed_thread_id,
            idempotency_action=f"active:{changed_at}",
            event_type="lifecycle.status_changed",
            status="active",
            lifecycle_status="active",
            source_id=managed_thread_id,
            occurred_at=changed_at,
        )

    def create_turn(
        self,
        managed_thread_id: str,
        *,
        prompt: str,
        request_kind: str = "message",
        busy_policy: str = "reject",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        client_turn_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        queue_payload: Optional[dict[str, Any]] = None,
        turn_request: Optional[TurnExecutionRequest] = None,
        force_queue: bool = False,
    ) -> dict[str, Any]:
        managed_turn_id = (
            turn_request.request_id if turn_request is not None else str(uuid.uuid4())
        )
        started_at = now_iso()
        queue_item_id = uuid.uuid4().hex
        normalized_request_kind = _normalize_request_kind(request_kind)

        with self._write_conn() as conn:
            status_row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_targets
                 WHERE thread_target_id = ?
                """,
                (managed_thread_id,),
            ).fetchone()
            thread_status = (
                str(status_row["lifecycle_status"])
                if status_row is not None and status_row["lifecycle_status"] is not None
                else None
            )
            if status_row is None:
                raise ManagedThreadNotActiveError(managed_thread_id, thread_status)
            lifecycle_status = normalize_managed_thread_lifecycle_status(thread_status)
            if lifecycle_status.value != "active":
                raise ManagedThreadNotActiveError(managed_thread_id, thread_status)
            if turn_request is not None and turn_request.target_id != managed_thread_id:
                raise ValueError(
                    "canonical turn request target_id must match managed_thread_id"
                )
            self._recover_stale_running_turns(conn, managed_thread_id)
            existing_row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND execution_id = ?
                 LIMIT 1
                """,
                (managed_thread_id, managed_turn_id),
            ).fetchone()
            if existing_row is not None:
                existing_request_json = existing_row["turn_request_json"]
                if (
                    turn_request is not None
                    and isinstance(existing_request_json, str)
                    and existing_request_json != turn_request.to_json()
                ):
                    raise ValueError(
                        "canonical turn request conflicts with existing execution_id"
                    )
                return _execution_row_to_record(existing_row)
            with conn:
                running_exists = conn.execute(
                    """
                    SELECT 1
                      FROM orch_thread_executions
                     WHERE thread_target_id = ?
                       AND status = 'running'
                     LIMIT 1
                    """,
                    (managed_thread_id,),
                ).fetchone()
                execution_status = (
                    "queued" if force_queue or running_exists is not None else "running"
                )
                if execution_status == "queued" and busy_policy != "queue":
                    raise ManagedThreadAlreadyHasRunningTurnError(managed_thread_id)
                thread_row = {key: status_row[key] for key in status_row.keys()}
                request_metadata = dict(metadata or {})
                if isinstance(queue_payload, dict):
                    raw_request = queue_payload.get("request")
                    if isinstance(raw_request, dict):
                        raw_request_metadata = raw_request.get("metadata")
                        if isinstance(raw_request_metadata, dict):
                            request_metadata.update(raw_request_metadata)
                execution_mapping = {
                    "execution_id": managed_turn_id,
                    "thread_target_id": managed_thread_id,
                    "client_request_id": client_turn_id,
                    "request_kind": normalized_request_kind,
                    "prompt_text": prompt,
                    "status": execution_status,
                    "backend_turn_id": None,
                    "assistant_text": None,
                    "error_text": None,
                    "model_id": model,
                    "reasoning_level": reasoning,
                    "metadata_json": _json_dumps(request_metadata),
                    "transcript_mirror_id": None,
                    "started_at": started_at,
                    "finished_at": None,
                    "created_at": started_at,
                }
                if turn_request is None:
                    turn_request = _turn_request_for_direct_create(
                        managed_turn_id=managed_turn_id,
                        managed_thread_id=managed_thread_id,
                        thread=thread_row,
                        prompt=prompt,
                        request_kind=normalized_request_kind,
                        busy_policy=busy_policy,
                        model=model,
                        reasoning=reasoning,
                        client_turn_id=client_turn_id,
                        metadata=request_metadata,
                    )
                resolved_runtime_identity = _resolved_runtime_identity(turn_request)
                resolved_model = (
                    resolved_runtime_identity.resolved.canonical_model_label
                    if resolved_runtime_identity.resolved is not None
                    else turn_request.model
                )
                resolved_reasoning = (
                    resolved_runtime_identity.resolved.reasoning
                    if resolved_runtime_identity.resolved is not None
                    else turn_request.reasoning
                )
                canonical_queue_payload = {
                    "turn_request": turn_request.to_dict(),
                }
                execution_mapping["model_id"] = resolved_model
                execution_mapping["reasoning_level"] = resolved_reasoning
                turn_record = build_turn_execution_record_from_storage(
                    execution=execution_mapping,
                    thread=thread_row,
                    request=turn_request,
                    queue_item={},
                )
                conn.execute(
                    """
                    INSERT INTO orch_thread_executions (
                        execution_id,
                        thread_target_id,
                        client_request_id,
                        request_kind,
                        prompt_text,
                        status,
                        backend_turn_id,
                        assistant_text,
                        error_text,
                        model_id,
                        reasoning_level,
                        metadata_json,
                        transcript_mirror_id,
                        started_at,
                        finished_at,
                        created_at,
                        turn_contract_version,
                        turn_request_json,
                        turn_record_json,
                        runtime_identity_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        managed_turn_id,
                        managed_thread_id,
                        client_turn_id,
                        normalized_request_kind,
                        prompt,
                        execution_status,
                        None,
                        None,
                        None,
                        resolved_model,
                        resolved_reasoning,
                        _json_dumps(request_metadata),
                        None,
                        started_at,
                        None,
                        started_at,
                        TURN_EXECUTION_CONTRACT_VERSION,
                        turn_request.to_json(),
                        turn_record.to_json(),
                        resolved_runtime_identity.to_json(),
                    ),
                )
                if execution_status == "queued":
                    _insert_thread_execution_queue_item(
                        conn,
                        queue_item_id=queue_item_id,
                        lane_id=thread_queue_lane_id(managed_thread_id),
                        source_key=managed_turn_id,
                        dedupe_key=client_turn_id or managed_turn_id,
                        state="queued",
                        visible_at=started_at,
                        payload_json=_json_dumps(canonical_queue_payload),
                        created_at=started_at,
                        idempotency_key=client_turn_id or managed_turn_id,
                    )
                    self._refresh_turn_execution_envelopes(conn, managed_turn_id)
                else:
                    conn.execute(
                        """
                        UPDATE orch_thread_targets
                           SET last_execution_id = ?,
                               updated_at = ?
                         WHERE thread_target_id = ?
                        """,
                        (managed_turn_id, started_at, managed_thread_id),
                    )
                    self._transition_thread_status_in_transaction(
                        conn,
                        managed_thread_id,
                        reason=ManagedThreadStatusReason.TURN_STARTED,
                        changed_at=started_at,
                        turn_id=managed_turn_id,
                    )
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE execution_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()

        if row is None:
            raise RuntimeError("Failed to create managed PMA turn")
        record = _execution_row_to_record(row)
        self._emit_thread_event(
            managed_thread_id,
            idempotency_action=f"execution:{managed_turn_id}:{execution_status}",
            event_type=(
                "queue.state_changed"
                if execution_status == "queued"
                else "execution.progress"
            ),
            status=execution_status,
            lifecycle_status="active",
            source_kind="managed_thread.execution",
            source_id=managed_turn_id,
            payload={
                "execution": record,
                "request_kind": normalized_request_kind,
                "client_turn_id": client_turn_id,
                "busy_policy": busy_policy,
            },
            occurred_at=started_at,
        )
        return record

    def mark_turn_finished(
        self,
        managed_turn_id: str,
        *,
        status: str,
        assistant_text: Optional[str] = None,
        assistant_output: Optional[Any] = None,
        error: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
        transcript_turn_id: Optional[str] = None,
        effective_runtime: Optional[RuntimeIdentityStage | dict[str, Any]] = None,
    ) -> bool:
        finished_at = now_iso()
        if isinstance(assistant_output, dict):
            assistant_output = TurnAssistantOutput.from_mapping(assistant_output)
        turn_output_payload = (
            assistant_output.to_durable_dict() if assistant_output is not None else None
        )
        projected_assistant_text = (
            assistant_output.text if assistant_output is not None else assistant_text
        )
        reason = (
            ManagedThreadStatusReason.MANAGED_TURN_COMPLETED
            if status == "ok"
            else ManagedThreadStatusReason.MANAGED_TURN_FAILED
        )
        with self._write_conn() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE execution_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()
            if row is None:
                return False
            managed_thread_id = str(row["thread_target_id"])
            with conn:
                runtime_identity_json = None
                request_payload = _coerce_text(row["turn_request_json"])
                if request_payload is not None:
                    turn_request = TurnExecutionRequest.from_json(request_payload)
                    runtime_identity = _runtime_identity_from_json(
                        row["runtime_identity_json"]
                        if "runtime_identity_json" in row.keys()
                        else None
                    )
                    runtime_identity = _runtime_identity_with_stage(
                        runtime_identity,
                        _effective_runtime_stage(
                            turn_request,
                            backend_turn_id=backend_turn_id,
                            observed_at=finished_at,
                            effective_runtime=effective_runtime,
                        ),
                    )
                    runtime_identity_json = runtime_identity.to_json()
                cursor = conn.execute(
                    """
                    UPDATE orch_thread_executions
                       SET status = ?,
                           assistant_text = ?,
                           turn_assistant_output_json = ?,
                           error_text = ?,
                           backend_turn_id = ?,
                           transcript_mirror_id = ?,
                           finished_at = ?,
                           runtime_identity_json = COALESCE(?, runtime_identity_json)
                     WHERE execution_id = ?
                       AND status = 'running'
                    """,
                    (
                        status,
                        projected_assistant_text,
                        (
                            _json_dumps(turn_output_payload)
                            if turn_output_payload is not None
                            else None
                        ),
                        error,
                        backend_turn_id,
                        transcript_turn_id,
                        finished_at,
                        runtime_identity_json,
                        managed_turn_id,
                    ),
                )
            if cursor.rowcount == 0:
                return False
            queue_state = "completed" if status == "ok" else "failed"
            _complete_thread_execution_queue_item(
                conn,
                source_key=managed_turn_id,
                target_state=queue_state,
                completed_at=finished_at,
                error_text=error,
                result_json=_json_dumps(
                    {
                        "status": status,
                        "assistant_text": projected_assistant_text or "",
                        "turn_assistant_output": turn_output_payload or {},
                        "backend_turn_id": backend_turn_id or "",
                        "transcript_turn_id": transcript_turn_id or "",
                        "error": error or "",
                    }
                ),
            )
            self._transition_thread_status(
                conn,
                managed_thread_id,
                reason=reason,
                changed_at=finished_at,
                turn_id=managed_turn_id,
            )
            self._refresh_turn_execution_envelopes(conn, managed_turn_id)
        self._emit_thread_event(
            managed_thread_id,
            idempotency_action=f"execution:{managed_turn_id}:{status}",
            event_type="execution.progress",
            status=status,
            lifecycle_status="active",
            source_kind="managed_thread.execution",
            source_id=managed_turn_id,
            payload={
                "managed_turn_id": managed_turn_id,
                "assistant_text": projected_assistant_text or "",
                "turn_assistant_output": turn_output_payload or {},
                "error": error or "",
                "backend_turn_id": backend_turn_id or "",
                "transcript_turn_id": transcript_turn_id or "",
            },
            occurred_at=finished_at,
        )
        return True

    def set_turn_backend_turn_id(
        self,
        managed_turn_id: str,
        backend_turn_id: Optional[str],
        *,
        confirmed_start: bool = True,
    ) -> None:
        normalized_backend_turn_id = _coerce_text(backend_turn_id)
        runtime_started_at = now_iso()
        with self._write_conn() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE execution_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()
            metadata = (
                _json_loads_object(row["metadata_json"]) if row is not None else {}
            )
            existing_runtime_identity = (
                _runtime_identity_from_json(row["runtime_identity_json"])
                if row is not None and "runtime_identity_json" in row.keys()
                else RuntimeIdentityEnvelope()
            )
            request_payload = (
                _coerce_text(row["turn_request_json"])
                if row is not None and "turn_request_json" in row.keys()
                else None
            )
            turn_request = (
                TurnExecutionRequest.from_json(request_payload)
                if request_payload is not None
                else None
            )
            runtime_identity = existing_runtime_identity
            if turn_request is not None:
                launch_stage = _launch_runtime_stage(
                    turn_request,
                    backend_turn_id=normalized_backend_turn_id,
                )
                runtime_identity = _runtime_identity_with_stage(
                    runtime_identity,
                    launch_stage,
                )
            if (
                confirmed_start
                and normalized_backend_turn_id is not None
                and _coerce_text(metadata.get(_RUNTIME_STARTED_AT_KEY)) is None
            ):
                metadata[_RUNTIME_STARTED_AT_KEY] = runtime_started_at
            with conn:
                if metadata:
                    conn.execute(
                        """
                        UPDATE orch_thread_executions
                           SET backend_turn_id = ?,
                               metadata_json = ?,
                               runtime_identity_json = ?
                         WHERE execution_id = ?
                        """,
                        (
                            normalized_backend_turn_id,
                            _json_dumps(metadata),
                            runtime_identity.to_json(),
                            managed_turn_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE orch_thread_executions
                           SET backend_turn_id = ?,
                               runtime_identity_json = ?
                         WHERE execution_id = ?
                        """,
                        (
                            normalized_backend_turn_id,
                            runtime_identity.to_json(),
                            managed_turn_id,
                        ),
                    )
                self._refresh_turn_execution_envelopes(conn, managed_turn_id)

    def update_turn_metadata(
        self,
        managed_turn_id: str,
        metadata: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        metadata_patch = dict(metadata or {})
        if not metadata_patch:
            return None
        with self._write_conn() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE execution_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()
            if row is None:
                return None
            current_metadata = _json_loads_object(row["metadata_json"])
            updated_metadata = dict(current_metadata)
            updated_metadata.update(metadata_patch)
            if updated_metadata == current_metadata:
                return _execution_row_to_record(row)
            with conn:
                conn.execute(
                    """
                    UPDATE orch_thread_executions
                       SET metadata_json = ?
                     WHERE execution_id = ?
                    """,
                    (_json_dumps(updated_metadata), managed_turn_id),
                )
                self._refresh_turn_execution_envelopes(conn, managed_turn_id)
            updated = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE execution_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()
        if updated is None:
            return None
        return _execution_row_to_record(updated)

    def mark_turn_interrupted(self, managed_turn_id: str) -> bool:
        finished_at = now_iso()
        with self._write_conn() as conn:
            row = conn.execute(
                """
                SELECT thread_target_id
                  FROM orch_thread_executions
                 WHERE execution_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()
            if row is None:
                return False
            managed_thread_id = str(row["thread_target_id"])
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE orch_thread_executions
                       SET status = 'interrupted',
                           finished_at = ?
                     WHERE execution_id = ?
                       AND status = 'running'
                    """,
                    (finished_at, managed_turn_id),
                )
            if cursor.rowcount == 0:
                return False
            _fail_thread_execution_running_items(
                conn,
                source_keys=[managed_turn_id],
                completed_at=finished_at,
                error_text="interrupted",
            )
            self._transition_thread_status(
                conn,
                managed_thread_id,
                reason=ManagedThreadStatusReason.MANAGED_TURN_INTERRUPTED,
                changed_at=finished_at,
                turn_id=managed_turn_id,
            )
            self._refresh_turn_execution_envelopes(conn, managed_turn_id)
        self._emit_thread_event(
            managed_thread_id,
            idempotency_action=f"execution:{managed_turn_id}:interrupted",
            event_type="execution.progress",
            status="interrupted",
            lifecycle_status="active",
            source_kind="managed_thread.execution",
            source_id=managed_turn_id,
            payload={"managed_turn_id": managed_turn_id},
            occurred_at=finished_at,
        )
        return True

    def list_turns(
        self, managed_thread_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        with self._read_conn() as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                 ORDER BY rowid DESC
                 LIMIT ?
                """,
                (managed_thread_id, limit),
            ).fetchall()
        return [_execution_row_to_record(row) for row in rows]

    def has_running_turn(self, managed_thread_id: str) -> bool:
        return self.get_running_turn(managed_thread_id) is not None

    def get_running_turn(self, managed_thread_id: str) -> Optional[dict[str, Any]]:
        # Passive status checks must stay read-only so hub control-plane probes do
        # not contend on the PMA write lock or trigger the legacy mirror path.
        # Still filter out rows that are already logically stale because thread
        # state no longer points at them; mutation paths perform the actual
        # recovery when they acquire the write lock.
        with self._read_conn() as conn:
            stale_execution_ids = set(
                self._lifecycle.find_stale_running_turn_ids(
                    conn,
                    managed_thread_id,
                    include_status_turn_age_recovery=False,
                )
            )
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND status = 'running'
                 ORDER BY started_at DESC, execution_id DESC
                """,
                (managed_thread_id,),
            ).fetchall()
        for row in rows:
            execution_id = str(row["execution_id"] or "").strip()
            if execution_id and execution_id in stale_execution_ids:
                continue
            return _execution_row_to_record(row)
        return None

    def get_turn(
        self, managed_thread_id: str, managed_turn_id: str
    ) -> Optional[dict[str, Any]]:
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND execution_id = ?
                """,
                (managed_thread_id, managed_turn_id),
            ).fetchone()
        if row is None:
            return None
        return _execution_row_to_record(row)

    def get_turn_by_id(self, managed_turn_id: str) -> Optional[dict[str, Any]]:
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE execution_id = ?
                """,
                (managed_turn_id,),
            ).fetchone()
        if row is None:
            return None
        return _execution_row_to_record(row)

    def get_previous_completed_turn(
        self,
        managed_thread_id: str,
        *,
        exclude_turn_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        normalized_excluded = _coerce_text(exclude_turn_id)
        params: list[Any] = [managed_thread_id]
        exclusion_sql = ""
        if normalized_excluded is not None:
            exclusion_sql = "AND execution_id != ?"
            params.append(normalized_excluded)
        with self._read_conn() as conn:
            row = conn.execute(
                f"""
                SELECT *
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND status = 'ok'
                   AND COALESCE(assistant_text, '') != ''
                   {exclusion_sql}
                 ORDER BY COALESCE(finished_at, started_at, created_at) DESC,
                          rowid DESC
                 LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        if row is None:
            return None
        return _execution_row_to_record(row)

    def get_turn_by_client_turn_id(
        self, managed_thread_id: str, client_turn_id: str
    ) -> Optional[dict[str, Any]]:
        normalized_client_turn_id = _coerce_text(client_turn_id)
        if normalized_client_turn_id is None:
            return None
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE thread_target_id = ?
                   AND client_request_id = ?
                 ORDER BY
                       CASE status
                           WHEN 'running' THEN 0
                           WHEN 'queued' THEN 1
                           ELSE 2
                       END,
                       COALESCE(started_at, created_at) DESC,
                       execution_id DESC
                 LIMIT 1
                """,
                (managed_thread_id, normalized_client_turn_id),
            ).fetchone()
        if row is None:
            return None
        return _execution_row_to_record(row)

    def get_turn_by_client_turn_id_any_thread(
        self, client_turn_id: str
    ) -> Optional[dict[str, Any]]:
        """Return the best matching execution for this client id across all threads.

        Publish dedupe keys do not vary when a PR binding is repointed to a new
        managed thread; without a global lookup, a retried enqueue could miss a
        turn created on the replacement thread and enqueue a duplicate.
        """
        normalized_client_turn_id = _coerce_text(client_turn_id)
        if normalized_client_turn_id is None:
            return None
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_thread_executions
                 WHERE client_request_id = ?
                 ORDER BY
                       CASE status
                           WHEN 'running' THEN 0
                           WHEN 'queued' THEN 1
                           ELSE 2
                       END,
                       COALESCE(started_at, created_at) DESC,
                       execution_id DESC
                 LIMIT 1
                """,
                (normalized_client_turn_id,),
            ).fetchone()
        if row is None:
            return None
        return _execution_row_to_record(row)

    def list_queued_turns(
        self, managed_thread_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._read_conn() as conn:
            rows = conn.execute(
                """
                SELECT e.*
                  FROM orch_thread_executions AS e
                  JOIN orch_queue_items AS q
                    ON q.source_kind = 'thread_execution'
                   AND q.source_key = e.execution_id
                 WHERE e.thread_target_id = ?
                   AND e.status = 'queued'
                   AND q.lane_id = ?
                   AND q.state IN ('pending', 'queued', 'waiting')
                 ORDER BY COALESCE(q.visible_at, q.created_at) ASC, q.rowid ASC
                 LIMIT ?
                """,
                (
                    managed_thread_id,
                    thread_queue_lane_id(managed_thread_id),
                    limit,
                ),
            ).fetchall()
        return [_execution_row_to_record(row) for row in rows]

    def list_pending_turn_queue_items(
        self, managed_thread_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self._read_conn() as conn:
            return self._lifecycle.list_pending_turn_queue_items(
                conn,
                managed_thread_id,
                limit=limit,
            )

    def get_queued_turn_queue_payload(
        self, managed_thread_id: str, managed_turn_id: str
    ) -> Optional[dict[str, Any]]:
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT q.payload_json
                  FROM orch_queue_items AS q
                  JOIN orch_thread_executions AS e
                    ON e.execution_id = q.source_key
                 WHERE q.source_kind = 'thread_execution'
                   AND e.thread_target_id = ?
                   AND e.execution_id = ?
                   AND e.status = 'queued'
                   AND q.lane_id = ?
                   AND q.state IN ('pending', 'queued', 'waiting')
                 ORDER BY COALESCE(q.visible_at, q.created_at) ASC, q.rowid ASC
                 LIMIT 1
                """,
                (
                    managed_thread_id,
                    managed_turn_id,
                    thread_queue_lane_id(managed_thread_id),
                ),
            ).fetchone()
        if row is None:
            return None
        return _json_loads_object(row["payload_json"])

    def update_queued_turn_request(
        self,
        managed_thread_id: str,
        managed_turn_id: str,
        *,
        prompt: str,
        queue_payload: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        updated_at = now_iso()
        with self._write_conn() as conn:
            with conn:
                execution_cursor = conn.execute(
                    """
                    UPDATE orch_thread_executions
                       SET prompt_text = ?
                     WHERE execution_id = ?
                       AND thread_target_id = ?
                       AND status = 'queued'
                    """,
                    (
                        prompt,
                        managed_turn_id,
                        managed_thread_id,
                    ),
                )
                if execution_cursor.rowcount == 0:
                    return None
                row = conn.execute(
                    """
                    SELECT *
                      FROM orch_thread_executions
                     WHERE execution_id = ?
                    """,
                    (managed_turn_id,),
                ).fetchone()
                if row is None:
                    return None
                execution = {key: row[key] for key in row.keys()}
                thread = self._fetch_thread(conn, managed_thread_id)
                if thread is None:
                    return None
                requested_turn = queue_payload.get("turn_request")
                if not isinstance(requested_turn, dict):
                    raise ValueError(
                        "Queued turn update requires canonical turn_request"
                    )
                turn_request = TurnExecutionRequest.from_mapping(requested_turn)
                if turn_request.target_id != managed_thread_id:
                    raise ValueError(
                        "canonical turn request target_id must match managed_thread_id"
                    )
                turn_request = replace(turn_request, prompt_text=prompt)
                canonical_queue_payload = {
                    "turn_request": turn_request.to_dict(),
                }
                queue_cursor = conn.execute(
                    """
                    UPDATE orch_queue_items
                       SET payload_json = ?,
                           updated_at = ?
                     WHERE source_kind = 'thread_execution'
                       AND source_key = ?
                       AND lane_id = ?
                       AND state IN ('pending', 'queued', 'waiting')
                    """,
                    (
                        _json_dumps(canonical_queue_payload),
                        updated_at,
                        managed_turn_id,
                        thread_queue_lane_id(managed_thread_id),
                    ),
                )
                if queue_cursor.rowcount == 0:
                    raise RuntimeError(
                        "Queued turn execution was updated but no matching queue item "
                        "was found; refusing partial commit"
                    )
                turn_record = build_turn_execution_record_from_storage(
                    execution=execution,
                    thread=thread,
                    request=turn_request,
                    queue_item=self._queue_item_for_execution(conn, managed_turn_id),
                )
                conn.execute(
                    """
                    UPDATE orch_thread_executions
                       SET turn_contract_version = ?,
                           turn_request_json = ?,
                           turn_record_json = ?
                     WHERE execution_id = ?
                    """,
                    (
                        TURN_EXECUTION_CONTRACT_VERSION,
                        turn_request.to_json(),
                        turn_record.to_json(),
                        managed_turn_id,
                    ),
                )
        record = _execution_row_to_record(row) if row is not None else None
        if record is not None:
            self._emit_thread_event(
                managed_thread_id,
                idempotency_action=f"queue:{managed_turn_id}:updated:{updated_at}",
                event_type="queue.state_changed",
                status="updated",
                source_kind="managed_thread.queue",
                source_id=managed_turn_id,
                payload={"execution": record, "queue_payload": queue_payload},
                occurred_at=updated_at,
            )
        return record

    def get_queue_depth(self, managed_thread_id: str) -> int:
        with self._read_conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS queue_depth
                  FROM orch_queue_items
                 WHERE source_kind = 'thread_execution'
                   AND lane_id = ?
                   AND state IN ('pending', 'queued', 'waiting')
                """,
                (thread_queue_lane_id(managed_thread_id),),
            ).fetchone()
        return int((row["queue_depth"] if row is not None else 0) or 0)

    def list_thread_ids_with_running_executions(
        self, *, limit: Optional[int] = 200
    ) -> list[str]:
        if limit is not None and limit <= 0:
            return []
        limit_clause = ""
        params: list[Any] = []
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(limit)
        with self._read_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT thread_target_id, MIN(started_at) AS earliest_started_at
                  FROM orch_thread_executions
                 WHERE status = 'running'
                 GROUP BY thread_target_id
                 ORDER BY earliest_started_at ASC, thread_target_id ASC
                {limit_clause}
                """,
                params,
            ).fetchall()
        return [
            str(row["thread_target_id"])
            for row in rows
            if isinstance(row["thread_target_id"], str) and row["thread_target_id"]
        ]

    def list_thread_ids_with_pending_queue(
        self, *, limit: Optional[int] = 200
    ) -> list[str]:
        if limit is not None and limit <= 0:
            return []
        limit_clause = ""
        params: list[Any] = []
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(limit)
        with self._read_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    e.thread_target_id,
                    MIN(COALESCE(q.visible_at, q.created_at)) AS first_visible_at
                  FROM orch_queue_items AS q
                  JOIN orch_thread_executions AS e
                    ON e.execution_id = q.source_key
                 WHERE q.source_kind = 'thread_execution'
                   AND q.state IN ('pending', 'queued', 'waiting')
                 GROUP BY e.thread_target_id
                 ORDER BY first_visible_at ASC, e.thread_target_id ASC
                {limit_clause}
                """,
                params,
            ).fetchall()
        return [
            str(row["thread_target_id"])
            for row in rows
            if isinstance(row["thread_target_id"], str) and row["thread_target_id"]
        ]

    def cancel_queued_turns(self, managed_thread_id: str) -> list[str]:
        with self._write_conn() as conn:
            cancelled = self._lifecycle.cancel_queued_turns(conn, managed_thread_id)
            for execution_id in cancelled:
                self._refresh_turn_execution_envelopes(conn, execution_id)
        for execution_id in cancelled:
            self._emit_thread_event(
                managed_thread_id,
                idempotency_action=f"queue:{execution_id}:cancelled",
                event_type="queue.state_changed",
                status="cancelled",
                source_kind="managed_thread.queue",
                source_id=execution_id,
                payload={"managed_turn_id": execution_id},
            )
        return cancelled

    def cancel_queued_turn(self, managed_thread_id: str, execution_id: str) -> bool:
        with self._write_conn() as conn:
            cancelled = self._lifecycle.cancel_queued_turn(
                conn,
                managed_thread_id,
                execution_id,
            )
            if cancelled:
                self._refresh_turn_execution_envelopes(conn, execution_id)
        if cancelled:
            self._emit_thread_event(
                managed_thread_id,
                idempotency_action=f"queue:{execution_id}:cancelled",
                event_type="queue.state_changed",
                status="cancelled",
                source_kind="managed_thread.queue",
                source_id=execution_id,
                payload={"managed_turn_id": execution_id},
            )
        return cancelled

    def promote_queued_turn(self, managed_thread_id: str, execution_id: str) -> bool:
        with self._write_conn() as conn:
            promoted = self._lifecycle.promote_queued_turn(
                conn,
                managed_thread_id,
                execution_id,
            )
        if promoted:
            self._emit_thread_event(
                managed_thread_id,
                idempotency_action=f"queue:{execution_id}:promoted",
                event_type="queue.state_changed",
                status="promoted",
                source_kind="managed_thread.queue",
                source_id=execution_id,
                payload={"managed_turn_id": execution_id},
            )
        return promoted

    def claim_next_queued_turn(
        self, managed_thread_id: str
    ) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
        with self._write_conn() as conn:
            claimed = self._lifecycle.claim_next_queued_turn(conn, managed_thread_id)
            if claimed is not None:
                execution, _payload = claimed
                execution_id = str(execution.get("managed_turn_id") or "")
                if execution_id:
                    self._refresh_turn_execution_envelopes(conn, execution_id)
                    refreshed_row = conn.execute(
                        """
                        SELECT *
                          FROM orch_thread_executions
                         WHERE thread_target_id = ?
                           AND execution_id = ?
                        """,
                        (managed_thread_id, execution_id),
                    ).fetchone()
                    if refreshed_row is not None:
                        claimed = (
                            _execution_row_to_record(refreshed_row),
                            self._queue_payload_for_execution(conn, execution_id),
                        )
        if claimed is not None:
            execution, payload = claimed
            execution_id = str(execution.get("managed_turn_id") or "")
            if execution_id:
                self._emit_thread_event(
                    managed_thread_id,
                    idempotency_action=f"queue:{execution_id}:claimed",
                    event_type="queue.state_changed",
                    status="claimed",
                    source_kind="managed_thread.queue",
                    source_id=execution_id,
                    payload={"execution": execution, "queue_payload": payload},
                )
        return claimed

    def append_action(
        self,
        action_type: str,
        *,
        managed_thread_id: Optional[str] = None,
        payload_json: Optional[str] = None,
    ) -> int:
        with self._write_conn() as conn:
            row = conn.execute("""
                SELECT MAX(CAST(action_id AS INTEGER)) AS max_action_id
                  FROM orch_thread_actions
                 WHERE action_id GLOB '[0-9]*'
                """).fetchone()
            next_id = int(row["max_action_id"] or 0) + 1
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_thread_actions (
                        action_id,
                        thread_target_id,
                        execution_id,
                        action_type,
                        payload_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(next_id),
                        managed_thread_id,
                        None,
                        action_type,
                        payload_json or "{}",
                        now_iso(),
                    ),
                )
        return next_id

    def list_thread_actions(
        self,
        managed_thread_id: str,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        bounded_limit = min(max(int(limit or 500), 1), 1000)
        with self._read_conn() as conn:
            rows = conn.execute(
                """
                SELECT action_id,
                       thread_target_id,
                       execution_id,
                       action_type,
                       payload_json,
                       created_at
                  FROM orch_thread_actions
                 WHERE thread_target_id = ?
                 ORDER BY CAST(action_id AS INTEGER) ASC,
                          action_id ASC
                 LIMIT ?
                """,
                (managed_thread_id, bounded_limit),
            ).fetchall()
        return [dict(row) for row in rows]


__all__ = [
    "PMA_THREADS_DB_FILENAME",
    "ManagedThreadAlreadyHasRunningTurnError",
    "ManagedThreadNotActiveError",
    "ManagedThreadStore",
    "default_managed_threads_db_path",
    "managed_threads_db_lock",
    "managed_threads_db_lock_path",
]
