from __future__ import annotations

from typing import Any, Optional

from ...core.orchestration.opencode_event_fields import (
    extract_message_id as _shared_extract_message_id,
)
from ...core.orchestration.opencode_event_fields import (
    extract_message_role as _shared_extract_message_role,
)
from ...core.orchestration.opencode_event_fields import (
    extract_part_id as _shared_extract_part_id,
)
from ...core.orchestration.opencode_event_fields import (
    extract_part_message_id as _shared_extract_part_message_id,
)
from ...core.orchestration.opencode_event_fields import (
    extract_part_type as _shared_extract_part_type,
)


def _as_params(payload: Any) -> dict[str, Any]:
    return payload if isinstance(payload, dict) else {}


def extract_message_id(payload: Any) -> Optional[str]:
    return _shared_extract_message_id(_as_params(payload))


def extract_message_role(payload: Any) -> Optional[str]:
    return _shared_extract_message_role(_as_params(payload))


def extract_part_message_id(payload: Any) -> Optional[str]:
    return _shared_extract_part_message_id(_as_params(payload))


def extract_part_id(payload: Any) -> Optional[str]:
    return _shared_extract_part_id(_as_params(payload))


def extract_part_type(
    payload: Any, *, part_types: Optional[dict[str, str]] = None
) -> Optional[str]:
    part_type = _shared_extract_part_type(_as_params(payload), part_types=part_types)
    return part_type or None
