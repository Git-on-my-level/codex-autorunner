"""
Compatibility normalization helpers for web request schemas.

These helpers validate and normalize filter/payload inputs for PMA automation
request models. They exist separately from schemas.py so that the main schema
module stays focused on public request/response model definitions rather than
growing normalization logic.
"""

from __future__ import annotations

from typing import Any

from ...core.text_utils import _normalize_text


def validate_supported_payload_keys(
    data: dict[str, Any],
    *,
    supported_keys: set[str] | frozenset[str],
    label: str,
) -> None:
    unknown_keys = sorted(
        str(key)
        for key in data.keys()
        if isinstance(key, str) and key not in supported_keys
    )
    if unknown_keys:
        raise ValueError(
            f"Unsupported {label} keys: "
            + ", ".join(unknown_keys)
            + ". Valid keys: "
            + ", ".join(sorted(supported_keys))
        )


def normalize_filter_payload(
    *,
    raw_filter: Any,
    supported_keys: set[str] | frozenset[str],
    key_aliases: dict[str, str],
    label: str,
) -> dict[str, str]:
    if raw_filter is None:
        return {}
    if not isinstance(raw_filter, dict):
        raise ValueError(f"{label} must be an object")

    normalized_filter: dict[str, str] = {}
    unknown_filter_keys: list[str] = []
    for raw_key, raw_value in raw_filter.items():
        if not isinstance(raw_key, str):
            unknown_filter_keys.append(str(raw_key))
            continue
        normalized_key = key_aliases.get(raw_key, raw_key)
        if normalized_key not in supported_keys:
            unknown_filter_keys.append(raw_key)
            continue
        normalized_value = _normalize_text(raw_value)
        if normalized_value is None:
            raise ValueError(f"{label}.{normalized_key} must be a non-empty string")
        existing = normalized_filter.get(normalized_key)
        if existing is not None and existing != normalized_value:
            raise ValueError(
                f"Conflicting {label} values for {normalized_key}: "
                f"{existing!r} vs {normalized_value!r}"
            )
        normalized_filter[normalized_key] = normalized_value

    if unknown_filter_keys:
        raise ValueError(
            f"Unsupported {label} keys: "
            + ", ".join(sorted(unknown_filter_keys))
            + ". Valid keys: "
            + ", ".join(sorted(supported_keys))
        )
    return normalized_filter


def merge_normalized_filter(
    data: dict[str, Any],
    normalized_filter: dict[str, str],
    *,
    label: str,
) -> dict[str, Any]:
    for key, value in normalized_filter.items():
        existing = _normalize_text(data.get(key))
        if existing is not None and existing != value:
            raise ValueError(
                f"Conflicting values for {key}: top-level={existing!r}, "
                f"{label}={value!r}"
            )
        if existing is None:
            data[key] = value
    return data
