from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import filebox, filebox_lifecycle
from .artifact_delivery import (
    ArtifactDeliveryService,
    DeliveryIntent,
)
from .filebox import FileBoxEntry
from .filebox_retention import (
    FileBoxPruneSummary,
    FileBoxRetentionPolicy,
    prune_filebox_root,
)


@dataclass(frozen=True)
class DurableFileRecord:
    name: str
    box: str
    path: Path
    provenance: str
    size: int | None
    mime_type: str
    checksum_sha256: str | None
    modified_at: str | None
    archived: bool


@dataclass(frozen=True)
class AppArtifactStorageRecord:
    path: Path
    filename: str
    size: int
    mime_type: str
    checksum_sha256: str
    provenance: dict[str, Any]


class ArtifactFileBoxStorage:
    """Shared storage boundary for artifact files, FileBox entries, and delivery intents."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root)
        self._delivery: ArtifactDeliveryService | None = None

    @property
    def delivery(self) -> ArtifactDeliveryService:
        if self._delivery is None:
            self._delivery = ArtifactDeliveryService(self.repo_root)
        return self._delivery

    def sanitize_filename(self, filename: str) -> str:
        return filebox.sanitize_filename(filename)

    def durable_file_record(
        self,
        path: Path,
        *,
        box: str,
        provenance: str,
        archived: bool = False,
        modified_at: str | None = None,
    ) -> DurableFileRecord:
        stat_result = path.stat()
        return DurableFileRecord(
            name=path.name,
            box=box,
            path=path,
            provenance=provenance,
            size=stat_result.st_size,
            mime_type=guess_mime_type(path.name),
            checksum_sha256=sha256_file(path),
            modified_at=modified_at,
            archived=archived,
        )

    def app_artifact_record(
        self,
        path: Path,
        *,
        provenance: dict[str, Any],
    ) -> AppArtifactStorageRecord:
        safe_name = self.sanitize_filename(path.name)
        stat_result = path.stat()
        return AppArtifactStorageRecord(
            path=path,
            filename=safe_name,
            size=stat_result.st_size,
            mime_type=guess_mime_type(safe_name),
            checksum_sha256=sha256_file(path),
            provenance=dict(provenance),
        )

    def list_filebox(self) -> dict[str, list[FileBoxEntry]]:
        return filebox.list_filebox(self.repo_root)

    def save_filebox_file(self, box: str, filename: str, data: bytes) -> Path:
        return filebox.save_file(self.repo_root, box, filename, data)

    def open_filebox_file(self, box: str, filename: str):
        return filebox.open_file(self.repo_root, box, filename)

    def delete_filebox_file(self, box: str, filename: str) -> bool:
        return filebox.delete_file(self.repo_root, box, filename)

    def consume_inbox_file(self, filename: str) -> FileBoxEntry:
        return filebox_lifecycle.consume_inbox_file(self.repo_root, filename)

    def dismiss_inbox_file(self, filename: str) -> FileBoxEntry:
        return filebox_lifecycle.dismiss_inbox_file(self.repo_root, filename)

    def restore_inbox_file(self, filename: str) -> FileBoxEntry:
        return filebox_lifecycle.unconsume_inbox_file(self.repo_root, filename)

    def list_archived_inbox_files(self) -> list[FileBoxEntry]:
        return filebox_lifecycle.list_consumed_files(self.repo_root)

    def prune_filebox(
        self,
        *,
        policy: FileBoxRetentionPolicy,
        scope: str = "both",
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> FileBoxPruneSummary:
        return prune_filebox_root(
            self.repo_root,
            policy=policy,
            scope=scope,
            dry_run=dry_run,
            now=now,
        )

    def enqueue_delivery_file(
        self,
        path: Path,
        *,
        target_surface: str,
        target_conversation_key: str,
        workspace_scope: str | None = None,
        origin_root: Path | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DeliveryIntent:
        return self.delivery.enqueue_file(
            path,
            target_surface=target_surface,
            target_conversation_key=target_conversation_key,
            workspace_scope=workspace_scope,
            origin_root=origin_root,
            metadata=metadata,
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def guess_mime_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    if suffix == ".json":
        return "application/json"
    guessed, _encoding = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


__all__ = [
    "AppArtifactStorageRecord",
    "ArtifactFileBoxStorage",
    "DurableFileRecord",
    "guess_mime_type",
    "sha256_file",
]
