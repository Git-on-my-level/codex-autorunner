from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote, unquote

from ..chat.agents import normalize_chat_agent, normalize_hermes_profile
from ..chat.approval_modes import (
    APPROVAL_MODE_VALUES,
)
from ..chat.approval_modes import (
    normalize_approval_mode as normalize_chat_approval_mode,
)

STATE_VERSION = 5
TOPIC_ROOT = "root"
APPROVAL_MODE_YOLO = "yolo"
APPROVAL_MODE_SAFE = "safe"
APPROVAL_MODES = set(APPROVAL_MODE_VALUES)


def normalize_approval_mode(
    mode: Optional[str], *, default: str = APPROVAL_MODE_YOLO
) -> str:
    normalized = normalize_chat_approval_mode(mode, default=default)
    if isinstance(normalized, str) and normalized in APPROVAL_MODES:
        return normalized
    return default


def normalize_agent(value: Optional[str], *, context: Any = None) -> Optional[str]:
    return normalize_chat_agent(value, context=context)


def _encode_scope(scope: str) -> str:
    return quote(scope, safe="")


def _decode_scope(scope: str) -> str:
    return unquote(scope)


def topic_key(
    chat_id: int, thread_id: Optional[int], *, scope: Optional[str] = None
) -> str:
    if not isinstance(chat_id, int):
        raise TypeError("chat_id must be int")
    suffix = str(thread_id) if thread_id is not None else TOPIC_ROOT
    base_key = f"{chat_id}:{suffix}"
    if not isinstance(scope, str):
        return base_key
    scope = scope.strip()
    if not scope:
        return base_key
    return f"{base_key}:{_encode_scope(scope)}"


def parse_topic_key(key: str) -> tuple[int, Optional[int], Optional[str]]:
    parts = key.split(":", 2)
    if len(parts) < 2:
        raise ValueError("invalid topic key")
    chat_raw, thread_raw = parts[0], parts[1]
    scope_raw = parts[2] if len(parts) == 3 else None
    if not chat_raw or not thread_raw:
        raise ValueError("invalid topic key")
    try:
        chat_id = int(chat_raw)
    except ValueError as exc:
        raise ValueError("invalid chat id in topic key") from exc
    if thread_raw == TOPIC_ROOT:
        thread_id = None
    else:
        try:
            thread_id = int(thread_raw)
        except ValueError as exc:
            raise ValueError("invalid thread id in topic key") from exc
    scope = None
    if isinstance(scope_raw, str) and scope_raw:
        scope = _decode_scope(scope_raw)
    return chat_id, thread_id, scope


def _base_topic_key(raw_key: str) -> Optional[str]:
    try:
        chat_id, thread_id, _scope = parse_topic_key(raw_key)
    except ValueError:
        return None
    return topic_key(chat_id, thread_id)


@dataclass
class ThreadSummary:
    user_preview: Optional[str] = None
    assistant_preview: Optional[str] = None
    last_used_at: Optional[str] = None
    workspace_path: Optional[str] = None
    rollout_path: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["ThreadSummary"]:
        if not isinstance(payload, dict):
            return None
        user_preview = payload.get("user_preview") or payload.get("userPreview")
        assistant_preview = payload.get("assistant_preview") or payload.get(
            "assistantPreview"
        )
        last_used_at = payload.get("last_used_at") or payload.get("lastUsedAt")
        workspace_path = payload.get("workspace_path") or payload.get("workspacePath")
        rollout_path = (
            payload.get("rollout_path")
            or payload.get("rolloutPath")
            or payload.get("path")
        )
        if not isinstance(user_preview, str):
            user_preview = None
        if not isinstance(assistant_preview, str):
            assistant_preview = None
        if not isinstance(last_used_at, str):
            last_used_at = None
        if not isinstance(workspace_path, str):
            workspace_path = None
        if not isinstance(rollout_path, str):
            rollout_path = None
        return cls(
            user_preview=user_preview,
            assistant_preview=assistant_preview,
            last_used_at=last_used_at,
            workspace_path=workspace_path,
            rollout_path=rollout_path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_preview": self.user_preview,
            "assistant_preview": self.assistant_preview,
            "last_used_at": self.last_used_at,
            "workspace_path": self.workspace_path,
            "rollout_path": self.rollout_path,
        }


