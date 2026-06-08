"""Compatibility wrapper for the update worker CLI."""

from __future__ import annotations

from typing import Any

from .update.runner import _build_logger, main

__all__ = ("_build_logger", "_system_update_worker", "main")


def _system_update_worker(**kwargs: Any) -> Any:
    from .update._facade import _system_update_worker as facade_worker

    return facade_worker(**kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
