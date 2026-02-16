"""Self-description contract primitives for `car describe`."""

from .contract import (
    CONFIG_PRECEDENCE,
    NON_SECRET_ENV_KNOBS,
    SCHEMA_FILENAME,
    SCHEMA_ID,
    SCHEMA_VERSION,
    default_runtime_schema_path,
)

__all__ = [
    "CONFIG_PRECEDENCE",
    "NON_SECRET_ENV_KNOBS",
    "SCHEMA_FILENAME",
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "default_runtime_schema_path",
]