@dataclass
class TelegramTopicRecord:
    repo_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    workspace_path: Optional[str] = None
    workspace_id: Optional[str] = None
    pma_enabled: bool = False
    pma_prev_repo_id: Optional[str] = None
    pma_prev_resource_kind: Optional[str] = None
    pma_prev_resource_id: Optional[str] = None
    pma_prev_workspace_path: Optional[str] = None
    pma_prev_workspace_id: Optional[str] = None
    pma_prev_active_thread_id: Optional[str] = None
    active_thread_id: Optional[str] = None
    thread_ids: list[str] = dataclasses.field(default_factory=list)
    thread_summaries: dict[str, ThreadSummary] = dataclasses.field(default_factory=dict)
    pending_compact_seed: Optional[str] = None
    pending_compact_seed_thread_id: Optional[str] = None
    last_update_id: Optional[int] = None
    agent: Optional[str] = None
    agent_profile: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    summary: Optional[str] = None
    approval_policy: Optional[str] = None
    sandbox_policy: Optional[Any] = None
    rollout_path: Optional[str] = None
    approval_mode: str = APPROVAL_MODE_YOLO
    last_active_at: Optional[str] = None
    last_ticket_dispatch_seq: Optional[str] = None
    last_terminal_run_id: Optional[str] = None
    last_terminal_finished_at: Optional[str] = None

    @classmethod
    def from_dict(
        cls, payload: dict[str, Any], *, default_approval_mode: str
    ) -> "TelegramTopicRecord":
        repo_id = payload.get("repo_id") or payload.get("repoId")
        if not isinstance(repo_id, str):
            repo_id = None
        resource_kind = payload.get("resource_kind") or payload.get("resourceKind")
        if not isinstance(resource_kind, str):
            resource_kind = None
        resource_id = payload.get("resource_id") or payload.get("resourceId")
        if not isinstance(resource_id, str):
            resource_id = None
        workspace_path = payload.get("workspace_path") or payload.get("workspacePath")
        if not isinstance(workspace_path, str):
            workspace_path = None
        workspace_id = payload.get("workspace_id") or payload.get("workspaceId")
        if not isinstance(workspace_id, str):
            workspace_id = None
        pma_enabled = payload.get("pma_enabled") or payload.get("pmaEnabled")
        if not isinstance(pma_enabled, bool):
            pma_enabled = False
        pma_prev_repo_id = payload.get("pma_prev_repo_id") or payload.get(
            "pmaPrevRepoId"
        )
        if not isinstance(pma_prev_repo_id, str):
            pma_prev_repo_id = None
        pma_prev_resource_kind = payload.get("pma_prev_resource_kind") or payload.get(
            "pmaPrevResourceKind"
        )
        if not isinstance(pma_prev_resource_kind, str):
            pma_prev_resource_kind = None
        pma_prev_resource_id = payload.get("pma_prev_resource_id") or payload.get(
            "pmaPrevResourceId"
        )
        if not isinstance(pma_prev_resource_id, str):
            pma_prev_resource_id = None
        pma_prev_workspace_path = payload.get("pma_prev_workspace_path") or payload.get(
            "pmaPrevWorkspacePath"
        )
        if not isinstance(pma_prev_workspace_path, str):
            pma_prev_workspace_path = None
        pma_prev_workspace_id = payload.get("pma_prev_workspace_id") or payload.get(
            "pmaPrevWorkspaceId"
        )
        if not isinstance(pma_prev_workspace_id, str):
            pma_prev_workspace_id = None
        pma_prev_active_thread_id = payload.get(
            "pma_prev_active_thread_id"
        ) or payload.get("pmaPrevActiveThreadId")
        if not isinstance(pma_prev_active_thread_id, str):
            pma_prev_active_thread_id = None
        active_thread_id = payload.get("active_thread_id") or payload.get(
            "activeThreadId"
        )
        if not isinstance(active_thread_id, str):
            active_thread_id = None
        thread_ids_raw = payload.get("thread_ids") or payload.get("threadIds")
        thread_ids: list[str] = []
        if isinstance(thread_ids_raw, list):
            for item in thread_ids_raw:
                if isinstance(item, str) and item:
                    thread_ids.append(item)
        thread_summaries_raw = payload.get("thread_summaries") or payload.get(
            "threadSummaries"
        )
        thread_summaries: dict[str, ThreadSummary] = {}
        if isinstance(thread_summaries_raw, dict):
            for thread_id, summary in thread_summaries_raw.items():
                if not isinstance(thread_id, str):
                    continue
                if not isinstance(summary, dict):
                    continue
                parsed = ThreadSummary.from_dict(summary)
                if parsed is None:
                    continue
                thread_summaries[thread_id] = parsed
        pending_compact_seed = payload.get("pending_compact_seed") or payload.get(
            "pendingCompactSeed"
        )
        if not isinstance(pending_compact_seed, str):
            pending_compact_seed = None
        pending_compact_seed_thread_id = payload.get(
            "pending_compact_seed_thread_id"
        ) or payload.get("pendingCompactSeedThreadId")
        if not isinstance(pending_compact_seed_thread_id, str):
            pending_compact_seed_thread_id = None
        if not thread_ids and isinstance(active_thread_id, str):
            thread_ids = [active_thread_id]
        last_update_id = payload.get("last_update_id") or payload.get("lastUpdateId")
        if not isinstance(last_update_id, int) or isinstance(last_update_id, bool):
            last_update_id = None
        raw_agent = payload.get("agent")
        agent = normalize_agent(raw_agent)
        if agent is None and isinstance(raw_agent, str) and raw_agent.strip():
            agent = raw_agent.strip().lower()
        raw_agent_profile = payload.get("agent_profile") or payload.get("agentProfile")
        if not isinstance(raw_agent_profile, str):
            agent_profile = None
        else:
            agent_profile = normalize_hermes_profile(raw_agent_profile)
        legacy_hermes_profile = normalize_hermes_profile(agent)
        if agent_profile is None and legacy_hermes_profile is not None:
            agent_profile = legacy_hermes_profile
        if agent_profile is None and isinstance(raw_agent_profile, str):
            raw_stripped = raw_agent_profile.strip().lower()
            if raw_stripped and agent == "hermes":
                agent_profile = raw_stripped
        if agent_profile is not None:
            agent = "hermes"
        model = payload.get("model")
        if not isinstance(model, str):
            model = None
        effort = payload.get("effort") or payload.get("reasoningEffort")
        if not isinstance(effort, str):
            effort = None
        summary = payload.get("summary") or payload.get("summaryMode")
        if not isinstance(summary, str):
            summary = None
        approval_policy = payload.get("approval_policy") or payload.get(
            "approvalPolicy"
        )
        if not isinstance(approval_policy, str):
            approval_policy = None
        sandbox_policy = payload.get("sandbox_policy") or payload.get("sandboxPolicy")
        if not isinstance(sandbox_policy, (dict, str)):
            sandbox_policy = None
        rollout_path = (
            payload.get("rollout_path")
            or payload.get("rolloutPath")
            or payload.get("path")
        )
        if not isinstance(rollout_path, str):
            rollout_path = None
        approval_mode = payload.get("approval_mode") or payload.get("approvalMode")
        approval_mode = normalize_approval_mode(
            approval_mode, default=default_approval_mode
        )
        last_active_at = payload.get("last_active_at") or payload.get("lastActiveAt")
        if not isinstance(last_active_at, str):
            last_active_at = None
        last_ticket_dispatch_seq = payload.get(
            "last_ticket_dispatch_seq"
        ) or payload.get("lastTicketDispatchSeq")
        if not isinstance(last_ticket_dispatch_seq, str):
            last_ticket_dispatch_seq = None
        last_terminal_run_id = payload.get("last_terminal_run_id") or payload.get(
            "lastTerminalRunId"
        )
        if not isinstance(last_terminal_run_id, str):
            last_terminal_run_id = None
        last_terminal_finished_at = payload.get(
            "last_terminal_finished_at"
        ) or payload.get("lastTerminalFinishedAt")
        if not isinstance(last_terminal_finished_at, str):
            last_terminal_finished_at = None
        return cls(
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            workspace_path=workspace_path,
            workspace_id=workspace_id,
            pma_enabled=pma_enabled,
            pma_prev_repo_id=pma_prev_repo_id,
            pma_prev_resource_kind=pma_prev_resource_kind,
            pma_prev_resource_id=pma_prev_resource_id,
            pma_prev_workspace_path=pma_prev_workspace_path,
            pma_prev_workspace_id=pma_prev_workspace_id,
            pma_prev_active_thread_id=pma_prev_active_thread_id,
            active_thread_id=active_thread_id,
            thread_ids=thread_ids,
            thread_summaries=thread_summaries,
            pending_compact_seed=pending_compact_seed,
            pending_compact_seed_thread_id=pending_compact_seed_thread_id,
            last_update_id=last_update_id,
            agent=agent,
            agent_profile=agent_profile,
            model=model,
            effort=effort,
            summary=summary,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            rollout_path=rollout_path,
            approval_mode=approval_mode,
            last_active_at=last_active_at,
            last_ticket_dispatch_seq=last_ticket_dispatch_seq,
            last_terminal_run_id=last_terminal_run_id,
            last_terminal_finished_at=last_terminal_finished_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "workspace_path": self.workspace_path,
            "workspace_id": self.workspace_id,
            "pma_enabled": self.pma_enabled,
            "pma_prev_repo_id": self.pma_prev_repo_id,
            "pma_prev_resource_kind": self.pma_prev_resource_kind,
            "pma_prev_resource_id": self.pma_prev_resource_id,
            "pma_prev_workspace_path": self.pma_prev_workspace_path,
            "pma_prev_workspace_id": self.pma_prev_workspace_id,
            "pma_prev_active_thread_id": self.pma_prev_active_thread_id,
            "active_thread_id": self.active_thread_id,
            "thread_ids": list(self.thread_ids),
            "thread_summaries": {
                thread_id: summary.to_dict()
                for thread_id, summary in self.thread_summaries.items()
            },
            "pending_compact_seed": self.pending_compact_seed,
            "pending_compact_seed_thread_id": self.pending_compact_seed_thread_id,
            "last_update_id": self.last_update_id,
            "agent": self.agent,
            "agent_profile": self.agent_profile,
            "model": self.model,
            "effort": self.effort,
            "summary": self.summary,
            "approval_policy": self.approval_policy,
            "sandbox_policy": self.sandbox_policy,
            "rollout_path": self.rollout_path,
            "approval_mode": self.approval_mode,
            "last_active_at": self.last_active_at,
            "last_ticket_dispatch_seq": self.last_ticket_dispatch_seq,
            "last_terminal_run_id": self.last_terminal_run_id,
            "last_terminal_finished_at": self.last_terminal_finished_at,
        }


