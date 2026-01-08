from __future__ import annotations

import importlib.util
from typing import Optional, Sequence, Tuple

from .config import ConfigError

OptionalDependency = Tuple[str, str]


def missing_optional_dependencies(
    deps: Sequence[OptionalDependency],
) -> list[str]:
    missing: list[str] = []
    for module_name, display_name in deps:
        if importlib.util.find_spec(module_name) is None:
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
