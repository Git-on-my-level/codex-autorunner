from typing import Any, Optional


def coerce_int(
    value: Any, default: Optional[int] = None, *, reject_bool: bool = True
) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default if reject_bool else int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (OverflowError, ValueError):
            return default
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return int(float(value))
        except (ValueError, OverflowError):
            pass
    return default
