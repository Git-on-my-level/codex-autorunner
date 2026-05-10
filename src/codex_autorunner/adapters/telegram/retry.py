from __future__ import annotations

import re
from typing import Optional

import httpx


def _extract_retry_after_seconds(exc: Exception) -> Optional[int]:
    current: Optional[BaseException] = exc
    while current is not None:
        if isinstance(current, httpx.HTTPStatusError):
            header = current.response.headers.get("Retry-After")
            if header and header.isdigit():
                return int(header)
            try:
                payload = current.response.json()
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                parameters = payload.get("parameters")
                if isinstance(parameters, dict):
                    retry_after = parameters.get("retry_after")
                    if isinstance(retry_after, int):
                        return retry_after
            message = (
                str(payload.get("description")) if isinstance(payload, dict) else ""
            )
            match = re.search(r"retry after (\d+)", message.lower())
            if match:
                return int(match.group(1))
        message = str(current)
        match = re.search(r"retry after (\d+)", message.lower())
        if match:
            return int(match.group(1))
        current = current.__cause__ or current.__context__
    return None
