from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TypedDict

from .logging_utils import log_event

FORCE_ATTESTATION_REQUIRED_PHRASE = (
    "the user has explicitly asked me to perform a dangerous action and I'm confident "
    "I'm not misunderstanding them"
)
FORCE_ATTESTATION_REQUIRED_ERROR = (
    "--force requires --force-attestation for dangerous actions."
)


class ForceAttestation(TypedDict):
    phrase: str
    user_request: str
    target_scope: str


def _required_text(fields: Mapping[str, object], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str):
        raise ValueError(FORCE_ATTESTATION_REQUIRED_ERROR)
    text = value.strip()
    if not text:
        raise ValueError(FORCE_ATTESTATION_REQUIRED_ERROR)
    return text


def validate_force_attestation(
    force_attestation: Mapping[str, object] | None,
) -> ForceAttestation:
    if force_attestation is None:
        raise ValueError(FORCE_ATTESTATION_REQUIRED_ERROR)
    if not isinstance(force_attestation, Mapping):
        raise ValueError(FORCE_ATTESTATION_REQUIRED_ERROR)

    phrase = _required_text(force_attestation, "phrase")
    user_request = _required_text(force_attestation, "user_request")
    target_scope = _required_text(force_attestation, "target_scope")
    if phrase != FORCE_ATTESTATION_REQUIRED_PHRASE:
        raise ValueError(FORCE_ATTESTATION_REQUIRED_ERROR)
    return {
        "phrase": phrase,
        "user_request": user_request,
        "target_scope": target_scope,
    }


def enforce_force_attestation(
    *,
    force: bool,
    force_attestation: Mapping[str, object] | None,
    logger: logging.Logger,
    action: str,
) -> None:
    if not force:
        return
    approved = validate_force_attestation(force_attestation)
    log_event(
        logger,
        logging.INFO,
        "force_attestation.approved",
        action=action,
        target_scope=approved["target_scope"],
        user_request_chars=len(approved["user_request"]),
    )
