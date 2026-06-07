from __future__ import annotations

import os
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..config_parsers import normalize_base_path
from ..config_validation import is_loopback_host


def resolve_public_hub_base_url(config: Any) -> str | None:
    explicit = _explicit_public_base_url(config)
    if explicit:
        return explicit
    derived = _public_base_url_from_allowed_origins(config)
    if derived:
        return derived
    return None


def resolve_user_facing_preview_url(config: Any, preview_url: str) -> str:
    if preview_url.startswith(("http://", "https://")):
        return preview_url
    public_base = resolve_public_hub_base_url(config)
    if public_base:
        return urljoin(f"{public_base.rstrip('/')}/", preview_url.lstrip("/"))
    return _base_path_relative_url(config, preview_url)


def _explicit_public_base_url(config: Any) -> str | None:
    env_value = os.environ.get("CAR_PREVIEW_PUBLIC_BASE_URL")
    if env_value and env_value.strip():
        return env_value.strip().rstrip("/")
    preview_cfg = _preview_services_config(config)
    for key in ("public_base_url", "publicBaseUrl", "base_url", "baseUrl"):
        value = preview_cfg.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip("/")
    for attr in ("public_base_url", "server_public_url", "external_url"):
        value = getattr(config, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip("/")
    return None


def _public_base_url_from_allowed_origins(config: Any) -> str | None:
    origins = _server_allowed_origins(config)
    candidates = [
        candidate
        for origin in origins
        if (candidate := _origin_base(origin)) is not None
    ]
    https_candidates = [
        candidate for candidate in candidates if candidate.startswith("https://")
    ]
    selected: str | None = None
    if len(https_candidates) == 1:
        selected = https_candidates[0]
    elif len(candidates) == 1:
        selected = candidates[0]
    if selected is None:
        return None
    base_path = _server_base_path(config)
    return f"{selected}{base_path}".rstrip("/")


def _origin_base(origin: str) -> str | None:
    value = origin.strip()
    if not value:
        return None
    split = urlsplit(value)
    if split.scheme not in {"http", "https"} or not split.netloc:
        return None
    hostname = split.hostname
    if not hostname or is_loopback_host(hostname):
        return None
    return urlunsplit((split.scheme, split.netloc, "", "", "")).rstrip("/")


def _base_path_relative_url(config: Any, preview_url: str) -> str:
    if not preview_url.startswith("/"):
        return preview_url
    base_path = _server_base_path(config)
    if (
        not base_path
        or preview_url == base_path
        or preview_url.startswith(f"{base_path}/")
    ):
        return preview_url
    return f"{base_path}{preview_url}"


def _server_base_path(config: Any) -> str:
    value = getattr(config, "server_base_path", None)
    if not isinstance(value, str):
        server_cfg = _server_config(config)
        value = server_cfg.get("base_path") if isinstance(server_cfg, dict) else ""
    return normalize_base_path(value or "")


def _server_allowed_origins(config: Any) -> list[str]:
    values = getattr(config, "server_allowed_origins", None)
    if not isinstance(values, list):
        server_cfg = _server_config(config)
        values = (
            server_cfg.get("allowed_origins") if isinstance(server_cfg, dict) else []
        )
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str)]


def _preview_services_config(config: Any) -> dict[str, Any]:
    preview_cfg = getattr(config, "preview_services", None)
    if not isinstance(preview_cfg, dict):
        raw_cfg = getattr(config, "raw", None)
        preview_cfg = (
            raw_cfg.get("preview_services") if isinstance(raw_cfg, dict) else None
        )
    return preview_cfg if isinstance(preview_cfg, dict) else {}


def _server_config(config: Any) -> dict[str, Any]:
    raw_cfg = getattr(config, "raw", None)
    server_cfg = raw_cfg.get("server") if isinstance(raw_cfg, dict) else None
    return server_cfg if isinstance(server_cfg, dict) else {}
