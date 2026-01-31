"""Core type definitions.

This module provides type definitions that were previously in Engine.
"""

from typing import Any, Callable, Optional

# Type aliases for factory functions
BackendFactory = Callable[[str, Any, Optional[Callable[[dict[str, Any]], Any]]], Any]
AppServerSupervisorFactory = Callable[
    [str, Optional[Callable[[dict[str, Any]], Any]]], Any
]


__all__ = [
    "BackendFactory",
    "AppServerSupervisorFactory",
]