@dataclass
class TelegramState:
    version: int = STATE_VERSION
    topics: dict[str, TelegramTopicRecord] = dataclasses.field(default_factory=dict)
    topic_scopes: dict[str, str] = dataclasses.field(default_factory=dict)
    pending_approvals: dict[str, "PendingApprovalRecord"] = dataclasses.field(
        default_factory=dict
    )
    outbox: dict[str, "OutboxRecord"] = dataclasses.field(default_factory=dict)
    pending_voice: dict[str, "PendingVoiceRecord"] = dataclasses.field(
        default_factory=dict
    )
    last_update_id_global: Optional[int] = None

    def to_json(self) -> str:
        payload = {
            "version": self.version,
            "topics": {key: record.to_dict() for key, record in self.topics.items()},
            "topic_scopes": dict(self.topic_scopes),
            "pending_approvals": {
                key: record.to_dict() for key, record in self.pending_approvals.items()
            },
            "outbox": {key: record.to_dict() for key, record in self.outbox.items()},
            "pending_voice": {
                key: record.to_dict() for key, record in self.pending_voice.items()
            },
            "last_update_id_global": self.last_update_id_global,
        }
        return json.dumps(payload, indent=2) + "\n"


@dataclass
class PendingApprovalRecord:
    request_id: str
    turn_id: str
    chat_id: int
    thread_id: Optional[int]
    message_id: Optional[int]
    prompt: str
    created_at: str
    topic_key: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["PendingApprovalRecord"]:
        if not isinstance(payload, dict):
            return None
        request_id = payload.get("request_id")
        turn_id = payload.get("turn_id")
        chat_id = payload.get("chat_id")
        thread_id = payload.get("thread_id")
        message_id = payload.get("message_id")
        prompt = payload.get("prompt") or ""
        created_at = payload.get("created_at")
        topic_key = payload.get("topic_key") or payload.get("topicKey")
        if not isinstance(request_id, str) or not request_id:
            return None
        if not isinstance(turn_id, str) or not turn_id:
            return None
        if not isinstance(chat_id, int):
            return None
        if thread_id is not None and not isinstance(thread_id, int):
            thread_id = None
        if message_id is not None and not isinstance(message_id, int):
            message_id = None
        if not isinstance(prompt, str):
            prompt = ""
        if not isinstance(created_at, str) or not created_at:
            return None
        if not isinstance(topic_key, str) or not topic_key:
            topic_key = None
        return cls(
            request_id=request_id,
            turn_id=turn_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            prompt=prompt,
            created_at=created_at,
            topic_key=topic_key,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "prompt": self.prompt,
            "created_at": self.created_at,
            "topic_key": self.topic_key,
        }


