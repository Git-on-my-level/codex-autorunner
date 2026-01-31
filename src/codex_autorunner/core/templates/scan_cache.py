from __future__ import annotations

import dataclasses
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from ..locks import FileLock
from ..state_roots import resolve_hub_templates_root
from ..utils import atomic_write


@dataclasses.dataclass(frozen=True)
class TemplateScanRecord:
    blob_sha: str
    repo_id: str
    path: str
    ref: str
    commit_sha: str
    trusted: bool
    decision: str
    severity: str
    reason: str
    evidence: Optional[list[str]]
    scanned_at: str
    scanner: Optional[dict[str, str]]

    def to_dict(self, *, include_evidence: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "blob_sha": self.blob_sha,
            "repo_id": self.repo_id,
            "path": self.path,
            "ref": self.ref,
            "commit_sha": self.commit_sha,
            "trusted": self.trusted,
            "decision": self.decision,
            "severity": self.severity,
            "reason": self.reason,
            "scanned_at": self.scanned_at,
        }
        if include_evidence and self.evidence:
            payload["evidence"] = list(self.evidence)
        if self.scanner:
            payload["scanner"] = dict(self.scanner)
        return payload

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "TemplateScanRecord":
        return TemplateScanRecord(
            blob_sha=str(payload.get("blob_sha", "")),
            repo_id=str(payload.get("repo_id", "")),
            path=str(payload.get("path", "")),
            ref=str(payload.get("ref", "")),
            commit_sha=str(payload.get("commit_sha", "")),
            trusted=bool(payload.get("trusted", False)),
            decision=str(payload.get("decision", "")),
            severity=str(payload.get("severity", "")),
            reason=str(payload.get("reason", "")),
            evidence=_coerce_evidence(payload.get("evidence")),
            scanned_at=str(payload.get("scanned_at", "")),
            scanner=_coerce_scanner(payload.get("scanner")),
        )


def _coerce_evidence(value: Any) -> Optional[list[str]]:
    if not value:
        return None
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _coerce_scanner(value: Any) -> Optional[dict[str, str]]:
    if not value or not isinstance(value, dict):
        return None
    return {str(key): str(val) for key, val in value.items()}


def _scan_root(hub_root: Path) -> Path:
    return resolve_hub_templates_root(hub_root) / "scans"


def scan_record_path(hub_root: Path, blob_sha: str) -> Path:
    return _scan_root(hub_root) / f"{blob_sha}.json"


def scan_lock_path(hub_root: Path, blob_sha: str) -> Path:
    return _scan_root(hub_root) / "locks" / f"{blob_sha}.lock"


def get_scan_record(hub_root: Path, blob_sha: str) -> Optional[TemplateScanRecord]:
    path = scan_record_path(hub_root, blob_sha)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        return None
    return TemplateScanRecord.from_dict(payload)


def write_scan_record(record: TemplateScanRecord, hub_root: Path) -> None:
    path = scan_record_path(hub_root, record.blob_sha)
    payload = record.to_dict(include_evidence=False)
    if record.evidence:
        payload["evidence_redacted"] = True
    atomic_write(path, json.dumps(payload, indent=2) + "\n")


@contextmanager
def scan_lock(hub_root: Path, blob_sha: str) -> Iterator[None]:
    path = scan_lock_path(hub_root, blob_sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(path)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
