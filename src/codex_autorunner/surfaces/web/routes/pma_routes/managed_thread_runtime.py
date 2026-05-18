from __future__ import annotations

import sys
from typing import Any

from ...services.pma import managed_thread_runtime as _service_module

build_managed_thread_runtime_routes: Any = (
    _service_module.build_managed_thread_runtime_routes
)

sys.modules[__name__] = _service_module
