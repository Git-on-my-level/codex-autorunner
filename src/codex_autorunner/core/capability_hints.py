from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from ..voice.config import VoiceConfig
from .config import (
    CONFIG_FILENAME,
    REPO_OVERRIDE_FILENAME,
    ConfigError,
    HubConfig,
    load_repo_config,
    load_root_defaults,
    resolve_env_for_root,
)
from .generated_hub_config import normalize_generated_hub_config
from .text_utils import _mapping

CAPABILITY_HINT_ITEM_TYPE = "capability_hint"
CAPABILITY_HINT_VERSION = 1

VOICE_HINT_ID = "voice_enablement"
SCM_HINT_ID = "scm_automation_enablement"
PMA_HINT_ID = "pma_enablement"
SHELL_HINT_ID = "shell_enablement"
HUB_HINT_REPO_ID = "__hub__"
HUB_SCOPED_HINT_IDS = frozenset({PMA_HINT_ID, SHELL_HINT_ID})


def capability_hint_run_id(hint_id: str) -> str:
    return f"capability_hint:{hint_id.strip()}"


def capability_hint_id_from_run_id(run_id: str) -> Optional[str]:
    normalized = str(run_id or "").strip()
    prefix = "capability_hint:"
    if not normalized.startswith(prefix):
        return None
    hint_id = normalized[len(prefix) :].strip()
    return hint_id or None


def build_repo_capability_hints(
    *,
    hub_config: HubConfig,
    repo_id: str,
    repo_root: Path,
    repo_display_name: Optional[str] = None,
) -> list[dict[str, Any]]:
    try:
        repo_config = load_repo_config(repo_root, hub_path=hub_config.root)
        explicit_repo_config = _load_explicit_repo_config(
            hub_root=hub_config.root,
            repo_root=repo_root,
        )
    except ConfigError:
        return []

    display_name = repo_display_name or repo_id
    hints: list[dict[str, Any]] = []

    voice_hint = _build_voice_hint(
        repo_id=repo_id,
        repo_root=repo_root,
        repo_display_name=display_name,
        repo_config=repo_config,
        explicit_repo_config=explicit_repo_config,
    )
    if voice_hint is not None:
        hints.append(voice_hint)

    scm_hint = _build_scm_hint(
        repo_id=repo_id,
        repo_display_name=display_name,
        repo_config=repo_config,
        explicit_repo_config=explicit_repo_config,
    )
    if scm_hint is not None:
        hints.append(scm_hint)

    return hints


def build_hub_capability_hints(*, hub_config: HubConfig) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []

    pma_hint = _build_pma_hint(
        repo_id=HUB_HINT_REPO_ID,
        repo_display_name="Hub",
        hub_config=hub_config,
    )
    if pma_hint is not None:
        hints.append(pma_hint)

    shell_hint = _build_shell_hint(
        repo_id=HUB_HINT_REPO_ID,
        repo_display_name="Hub",
        hub_config=hub_config,
    )
    if shell_hint is not None:
        hints.append(shell_hint)

    return hints


def is_hub_scoped_hint_id(hint_id: Optional[str]) -> bool:
    return str(hint_id or "").strip() in HUB_SCOPED_HINT_IDS


