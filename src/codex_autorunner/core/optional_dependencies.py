from __future__ import annotations

import importlib.util
from typing import Optional, Sequence, Tuple, Union

from .config import ConfigError

OptionalDependency = Tuple[Union[str, Sequence[str]], str]


def missing_optional_dependencies(
    deps: Sequence[OptionalDependency],
) -> list[str]:
    missing: list[str] = []
    for module_names, display_name in deps:
        candidates = (
            [module_names] if isinstance(module_names, str) else list(module_names)
        )
        if not any(importlib.util.find_spec(name) is not None for name in candidates):
            missing.append(display_name)
    return missing


def require_optional_dependencies(
    *,
    feature: str,
    deps: Sequence[OptionalDependency],
    extra: Optional[str] = None,
    hint: Optional[str] = None,
) -> None:
    missing = missing_optional_dependencies(deps)
    if not missing:
        return

    extra_name = extra or feature
    deps_list = ", ".join(missing)
    message = (
        f"{feature} requires optional dependencies ({deps_list}). "
        f"Install with `pip install codex-autorunner[{extra_name}]` "
        f"(or `pip install -e .[{extra_name}]` for local dev)."
    )
    if hint:
        message = f"{message} {hint}"
    raise ConfigError(message)
