from __future__ import annotations

import shutil
from pathlib import Path


def copy_path(src: Path, dst: Path) -> None:
    """Copy a file or directory to a destination.

    If src is a directory, copy it recursively using copytree.
    If src is a file, copy it using copy2 after ensuring parent directory exists.

    Args:
        src: Source path to copy from
        dst: Destination path to copy to

    Raises:
        OSError: If the copy operation fails
    """
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
