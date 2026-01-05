import hashlib
from pathlib import Path

from .utils import canonicalize_path


WORKSPACE_ID_HEX_LEN = 12


def canonical_workspace_root(path: Path) -> Path:
    return canonicalize_path(path)


def workspace_id_for_path(path: Path) -> str:
    canonical = canonical_workspace_root(path)
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
    return digest[:WORKSPACE_ID_HEX_LEN]
