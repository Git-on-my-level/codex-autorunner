from __future__ import annotations

from typing import TYPE_CHECKING

from .....core import hub_repo_projection as _core_hub_repo_projection
from .....core.hub_repo_projection import HubRepoProjectionService

if TYPE_CHECKING:
    from ...app_state import HubAppContext
    from .mount_manager import HubMountManager

_REPO_RUNTIME_PROJECTION_MAX_AGE_SECONDS = (
    _core_hub_repo_projection._REPO_RUNTIME_PROJECTION_MAX_AGE_SECONDS
)


class HubRepoEnricher(HubRepoProjectionService):
    def __init__(self, context: HubAppContext, mount_manager: HubMountManager) -> None:
        self._mount_manager = mount_manager
        super().__init__(context, repo_payload_decorator=mount_manager.add_mount_info)

    def _repo_state_payload(self, snapshot, *, stale_threshold_seconds):
        _core_hub_repo_projection._REPO_RUNTIME_PROJECTION_MAX_AGE_SECONDS = (
            _REPO_RUNTIME_PROJECTION_MAX_AGE_SECONDS
        )
        return super()._repo_state_payload(
            snapshot,
            stale_threshold_seconds=stale_threshold_seconds,
        )


__all__ = [
    "HubRepoEnricher",
    "_REPO_RUNTIME_PROJECTION_MAX_AGE_SECONDS",
]
