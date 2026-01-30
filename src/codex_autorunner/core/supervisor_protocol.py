from __future__ import annotations

from pathlib import Path
from typing import Any


class AppServerSupervisorProtocol:
    async def get_client(self, workspace_root: Path) -> Any:
        raise NotImplementedError

    async def close_all(self) -> None:
        raise NotImplementedError

    async def prune_idle(self) -> int:
        raise NotImplementedError