@dataclass
class OutboxRecord:
    record_id: str
    chat_id: int
    thread_id: Optional[int]
    reply_to_message_id: Optional[int]
    placeholder_message_id: Optional[int]
    text: str
    created_at: str
    attempts: int = 0
    last_error: Optional[str] = None
    last_attempt_at: Optional[str] = None
    next_attempt_at: Optional[str] = None
    operation: Optional[str] = None
    operation_id: Optional[str] = None
    message_id: Optional[int] = None
    outbox_key: Optional[str] = None
    overflow_mode_override: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["OutboxRecord"]:
        if not isinstance(payload, dict):
            return None
        record_id = payload.get("record_id")
        chat_id = payload.get("chat_id")
        thread_id = payload.get("thread_id")
        reply_to_message_id = payload.get("reply_to_message_id")
        placeholder_message_id = payload.get("placeholder_message_id")
        text = payload.get("text") or ""
        created_at = payload.get("created_at")
        attempts = payload.get("attempts", 0)
        last_error = payload.get("last_error")
        last_attempt_at = payload.get("last_attempt_at")
        next_attempt_at = payload.get("next_attempt_at")
        operation = payload.get("operation")
        operation_id = payload.get("operation_id")
        message_id = payload.get("message_id")
        outbox_key = payload.get("outbox_key")
        overflow_mode_override = payload.get("overflow_mode_override")
        if not isinstance(record_id, str) or not record_id:
            return None
        if not isinstance(chat_id, int):
            return None
        if thread_id is not None and not isinstance(thread_id, int):
            thread_id = None
        if reply_to_message_id is not None and not isinstance(reply_to_message_id, int):
            reply_to_message_id = None
        if placeholder_message_id is not None and not isinstance(
            placeholder_message_id, int
        ):
            placeholder_message_id = None
        if not isinstance(text, str):
            text = ""
        if not isinstance(created_at, str) or not created_at:
            return None
        if not isinstance(attempts, int) or attempts < 0:
            attempts = 0
        if not isinstance(last_error, str):
            last_error = None
        if not isinstance(last_attempt_at, str):
            last_attempt_at = None
        if not isinstance(next_attempt_at, str):
            next_attempt_at = None
        if not isinstance(operation, str):
            operation = None
        if not isinstance(operation_id, str):
            operation_id = None
        if message_id is not None and not isinstance(message_id, int):
            message_id = None
        if not isinstance(outbox_key, str):
            outbox_key = None
        if not isinstance(overflow_mode_override, str):
            overflow_mode_override = None
        return cls(
            record_id=record_id,
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            placeholder_message_id=placeholder_message_id,
            text=text,
            created_at=created_at,
            attempts=attempts,
            last_error=last_error,
            last_attempt_at=last_attempt_at,
            next_attempt_at=next_attempt_at,
            operation=operation,
            operation_id=operation_id,
            message_id=message_id,
            outbox_key=outbox_key,
            overflow_mode_override=overflow_mode_override,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "reply_to_message_id": self.reply_to_message_id,
            "placeholder_message_id": self.placeholder_message_id,
            "text": self.text,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "last_attempt_at": self.last_attempt_at,
            "next_attempt_at": self.next_attempt_at,
            "operation": self.operation,
            "operation_id": self.operation_id,
            "message_id": self.message_id,
            "outbox_key": self.outbox_key,
            "overflow_mode_override": self.overflow_mode_override,
        }


