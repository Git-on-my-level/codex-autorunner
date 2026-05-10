from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Optional

from .locks import file_lock
from .time_utils import now_iso
from .utils import atomic_write

InteractionKind = Literal[
    "approval",
    "single_choice_question",
    "multi_choice_question",
    "custom_text_input",
    "selection",
]
InteractionStatus = Literal["pending", "answered", "expired", "canceled"]

INTERACTION_INBOX_FILENAME = "interaction_inbox.json"
INTERACTION_INBOX_VERSION = 1
PENDING_INTERACTION_STATUSES = {"pending"}


class InteractionInboxError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class InteractionOption:
    id: str
    label: str
    value: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "InteractionOption":
        option_id = _required_text(data.get("id"), "option id")
        label = _required_text(data.get("label"), "option label")
        metadata = data.get("metadata")
        return cls(
            id=option_id,
            label=label,
            value=data.get("value"),
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "label": self.label}
        if self.value is not None:
            payload["value"] = self.value
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class InteractionPrompt:
    id: str
    kind: InteractionKind
    title: str
    message: str
    owner: dict[str, Any]
    target_scope: dict[str, Any]
    requester_user_id: Optional[str] = None
    allowed_actor_user_ids: tuple[str, ...] = ()
    options: tuple[InteractionOption, ...] = ()
    expires_at: Optional[str] = None
    source: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: InteractionStatus = "pending"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    claimed_by_user_id: Optional[str] = None
    response: Optional[dict[str, Any]] = None
    delivered_surfaces: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "InteractionPrompt":
        options = data.get("options")
        allowed = data.get("allowed_actor_user_ids")
        return cls(
            id=_required_text(data.get("id"), "prompt id"),
            kind=_normalize_kind(data.get("kind")),
            title=_required_text(data.get("title"), "prompt title"),
            message=str(data.get("message") or ""),
            owner=_mapping(data.get("owner")),
            target_scope=_mapping(data.get("target_scope")),
            requester_user_id=_optional_text(data.get("requester_user_id")),
            allowed_actor_user_ids=tuple(
                str(item)
                for item in (allowed if isinstance(allowed, list) else [])
                if str(item).strip()
            ),
            options=tuple(
                InteractionOption.from_mapping(item)
                for item in (options if isinstance(options, list) else [])
                if isinstance(item, Mapping)
            ),
            expires_at=_optional_text(data.get("expires_at")),
            source=_mapping(data.get("source")),
            metadata=_mapping(data.get("metadata")),
            status=_normalize_status(data.get("status")),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            claimed_by_user_id=_optional_text(data.get("claimed_by_user_id")),
            response=(
                dict(data["response"])
                if isinstance(data.get("response"), Mapping)
                else None
            ),
            delivered_surfaces={
                str(key): dict(value)
                for key, value in _mapping(data.get("delivered_surfaces")).items()
                if isinstance(value, Mapping)
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "message": self.message,
            "owner": dict(self.owner),
            "target_scope": dict(self.target_scope),
            "requester_user_id": self.requester_user_id,
            "allowed_actor_user_ids": list(self.allowed_actor_user_ids),
            "options": [option.to_dict() for option in self.options],
            "expires_at": self.expires_at,
            "source": dict(self.source),
            "metadata": dict(self.metadata),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "claimed_by_user_id": self.claimed_by_user_id,
            "response": dict(self.response) if self.response is not None else None,
            "delivered_surfaces": {
                key: dict(value) for key, value in self.delivered_surfaces.items()
            },
        }


def default_interaction_inbox_path(state_path: Path) -> Path:
    return state_path.parent / INTERACTION_INBOX_FILENAME


def create_prompt(
    prompt: InteractionPrompt, *, now: Optional[str] = None
) -> InteractionPrompt:
    _validate_prompt_shape(prompt)
    timestamp = now or now_iso()
    return InteractionPrompt.from_mapping(
        {
            **prompt.to_dict(),
            "status": "pending",
            "created_at": prompt.created_at or timestamp,
            "updated_at": timestamp,
            "response": None,
        }
    )


def claim_or_validate_actor(
    prompt: InteractionPrompt,
    *,
    actor_user_id: Optional[str],
    now: Optional[str] = None,
) -> InteractionPrompt:
    _ensure_pending(prompt, now=now)
    normalized_actor = _optional_text(actor_user_id)
    if prompt.requester_user_id and normalized_actor != prompt.requester_user_id:
        raise InteractionInboxError(
            "unauthorized_actor", "Prompt belongs to another user"
        )
    if (
        prompt.allowed_actor_user_ids
        and normalized_actor not in prompt.allowed_actor_user_ids
    ):
        raise InteractionInboxError("unauthorized_actor", "Actor is not allowed")
    if prompt.claimed_by_user_id and normalized_actor != prompt.claimed_by_user_id:
        raise InteractionInboxError(
            "unauthorized_actor", "Prompt is claimed by another user"
        )
    if prompt.claimed_by_user_id or normalized_actor is None:
        return prompt
    return _replace_prompt(
        prompt,
        claimed_by_user_id=normalized_actor,
        updated_at=now or now_iso(),
    )


def apply_response(
    prompt: InteractionPrompt,
    *,
    actor_user_id: Optional[str],
    response: Mapping[str, Any],
    now: Optional[str] = None,
) -> InteractionPrompt:
    timestamp = now or now_iso()
    prompt = claim_or_validate_actor(prompt, actor_user_id=actor_user_id, now=timestamp)
    validated = _validate_response(prompt, response)
    return _replace_prompt(
        prompt,
        status="answered",
        response={
            **validated,
            "actor_user_id": _optional_text(actor_user_id),
            "responded_at": timestamp,
        },
        updated_at=timestamp,
    )


def expire_prompt(
    prompt: InteractionPrompt, *, now: Optional[str] = None
) -> InteractionPrompt:
    if prompt.status != "pending":
        return prompt
    return _replace_prompt(prompt, status="expired", updated_at=now or now_iso())


def cancel_prompt(
    prompt: InteractionPrompt,
    *,
    reason: Optional[str] = None,
    now: Optional[str] = None,
) -> InteractionPrompt:
    if prompt.status != "pending":
        raise InteractionInboxError("already_answered", "Prompt is already closed")
    response = {"reason": reason} if reason else None
    return _replace_prompt(
        prompt,
        status="canceled",
        response=response,
        updated_at=now or now_iso(),
    )


def mark_delivered(
    prompt: InteractionPrompt,
    *,
    surface: str,
    visible: bool = True,
    delivery_metadata: Optional[Mapping[str, Any]] = None,
    now: Optional[str] = None,
) -> InteractionPrompt:
    surface_id = _required_text(surface, "surface")
    delivered = dict(prompt.delivered_surfaces)
    delivered[surface_id] = {
        "visible": bool(visible),
        "delivered_at": now or now_iso(),
        **(dict(delivery_metadata) if delivery_metadata else {}),
    }
    return _replace_prompt(
        prompt,
        delivered_surfaces=delivered,
        updated_at=now or now_iso(),
    )


class InteractionInboxStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")

    def upsert_prompt(self, prompt: InteractionPrompt) -> InteractionPrompt:
        prompt = create_prompt(prompt)
        with file_lock(self.lock_path):
            state = self._read_unlocked()
            prompts = state.get("prompts")
            items = dict(prompts) if isinstance(prompts, Mapping) else {}
            items[prompt.id] = prompt.to_dict()
            state["version"] = INTERACTION_INBOX_VERSION
            state["updated_at"] = now_iso()
            state["prompts"] = items
            self._write_unlocked(state)
        return prompt

    def get_prompt(self, prompt_id: str) -> Optional[InteractionPrompt]:
        with file_lock(self.lock_path):
            raw = self._read_unlocked().get("prompts", {}).get(prompt_id)
        return InteractionPrompt.from_mapping(raw) if isinstance(raw, Mapping) else None

    def list_prompts(
        self,
        *,
        statuses: Optional[Iterable[str]] = None,
        kind: Optional[str] = None,
        include_expired: bool = False,
        now: Optional[str] = None,
    ) -> list[InteractionPrompt]:
        status_filter = {str(status) for status in statuses} if statuses else None
        with file_lock(self.lock_path):
            state = self._read_unlocked()
            prompts = state.get("prompts")
            items = [
                InteractionPrompt.from_mapping(item)
                for item in (prompts.values() if isinstance(prompts, Mapping) else [])
                if isinstance(item, Mapping)
            ]
            changed = False
            updated: dict[str, Any] = {}
            for prompt in items:
                current = prompt
                if (
                    current.status == "pending"
                    and not include_expired
                    and _is_expired(current, now=now)
                ):
                    current = expire_prompt(current, now=now)
                    changed = True
                updated[current.id] = current.to_dict()
            if changed:
                state["updated_at"] = now_iso()
                state["prompts"] = updated
                self._write_unlocked(state)
                items = [
                    InteractionPrompt.from_mapping(item) for item in updated.values()
                ]
        filtered = []
        for prompt in items:
            if status_filter is not None and prompt.status not in status_filter:
                continue
            if kind is not None and prompt.kind != kind:
                continue
            filtered.append(prompt)
        return sorted(filtered, key=lambda prompt: (prompt.created_at, prompt.id))

    def respond(
        self,
        prompt_id: str,
        *,
        actor_user_id: Optional[str],
        response: Mapping[str, Any],
        now: Optional[str] = None,
    ) -> InteractionPrompt:
        with file_lock(self.lock_path):
            state = self._read_unlocked()
            prompts = state.get("prompts")
            items = dict(prompts) if isinstance(prompts, Mapping) else {}
            raw = items.get(prompt_id)
            if not isinstance(raw, Mapping):
                raise InteractionInboxError("not_found", "Prompt not found")
            updated = apply_response(
                InteractionPrompt.from_mapping(raw),
                actor_user_id=actor_user_id,
                response=response,
                now=now,
            )
            items[prompt_id] = updated.to_dict()
            state["version"] = INTERACTION_INBOX_VERSION
            state["updated_at"] = now_iso()
            state["prompts"] = items
            self._write_unlocked(state)
            return updated

    def replace_prompt(self, prompt: InteractionPrompt) -> None:
        with file_lock(self.lock_path):
            state = self._read_unlocked()
            prompts = state.get("prompts")
            items = dict(prompts) if isinstance(prompts, Mapping) else {}
            items[prompt.id] = prompt.to_dict()
            state["version"] = INTERACTION_INBOX_VERSION
            state["updated_at"] = now_iso()
            state["prompts"] = items
            self._write_unlocked(state)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": INTERACTION_INBOX_VERSION,
                "prompts": {},
                "updated_at": now_iso(),
            }
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "version": INTERACTION_INBOX_VERSION,
                "prompts": {},
                "updated_at": now_iso(),
            }
        return (
            data
            if isinstance(data, dict)
            else {
                "version": INTERACTION_INBOX_VERSION,
                "prompts": {},
                "updated_at": now_iso(),
            }
        )

    def _write_unlocked(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _validate_prompt_shape(prompt: InteractionPrompt) -> None:
    if (
        prompt.kind in {"single_choice_question", "multi_choice_question", "selection"}
        and not prompt.options
    ):
        raise InteractionInboxError("invalid_prompt", "Prompt kind requires options")
    if prompt.kind == "approval":
        option_ids = {option.id for option in prompt.options}
        if prompt.options and not {"approve", "decline"}.issubset(option_ids):
            raise InteractionInboxError(
                "invalid_prompt", "Approval options must include approve and decline"
            )


def _ensure_pending(prompt: InteractionPrompt, *, now: Optional[str]) -> None:
    if prompt.status != "pending":
        raise InteractionInboxError("already_answered", "Prompt is already closed")
    if _is_expired(prompt, now=now):
        raise InteractionInboxError("expired", "Prompt has expired")


def _validate_response(
    prompt: InteractionPrompt, response: Mapping[str, Any]
) -> dict[str, Any]:
    option_ids = {option.id for option in prompt.options}
    if prompt.kind == "approval":
        decision = str(
            response.get("decision") or response.get("option_id") or ""
        ).strip()
        if decision not in {"approve", "decline"}:
            raise InteractionInboxError(
                "invalid_response", "Approval decision must be approve or decline"
            )
        return {"decision": decision}
    if prompt.kind in {"single_choice_question", "selection"}:
        option_id = str(response.get("option_id") or "").strip()
        if option_id not in option_ids:
            raise InteractionInboxError(
                "invalid_response", "Selected option is invalid"
            )
        return {"option_id": option_id}
    if prompt.kind == "multi_choice_question":
        raw_ids = response.get("option_ids")
        option_id_list = raw_ids if isinstance(raw_ids, list) else []
        selected = [str(option_id).strip() for option_id in option_id_list]
        if not selected or any(option_id not in option_ids for option_id in selected):
            raise InteractionInboxError(
                "invalid_response", "Selected options are invalid"
            )
        return {"option_ids": selected}
    if prompt.kind == "custom_text_input":
        text = response.get("text")
        if not isinstance(text, str) or not text:
            raise InteractionInboxError("invalid_response", "Text response is required")
        return {"text": text}
    raise InteractionInboxError("invalid_prompt", "Unsupported prompt kind")


def _replace_prompt(prompt: InteractionPrompt, **updates: Any) -> InteractionPrompt:
    payload = prompt.to_dict()
    payload.update(updates)
    return InteractionPrompt.from_mapping(payload)


def _is_expired(prompt: InteractionPrompt, *, now: Optional[str]) -> bool:
    if not prompt.expires_at:
        return False
    return str(now or now_iso()) >= prompt.expires_at


def _required_text(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise InteractionInboxError("invalid_prompt", f"{label} is required")
    return normalized


def _optional_text(value: Any) -> Optional[str]:
    normalized = str(value).strip() if value is not None else ""
    return normalized or None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _normalize_kind(value: Any) -> InteractionKind:
    normalized = str(value or "").strip()
    if normalized not in {
        "approval",
        "single_choice_question",
        "multi_choice_question",
        "custom_text_input",
        "selection",
    }:
        raise InteractionInboxError("invalid_prompt", "Unsupported prompt kind")
    return normalized  # type: ignore[return-value]


def _normalize_status(value: Any) -> InteractionStatus:
    normalized = str(value or "pending").strip()
    if normalized not in {"pending", "answered", "expired", "canceled"}:
        return "pending"
    return normalized  # type: ignore[return-value]
