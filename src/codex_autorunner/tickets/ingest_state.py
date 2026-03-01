from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..core.state_roots import resolve_repo_state_root
from ..core.utils import atomic_write

INGEST_STATE_SCHEMA_VERSION = 1
INGEST_STATE_FILENAME = "ingest_state.json"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_valid_iso_timestamp(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def ingest_state_path(repo_root: Path) -> Path:
    return resolve_repo_state_root(repo_root) / "tickets" / INGEST_STATE_FILENAME


def normalize_ingest_receipt(payload: Any) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != INGEST_STATE_SCHEMA_VERSION:
        return None
    if payload.get("ingested") is not True:
        return None

    ingested_at = payload.get("ingested_at")
    if not isinstance(ingested_at, str) or not _is_valid_iso_timestamp(ingested_at):
        return None

    source = payload.get("source")
    if not isinstance(source, str) or not source.strip():
        return None

    details = payload.get("details")
    if details is not None and not isinstance(details, dict):
        return None

    normalized: dict[str, Any] = {
        "schema_version": INGEST_STATE_SCHEMA_VERSION,
        "ingested": True,
        "ingested_at": ingested_at.strip(),
        "source": source.strip(),
    }
    if isinstance(details, dict):
        normalized["details"] = details
    return normalized


def read_ingest_receipt(repo_root: Path) -> Optional[dict[str, Any]]:
    path = ingest_state_path(repo_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return normalize_ingest_receipt(payload)


def write_ingest_receipt(
    repo_root: Path,
    *,
    source: str,
    details: Optional[dict[str, Any]] = None,
    ingested_at: Optional[str] = None,
) -> dict[str, Any]:
    source_text = str(source).strip()
    if not source_text:
        raise ValueError("Ingest receipt source must be non-empty.")

    if ingested_at is None:
        timestamp = _iso_now()
    else:
        timestamp = str(ingested_at).strip()
        if not _is_valid_iso_timestamp(timestamp):
            raise ValueError("Ingest receipt ingested_at must be an ISO timestamp.")

    if details is not None and not isinstance(details, dict):
        raise ValueError("Ingest receipt details must be an object.")

    payload: dict[str, Any] = {
        "schema_version": INGEST_STATE_SCHEMA_VERSION,
        "ingested": True,
        "ingested_at": timestamp,
        "source": source_text,
    }
    if details is not None:
        payload["details"] = details

    atomic_write(ingest_state_path(repo_root), json.dumps(payload, indent=2) + "\n")
    return payload
