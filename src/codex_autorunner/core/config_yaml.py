import json
from pathlib import Path
from typing import Any, Dict, cast

import yaml

from .config_contract import ConfigError


def _merge_defaults(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    merged = cast(Dict[str, Any], json.loads(json.dumps(base)))
    for key, value in overrides.items():
        if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def _clone_config_value(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _load_yaml_dict(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Failed to read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file must be a mapping: {path}")
    return data


def _mapping_has_nested_key(mapping: object, *keys: str) -> bool:
    current = mapping
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True
