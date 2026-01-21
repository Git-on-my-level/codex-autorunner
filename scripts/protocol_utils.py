#!/usr/bin/env python3

"""Utility functions for protocol-related scripts."""

import logging
import os
from pathlib import Path


def validate_binary_path(binary_name: str, env_var: str) -> Path:
    """Validate binary path from environment or PATH.

    Args:
        binary_name: Name of the binary (for error messages and logging)
        env_var: Environment variable name that may contain the path

    Returns:
        Path to the validated binary

    Raises:
        RuntimeError: If validation fails
    """
    import shutil

    env_path = os.environ.get(env_var)
    if env_path:
        path = Path(env_path)
    else:
        path_str = shutil.which(binary_name)
        if not path_str:
            raise RuntimeError(
                f"{binary_name} binary not found. "
                f"Set {env_var} environment variable or install {binary_name}."
            )
        path = Path(path_str)

    if not path.is_absolute():
        raise RuntimeError(
            f"{binary_name} path must be absolute: {path}. "
            f"Set {env_var} to an absolute path."
        )

    suspicious_chars = [";", "|", "&", "$", "`", "(", ")", "<", ">", "\n", "\r"]
    path_str = str(path)
    if any(char in path_str for char in suspicious_chars):
        raise RuntimeError(f"{binary_name} path contains suspicious characters: {path}")

    if not path.exists():
        raise RuntimeError(f"{binary_name} binary not found: {path}")

    if not path.is_file():
        raise RuntimeError(f"{binary_name} path is not a file: {path}")

    if not os.access(path, os.X_OK):
        raise RuntimeError(f"{binary_name} binary is not executable: {path}")

    logging.info(f"Using {binary_name} binary: {path}")

    return path