@dataclass
class PendingVoiceRecord:
    record_id: str
    chat_id: int
    thread_id: Optional[int]
    message_id: int
    file_id: str
    file_name: Optional[str]
    caption: str
    file_size: Optional[int]
    mime_type: Optional[str]
    duration: Optional[int]
    workspace_path: Optional[str]
    created_at: str
    attempts: int = 0
    last_error: Optional[str] = None
    last_attempt_at: Optional[str] = None
    next_attempt_at: Optional[str] = None
    download_path: Optional[str] = None
    progress_message_id: Optional[int] = None
    transcript_message_id: Optional[int] = None
    transcript_text: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["PendingVoiceRecord"]:
        if not isinstance(payload, dict):
            return None
        record_id = payload.get("record_id")
        chat_id = payload.get("chat_id")
        thread_id = payload.get("thread_id")
        message_id = payload.get("message_id")
        file_id = payload.get("file_id")
        file_name = payload.get("file_name")
        caption = payload.get("caption") or ""
        file_size = payload.get("file_size")
        mime_type = payload.get("mime_type")
        duration = payload.get("duration")
        workspace_path = payload.get("workspace_path")
        created_at = payload.get("created_at")
        attempts = payload.get("attempts", 0)
        last_error = payload.get("last_error")
        last_attempt_at = payload.get("last_attempt_at")
        next_attempt_at = payload.get("next_attempt_at")
        download_path = payload.get("download_path")
        progress_message_id = payload.get("progress_message_id")
        transcript_message_id = payload.get("transcript_message_id")
        transcript_text = payload.get("transcript_text")
        if not isinstance(record_id, str) or not record_id:
            return None
        if not isinstance(chat_id, int):
            return None
        if thread_id is not None and not isinstance(thread_id, int):
            thread_id = None
        if not isinstance(message_id, int):
            return None
        if not isinstance(file_id, str) or not file_id:
            return None
        if not isinstance(file_name, str):
            file_name = None
        if not isinstance(caption, str):
            caption = ""
        if file_size is not None and not isinstance(file_size, int):
            file_size = None
        if not isinstance(mime_type, str):
            mime_type = None
        if duration is not None and not isinstance(duration, int):
            duration = None
        if not isinstance(workspace_path, str):
            workspace_path = None
        if not isinstance(created_at, str) or not created_at:
            return None
        if not isinstance(attempts, int) or attempts < 0:
            attempts = 0
        if not isinstance(last_error, str):
            last_error = None
        if not isinstance(last_attempt_at, str):
            last_attempt_at = None
        if not isinstance(next_attempt_at, str):
            next_attempt_at = None
        if not isinstance(download_path, str):
            download_path = None
        if progress_message_id is not None and not isinstance(progress_message_id, int):
            progress_message_id = None
        if transcript_message_id is not None and not isinstance(
            transcript_message_id, int
        ):
            transcript_message_id = None
        if not isinstance(transcript_text, str):
            transcript_text = None
        return cls(
            record_id=record_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            file_id=file_id,
            file_name=file_name,
            caption=caption,
            file_size=file_size,
            mime_type=mime_type,
            duration=duration,
            workspace_path=workspace_path,
            created_at=created_at,
            attempts=attempts,
            last_error=last_error,
            last_attempt_at=last_attempt_at,
            next_attempt_at=next_attempt_at,
            download_path=download_path,
            progress_message_id=progress_message_id,
            transcript_message_id=transcript_message_id,
            transcript_text=transcript_text,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "file_id": self.file_id,
            "file_name": self.file_name,
            "caption": self.caption,
            "file_size": self.file_size,
            "mime_type": self.mime_type,
            "duration": self.duration,
            "workspace_path": self.workspace_path,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "last_attempt_at": self.last_attempt_at,
            "next_attempt_at": self.next_attempt_at,
            "download_path": self.download_path,
            "progress_message_id": self.progress_message_id,
            "transcript_message_id": self.transcript_message_id,
            "transcript_text": self.transcript_text,
        }
