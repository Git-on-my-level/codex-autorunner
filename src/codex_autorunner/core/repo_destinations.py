from __future__ import annotations

from typing import Mapping

from ..manifest import ManifestRepo
from .destinations import (
    default_local_destination as _default_local_destination,
)
from .destinations import (
    resolve_effective_repo_destination as _resolve_effective_repo_destination,
)


def default_local_destination() -> dict[str, object]:
    return _default_local_destination()


def resolve_effective_repo_destination(
    repo: ManifestRepo,
    repos_by_id: Mapping[str, ManifestRepo],
) -> dict[str, object]:
    resolution = _resolve_effective_repo_destination(repo, repos_by_id)
    return resolution.to_dict()
