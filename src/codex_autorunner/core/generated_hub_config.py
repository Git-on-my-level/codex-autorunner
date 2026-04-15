import logging
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import yaml

from .config_contract import CONFIG_VERSION
from .config_layering import (
    GENERATED_CONFIG_HEADER,
    GENERATED_HUB_CONFIG_PRESERVE_KEYS,
    PMA_DEFAULT_MAX_TEXT_CHARS,
    PMA_LEGACY_GENERATED_MAX_TEXT_CHARS,
    _clone_config_value,
    _load_yaml_dict,
    _mapping_has_nested_key,
    _merge_defaults,
    _root_explicitly_sets_pma_max_text_chars,
    resolve_hub_config_data,
)
from .utils import atomic_write

logger = logging.getLogger("codex_autorunner.core.generated_hub_config")


def _sparsify_generated_config_mapping(
    explicit: Mapping[str, Any],
    inherited_defaults: Mapping[str, Any],
    *,
    preserve_keys: tuple[str, ...] = (),
) -> Dict[str, Any]:
    sparse: Dict[str, Any] = {}
    for key, value in explicit.items():
        if key in preserve_keys:
            sparse[key] = _clone_config_value(value)
            continue
        if key not in inherited_defaults:
            sparse[key] = _clone_config_value(value)
            continue
        baseline_value = inherited_defaults[key]
        if isinstance(value, dict) and isinstance(baseline_value, dict):
            nested = _sparsify_generated_config_mapping(value, baseline_value)
            if nested:
                sparse[key] = nested
            continue
        if value != baseline_value:
            sparse[key] = _clone_config_value(value)
    return sparse


def build_generated_hub_config(
    root: Path, overrides: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Render generated hub config as sparse local state over inherited defaults."""
    inherited_defaults = resolve_hub_config_data(root)
    explicit = overrides or {}
    sparse = _sparsify_generated_config_mapping(
        explicit,
        inherited_defaults,
        preserve_keys=GENERATED_HUB_CONFIG_PRESERVE_KEYS,
    )

    rendered: Dict[str, Any] = {
        "version": explicit.get(
            "version", inherited_defaults.get("version", CONFIG_VERSION)
        ),
        "mode": explicit.get("mode", "hub"),
    }
    for key, value in sparse.items():
        if key in GENERATED_HUB_CONFIG_PRESERVE_KEYS:
            continue
        rendered[key] = value
    return rendered


def render_hub_config_yaml(
    config_path: Path,
    data: Dict[str, Any],
    *,
    generated: bool,
) -> str:
    payload = (
        build_generated_hub_config(config_path.parent.parent.resolve(), data)
        if generated
        else data
    )
    rendered = yaml.safe_dump(payload, sort_keys=False)
    if generated:
        return GENERATED_CONFIG_HEADER + rendered
    return rendered


def save_hub_config_data(
    config_path: Path,
    data: Dict[str, Any],
    *,
    generated: bool,
) -> None:
    atomic_write(
        config_path,
        render_hub_config_yaml(config_path, data, generated=generated),
    )


def normalize_generated_hub_config(config_path: Path) -> Dict[str, Any]:
    """Collapse generated hub config to sparse explicit state."""
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError:
        return _load_yaml_dict(config_path)
    if not raw_text.startswith(GENERATED_CONFIG_HEADER):
        return _load_yaml_dict(config_path)

    data = _load_yaml_dict(config_path)
    root = config_path.parent.parent.resolve()
    pma = data.get("pma")
    if (
        not _root_explicitly_sets_pma_max_text_chars(root)
        and _mapping_has_nested_key(data, "pma", "max_text_chars")
        and isinstance(pma, dict)
        and pma.get("max_text_chars") == PMA_LEGACY_GENERATED_MAX_TEXT_CHARS
    ):
        data = _merge_defaults(
            data,
            {"pma": {"max_text_chars": PMA_DEFAULT_MAX_TEXT_CHARS}},
        )

    rendered = render_hub_config_yaml(config_path, data, generated=True)
    if raw_text != rendered:
        try:
            atomic_write(config_path, rendered)
        except OSError:
            logger.warning(
                "Failed to persist generated hub config normalization for %s",
                config_path,
                exc_info=True,
            )
    return build_generated_hub_config(root, data)