def _build_voice_hint(
    *,
    repo_id: str,
    repo_root: Path,
    repo_display_name: str,
    repo_config: Any,
    explicit_repo_config: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    voice_config_source = _mapping(explicit_repo_config.get("voice"))
    if not voice_config_source:
        return None
    enabled = _explicit_bool(voice_config_source, "enabled")
    if enabled is None:
        return None
    workspace_env = resolve_env_for_root(repo_root, base_env=os.environ)
    voice_config = VoiceConfig.from_raw(repo_config.voice, env=workspace_env)
    provider_cfg = voice_config.providers.get(voice_config.provider or "", {})
    api_key_env = provider_cfg.get("api_key_env")
    api_key_env_name = api_key_env.strip() if isinstance(api_key_env, str) else ""
    requires_api_key = bool(api_key_env_name)
    has_api_key = (
        bool(workspace_env.get(api_key_env_name)) if requires_api_key else True
    )

    reason_code: Optional[str] = None
    prerequisite_text: Optional[str] = None

    if enabled and not has_api_key:
        reason_code = "voice_missing_api_key"
        prerequisite_text = (
            f"Set `{api_key_env_name}` for provider `{voice_config.provider}`."
            if api_key_env_name
            else None
        )

    if reason_code is None and not enabled:
        reason_code = "voice_disabled"

    if reason_code is None:
        return None

    lines = [
        "Voice transcription exists for this workspace but is not currently usable.",
        "Enable/configure: `voice.enabled` and `voice.provider`.",
    ]
    if prerequisite_text:
        lines.append(prerequisite_text)
    lines.extend(
        [
            "",
            "PMA prompt:",
            (
                "Enable voice transcription for this workspace. Turn on "
                "`voice.enabled`, choose an appropriate provider "
                "(`openai_whisper`, `local_whisper`, or `mlx_whisper`), "
                "install any required dependencies, and verify the voice "
                "config works end to end. Report back the exact config keys "
                "or package changes needed."
            ),
        ]
    )
    return _hint_item(
        repo_id=repo_id,
        repo_display_name=repo_display_name,
        hint_id=VOICE_HINT_ID,
        reason_code=reason_code,
        title="Enable voice transcription",
        body="\n".join(lines),
        queue_rank=90_000,
    )


def _build_scm_hint(
    *,
    repo_id: str,
    repo_display_name: str,
    repo_config: Any,
    explicit_repo_config: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    github_config_source = _mapping(explicit_repo_config.get("github"))
    automation = _mapping(github_config_source.get("automation"))
    if not automation:
        return None

    enabled = _explicit_bool(automation, "enabled")
    ingress = _mapping(automation.get("webhook_ingress"))
    ingress_enabled = _explicit_bool(ingress, "enabled")
    polling = _mapping(automation.get("polling"))
    polling_enabled = _explicit_bool(polling, "enabled")

    if enabled is False:
        reason_code = "scm_automation_disabled"
        prerequisite = "Enable `github.automation.enabled`."
    elif enabled is True and ingress_enabled is False and polling_enabled is False:
        reason_code = "scm_automation_transport_disabled"
        prerequisite = (
            "Enable `github.automation.polling.enabled` or "
            "`github.automation.webhook_ingress.enabled`."
        )
    else:
        return None

    body = "\n".join(
        [
            "GitHub SCM automation support exists for this repo but is disabled.",
            prerequisite,
            "",
            "PMA prompt:",
            (
                "Enable GitHub SCM automation for this repo. Turn on "
                "`github.automation.enabled`, choose either polling or "
                "webhook ingress as the transport, and confirm PRs are bound "
                "to a managed thread so review feedback and CI failures can "
                "be routed into follow-up work. Report back any remaining "
                "prerequisites."
            ),
        ]
    )
    return _hint_item(
        repo_id=repo_id,
        repo_display_name=repo_display_name,
        hint_id=SCM_HINT_ID,
        reason_code=reason_code,
        title="Enable GitHub SCM automation",
        body=body,
        queue_rank=90_010,
    )


def _build_pma_hint(
    *,
    repo_id: str,
    repo_display_name: str,
    hub_config: HubConfig,
) -> Optional[dict[str, Any]]:
    if hub_config.pma.enabled:
        return None
    body = "\n".join(
        [
            "PMA exists for this hub but is currently disabled.",
            "Enable/configure: `pma.enabled`.",
            "",
            "PMA prompt:",
            (
                "Enable PMA for this hub. Turn on `pma.enabled`, verify the "
                "managed thread routes are reachable, and report back any "
                "remaining prerequisites or config changes needed."
            ),
        ]
    )
    return _hint_item(
        repo_id=repo_id,
        repo_display_name=repo_display_name,
        hint_id=PMA_HINT_ID,
        reason_code="pma_disabled",
        title="Enable PMA",
        body=body,
        queue_rank=90_020,
        open_url="/",
    )


def _build_shell_hint(
    *,
    repo_id: str,
    repo_display_name: str,
    hub_config: HubConfig,
) -> Optional[dict[str, Any]]:
    raw = hub_config.raw if isinstance(hub_config.raw, dict) else {}
    discord_bot = _mapping(raw.get("discord_bot"))
    telegram_bot = _mapping(raw.get("telegram_bot"))
    discord_shell = _mapping(discord_bot.get("shell"))
    telegram_shell = _mapping(telegram_bot.get("shell"))
    disabled_keys: list[str] = []
    if bool(discord_bot.get("enabled", False)) and not bool(
        discord_shell.get("enabled", False)
    ):
        disabled_keys.append("`discord_bot.shell.enabled`")
    if bool(telegram_bot.get("enabled", False)) and not bool(
        telegram_shell.get("enabled", False)
    ):
        disabled_keys.append("`telegram_bot.shell.enabled`")
    if not disabled_keys:
        return None
    body = "\n".join(
        [
            "Chat shell command support exists but is disabled on one or more chat surfaces.",
            "Enable/configure: " + ", ".join(disabled_keys) + ".",
            "",
            "PMA prompt:",
            (
                "Enable chat shell commands for the configured chat surfaces. "
                "Turn on the relevant `*.shell.enabled` keys, confirm timeout "
                "and output limits are appropriate, and report back the exact "
                "config changes needed."
            ),
        ]
    )
    return _hint_item(
        repo_id=repo_id,
        repo_display_name=repo_display_name,
        hint_id=SHELL_HINT_ID,
        reason_code="shell_disabled",
        title="Enable chat shell commands",
        body=body,
        queue_rank=90_030,
        open_url="/",
    )


def _hint_item(
    *,
    repo_id: str,
    repo_display_name: str,
    hint_id: str,
    reason_code: str,
    title: str,
    body: str,
    queue_rank: int,
    open_url: Optional[str] = None,
) -> dict[str, Any]:
    run_id = capability_hint_run_id(hint_id)
    return {
        "repo_id": repo_id,
        "repo_display_name": repo_display_name,
        "run_id": run_id,
        "item_type": CAPABILITY_HINT_ITEM_TYPE,
        "hint_id": hint_id,
        "hint_version": CAPABILITY_HINT_VERSION,
        "reason_code": reason_code,
        "queue_rank": queue_rank,
        "queue_source": "capability_hint",
        "dispatch_actionable": False,
        "open_url": open_url or f"/repos/{repo_id}/",
        "dispatch": {
            "mode": CAPABILITY_HINT_ITEM_TYPE,
            "title": title,
            "body": body,
            "extra": {
                "hint_id": hint_id,
                "reason_code": reason_code,
                "hint_version": CAPABILITY_HINT_VERSION,
            },
            "is_handoff": False,
        },
    }


def _explicit_bool(mapping: Mapping[str, Any], key: str) -> Optional[bool]:
    if key not in mapping:
        return None
    value = mapping.get(key)
    if isinstance(value, bool):
        return value
    return None


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_explicit_repo_config(*, hub_root: Path, repo_root: Path) -> dict[str, Any]:
    explicit_repo_config: dict[str, Any] = {}
    root_defaults = load_root_defaults(hub_root)
    explicit_repo_defaults = _mapping(root_defaults.get("repo_defaults"))
    if explicit_repo_defaults:
        explicit_repo_config = _merge_mappings(
            explicit_repo_config, explicit_repo_defaults
        )
    hub_generated_config = normalize_generated_hub_config(hub_root / CONFIG_FILENAME)
    explicit_generated_repo_defaults = _mapping(
        hub_generated_config.get("repo_defaults")
    )
    if explicit_generated_repo_defaults:
        explicit_repo_config = _merge_mappings(
            explicit_repo_config, explicit_generated_repo_defaults
        )
    repo_override = _load_yaml_mapping(repo_root / REPO_OVERRIDE_FILENAME)
    if repo_override:
        explicit_repo_config = _merge_mappings(explicit_repo_config, repo_override)
    return explicit_repo_config


def _merge_mappings(
    base: Mapping[str, Any], overlay: Mapping[str, Any]
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_mappings(_mapping(merged.get(key)), value)
        elif isinstance(value, Mapping):
            merged[key] = _merge_mappings({}, value)
        else:
            merged[key] = value
    return merged
