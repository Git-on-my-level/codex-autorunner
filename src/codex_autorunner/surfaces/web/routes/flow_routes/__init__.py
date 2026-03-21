from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import (
    definitions,
    history_artifacts,
    runtime_service,
    status_history_routes,
)
from .dependencies import FlowRouteDependencies, build_default_flow_route_dependencies

__all__ = [
    "FlowRouteDependencies",
    "FlowRoutesState",
    "build_default_flow_route_dependencies",
    "definitions",
    "history_artifacts",
    "runtime_service",
    "status_history_routes",
]


@dataclass
class FlowRoutesState:
    active_workers: Dict[
        str, Tuple[Optional[object], Optional[object], Optional[object]]
    ]
    controller_cache: Dict[tuple[Path, str], object]
    definition_cache: Dict[tuple[Path, str], object]
    lock: threading.Lock

    def __init__(self) -> None:
        self.active_workers = {}
        self.controller_cache = {}
        self.definition_cache = {}
        self.lock = threading.Lock()
