from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .text_utils import _mapping


@dataclass(frozen=True)
class ResolvedWebhookConfig:
    secret: Optional[str] = None
    verify_signatures: bool = True
    allow_unsigned: bool = False


DEFAULT_MAX_PAYLOAD_BYTES = 262_144
DEFAULT_MAX_RAW_PAYLOAD_BYTES = 65_536


def github_automation_config(raw_config: object) -> Mapping[str, Any]:
    github = _mapping(raw_config).get("github")
    automation = _mapping(github).get("automation")
    return _mapping(automation)


def github_webhook_ingress_config(raw_config: object) -> Mapping[str, Any]:
    ingress = github_automation_config(raw_config).get("webhook_ingress")
    return _mapping(ingress)


def github_automation_enabled(raw_config: object) -> bool:
    return bool(github_automation_config(raw_config).get("enabled"))


def github_webhook_ingress_enabled(raw_config: object) -> bool:
    ingress = github_webhook_ingress_config(raw_config)
    return github_automation_enabled(raw_config) and bool(ingress.get("enabled"))


def drain_inline_enabled(raw_config: object) -> bool:
    return _resolve_bool(
        github_automation_config(raw_config).get("drain_inline"),
        default=False,
    )


def resolve_github_webhook_config(raw_config: object) -> ResolvedWebhookConfig:
    automation = github_automation_config(raw_config)
    ingress = github_webhook_ingress_config(raw_config)
    return ResolvedWebhookConfig(
        secret=_resolve_secret(automation, ingress),
        verify_signatures=_resolve_bool(
            ingress.get("verify_signatures", automation.get("verify_signatures")),
            default=True,
        ),
        allow_unsigned=_resolve_bool(
            ingress.get("allow_unsigned", automation.get("allow_unsigned")),
            default=False,
        ),
    )


def resolve_payload_limits(
    raw_config: object,
) -> tuple[int, int, bool]:
    ingress = github_webhook_ingress_config(raw_config)
    max_payload_bytes = _resolve_int(
        ingress.get("max_payload_bytes"),
        default=DEFAULT_MAX_PAYLOAD_BYTES,
    )
    max_raw_payload_bytes = _resolve_int(
        ingress.get("max_raw_payload_bytes"),
        default=DEFAULT_MAX_RAW_PAYLOAD_BYTES,
    )
    store_raw_payload = _resolve_bool(
        ingress.get("store_raw_payload"),
        default=False,
    )
    return max_payload_bytes, max_raw_payload_bytes, store_raw_payload


def _resolve_secret(*configs: Mapping[str, Any]) -> Optional[str]:
    secret: Optional[str] = None
    secret_env: Optional[str] = None
    for config in configs:
        raw_secret = config.get("secret")
        if isinstance(raw_secret, str) and raw_secret.strip():
            secret = raw_secret.strip()
        raw_secret_env = config.get("secret_env")
        if isinstance(raw_secret_env, str) and raw_secret_env.strip():
            secret_env = raw_secret_env.strip()
    if secret_env:
        env_value = os.getenv(secret_env)
        if isinstance(env_value, str) and env_value.strip():
            return env_value.strip()
    return secret


def _resolve_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _resolve_int(value: object, *, default: int) -> int:
    if isinstance(value, int) and value > 0:
        return value
    return default


__all__ = [
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_MAX_RAW_PAYLOAD_BYTES",
    "drain_inline_enabled",
    "github_automation_config",
    "github_automation_enabled",
    "github_webhook_ingress_config",
    "github_webhook_ingress_enabled",
    "resolve_github_webhook_config",
    "resolve_payload_limits",
]
