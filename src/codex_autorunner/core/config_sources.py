import json
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import yaml

from .config_contract import ConfigError
from .config_defaults import DEFAULT_HUB_CONFIG, DEFAULT_REPO_CONFIG, REPO_SHARED_KEYS
from .config_yaml import _load_yaml_dict, _mapping_has_nested_key, _merge_defaults
from .utils import atomic_write

CONFIG_FILENAME = ".codex-autorunner/config.yml"
ROOT_CONFIG_FILENAME = "codex-autorunner.yml"
ROOT_OVERRIDE_FILENAME = "codex-autorunner.override.yml"
REPO_OVERRIDE_FILENAME = ".codex-autorunner/repo.override.yml"


def _root_explicitly_sets_pma_max_text_chars(root: Path) -> bool:
    for candidate in (root / ROOT_CONFIG_FILENAME, root / ROOT_OVERRIDE_FILENAME):
        if not candidate.exists():
            continue
        if _mapping_has_nested_key(_load_yaml_dict(candidate), "pma", "max_text_chars"):
            return True
    return False


def _load_root_config(root: Path) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    base_path = root / ROOT_CONFIG_FILENAME
    base = _load_yaml_dict(base_path)
    if base:
        merged = _merge_defaults(merged, base)
    override_path = root / ROOT_OVERRIDE_FILENAME
    try:
        override = _load_yaml_dict(override_path)
    except ConfigError as exc:
        raise ConfigError(
            f"Invalid override config {override_path}; fix or delete it: {exc}"
        ) from exc
    if override:
        merged = _merge_defaults(merged, override)
    return merged


def load_root_defaults(root: Path) -> Dict[str, Any]:
    """Load hub defaults from the root config + override file."""
    return _load_root_config(root)


def resolve_hub_config_data(
    root: Path, overrides: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    merged = _merge_defaults(DEFAULT_HUB_CONFIG, load_root_defaults(root))
    if overrides:
        merged = _merge_defaults(merged, overrides)
    return merged


def repo_shared_overrides_from_hub(hub_data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: hub_data[key] for key in REPO_SHARED_KEYS if key in hub_data}


def _load_repo_override(repo_root: Path) -> Dict[str, Any]:
    override_path = repo_root / REPO_OVERRIDE_FILENAME
    data = _load_yaml_dict(override_path)
    if not data:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Repo override file must be a mapping: {override_path}")
    if "mode" in data or "version" in data:
        raise ConfigError(
            f"{override_path} must not set mode or version; those are hub-managed."
        )
    return data


def derive_repo_config_data(
    hub_data: Dict[str, Any], repo_root: Path
) -> Dict[str, Any]:
    repo_defaults = hub_data.get("repo_defaults") or {}
    if not isinstance(repo_defaults, dict):
        raise ConfigError("hub.repo_defaults must be a mapping if provided")
    merged = cast(Dict[str, Any], json.loads(json.dumps(DEFAULT_REPO_CONFIG)))
    if repo_defaults:
        merged = _merge_defaults(merged, repo_defaults)
    shared_overrides = repo_shared_overrides_from_hub(hub_data)
    if shared_overrides:
        merged = _merge_defaults(merged, shared_overrides)
    repo_overrides = _load_repo_override(repo_root)
    if repo_overrides:
        merged = _merge_defaults(merged, repo_overrides)
    return merged


def find_nearest_hub_config_path(start: Path) -> Optional[Path]:
    start = start.resolve()
    search_dir = start if start.is_dir() else start.parent
    for current in [search_dir] + list(search_dir.parents):
        candidate = current / CONFIG_FILENAME
        if not candidate.exists():
            continue
        data = _load_yaml_dict(candidate)
        if data.get("mode") in (None, "hub"):
            return candidate
    return None


def update_override_templates(repo_root: Path, repos: List[Dict[str, Any]]) -> None:
    override_path = repo_root / ROOT_OVERRIDE_FILENAME
    data = _load_yaml_dict(override_path)
    templates = data.get("templates")
    if templates is None or not isinstance(templates, dict):
        templates = {}
        data["templates"] = templates
    templates["repos"] = list(repos or [])
    rendered = yaml.safe_dump(data, sort_keys=False).rstrip() + "\n"
    atomic_write(override_path, rendered)
