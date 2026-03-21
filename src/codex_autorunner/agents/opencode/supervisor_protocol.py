from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class OpenCodeHarnessSupervisorProtocol(Protocol):
    async def get_client(self, workspace_root: Path) -> Any: ...

    async def session_stall_timeout_seconds_for_workspace(
        self, workspace_root: Path
    ) -> Optional[float]: ...
