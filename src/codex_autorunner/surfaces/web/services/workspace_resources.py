"""Typed workspace resource contracts shared by web resource routes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Optional
from urllib.parse import quote

from starlette.datastructures import UploadFile

from ....contextspace.paths import (
    contextspace_dir,
    normalize_contextspace_doc_kind,
    read_contextspace_docs,
    serialize_contextspace_doc_catalog,
    write_contextspace_doc,
)
from ....core.artifact_delivery import (
    ArtifactDeliveryService,
    DeliveryState,
    serialize_delivery,
)
from ....core.artifact_filebox_storage import ArtifactFileBoxStorage
from ....core.filebox import BOXES, FileBoxEntry, ensure_structure, sanitize_filename
from ....tickets.spec_ingest import (
    SpecIngestTicketsError,
    ingest_workspace_spec_to_tickets,
)
from ..schemas import ArchiveTreeResponse
from .archive_resources import (
    iter_local_run_archives,
    iter_snapshots,
    list_tree,
    load_meta,
    normalize_archive_rel_path,
    resolve_local_run_root,
    resolve_snapshot_root,
    snapshot_summary,
)

logger = logging.getLogger(__name__)

DEFAULT_FILEBOX_MAX_UPLOAD_BYTES = 10_000_000
UPLOAD_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class WorkspaceResourceError(Exception):
    status_code: int
    detail: str


@dataclass(frozen=True)
class ArchiveFileResource:
    path: Path
    filename: str
    content: str | None = None


@dataclass(frozen=True)
class FileBoxDownloadResource:
    entry: FileBoxEntry
    handle: BinaryIO


@dataclass(frozen=True)
class FileBoxUrlScope:
    root_path: str = ""
    repo_id: str | None = None
    # True for hub-level (repo-less) "Hub workspace" threads, whose delivery
    # downloads are served from /hub/artifacts/... rather than the per-repo
    # /api/... routes (which are not mounted on the hub app).
    hub_workspace: bool = False


def _archive_error(exc: Exception) -> WorkspaceResourceError:
    if isinstance(exc, ValueError):
        return WorkspaceResourceError(400, str(exc))
    if isinstance(exc, FileNotFoundError):
        return WorkspaceResourceError(404, str(exc))
    if isinstance(exc, RuntimeError):
        return WorkspaceResourceError(409, str(exc))
    return WorkspaceResourceError(500, str(exc))


class ContextspaceResourceService:
    """Owns contextspace document, tree, and spec-ingest route contracts."""

    def payload(self, repo_root: Path) -> dict[str, Any]:
        docs = read_contextspace_docs(repo_root)
        return {
            "active_context": docs["active_context"],
            "decisions": docs["decisions"],
            "spec": docs["spec"],
            "kinds": serialize_contextspace_doc_catalog(),
        }

    def tree_payload(self, repo_root: Path) -> dict[str, Any]:
        base = contextspace_dir(repo_root)
        base.mkdir(parents=True, exist_ok=True)
        pinned_paths = {entry["path"] for entry in serialize_contextspace_doc_catalog()}

        def _serialize_node(path: Path) -> dict[str, Any]:
            rel_path = path.relative_to(base).as_posix()
            stat = path.stat()
            modified_at = datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat()
            if path.is_dir():
                children = [
                    _serialize_node(child)
                    for child in sorted(
                        path.iterdir(),
                        key=lambda child: (
                            child.is_file(),
                            child.name.lower(),
                        ),
                    )
                ]
                return {
                    "name": path.name,
                    "path": rel_path,
                    "type": "folder",
                    "modified_at": modified_at,
                    "size": None,
                    "is_pinned": False,
                    "children": children,
                }
            return {
                "name": path.name,
                "path": rel_path,
                "type": "file",
                "modified_at": modified_at,
                "size": stat.st_size,
                "is_pinned": rel_path in pinned_paths,
            }

        tree = [
            _serialize_node(child)
            for child in sorted(
                base.iterdir(),
                key=lambda child: (
                    child.is_file(),
                    0 if child.name in pinned_paths else 1,
                    child.name.lower(),
                ),
            )
        ]
        return {"tree": tree, "defaultPath": "active_context.md"}

    def write_document(
        self, repo_root: Path, kind: str, content: str
    ) -> dict[str, Any]:
        try:
            key = normalize_contextspace_doc_kind(kind)
        except ValueError as exc:
            raise WorkspaceResourceError(400, str(exc)) from exc
        write_contextspace_doc(repo_root, key, content)
        return self.payload(repo_root)

    def ingest_spec(self, repo_root: Path) -> dict[str, Any]:
        try:
            result = ingest_workspace_spec_to_tickets(repo_root)
        except SpecIngestTicketsError as exc:
            raise WorkspaceResourceError(400, str(exc)) from exc
        return {
            "status": "ok",
            "created": result.created,
            "first_ticket_path": result.first_ticket_path,
        }


class ArchiveResourceService:
    """Owns snapshot/local-run archive browsing and file resolution contracts."""

    def list_snapshots(self, repo_root: Path) -> dict[str, Any]:
        return {"snapshots": iter_snapshots(repo_root)}

    def list_local_runs(self, repo_root: Path) -> dict[str, Any]:
        return {"archives": iter_local_run_archives(repo_root)}

    def snapshot_detail(
        self,
        repo_root: Path,
        snapshot_id: str,
        worktree_repo_id: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            snapshot_root, worktree_id = resolve_snapshot_root(
                repo_root, snapshot_id, worktree_repo_id
            )
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise _archive_error(exc) from exc
        meta = load_meta(snapshot_root / "META.json")
        return {
            "snapshot": snapshot_summary(snapshot_root, worktree_id, meta),
            "meta": meta,
        }

    def snapshot_tree(
        self,
        repo_root: Path,
        snapshot_id: str,
        rel_path: str,
        worktree_repo_id: Optional[str] = None,
    ) -> ArchiveTreeResponse:
        try:
            snapshot_root, _ = resolve_snapshot_root(
                repo_root, snapshot_id, worktree_repo_id
            )
            return list_tree(snapshot_root, rel_path)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise _archive_error(exc) from exc

    def local_run_tree(
        self,
        repo_root: Path,
        run_id: str,
        rel_path: str,
    ) -> ArchiveTreeResponse:
        try:
            run_root = resolve_local_run_root(repo_root, run_id)
            return list_tree(run_root, rel_path)
        except (ValueError, FileNotFoundError) as exc:
            raise _archive_error(exc) from exc

    def snapshot_file(
        self,
        repo_root: Path,
        snapshot_id: str,
        rel_path: str,
        *,
        worktree_repo_id: Optional[str] = None,
        include_content: bool,
    ) -> ArchiveFileResource:
        try:
            snapshot_root, _ = resolve_snapshot_root(
                repo_root, snapshot_id, worktree_repo_id
            )
            return self._archive_file(
                snapshot_root, rel_path, include_content=include_content
            )
        except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
            raise self._file_error(exc) from exc

    def local_run_file(
        self,
        repo_root: Path,
        run_id: str,
        rel_path: str,
        *,
        include_content: bool,
    ) -> ArchiveFileResource:
        try:
            run_root = resolve_local_run_root(repo_root, run_id)
            return self._archive_file(
                run_root,
                rel_path,
                include_content=include_content,
                reject_root=True,
            )
        except (ValueError, FileNotFoundError, OSError) as exc:
            raise self._file_error(exc) from exc

    def _archive_file(
        self,
        root: Path,
        rel_path: str,
        *,
        include_content: bool,
        reject_root: bool = False,
    ) -> ArchiveFileResource:
        target, rel_posix = normalize_archive_rel_path(root, rel_path)
        if reject_root and not rel_posix:
            raise FileNotFoundError("file not found")
        if not target.exists() or not target.is_file():
            raise FileNotFoundError("file not found")
        content = (
            target.read_text(encoding="utf-8", errors="replace")
            if include_content
            else None
        )
        return ArchiveFileResource(path=target, filename=target.name, content=content)

    def _file_error(self, exc: Exception) -> WorkspaceResourceError:
        if isinstance(exc, OSError):
            return WorkspaceResourceError(500, str(exc))
        return _archive_error(exc)


class FileBoxResourceService:
    """Owns filebox listing/upload/download and artifact delivery contracts."""

    def list_filebox(
        self, repo_root: Path, *, url_scope: FileBoxUrlScope
    ) -> dict[str, Any]:
        ensure_structure(repo_root)
        entries = ArtifactFileBoxStorage(repo_root).list_filebox()
        return serialize_filebox_listing(entries, url_scope=url_scope)

    def list_single_box(
        self, repo_root: Path, box: str, *, url_scope: FileBoxUrlScope
    ) -> dict[str, Any]:
        self.validate_box(box)
        listing = self.list_filebox(repo_root, url_scope=url_scope)
        return {box: listing.get(box, [])}

    async def upload_files_to_box(
        self,
        *,
        repo_root: Path,
        box: str,
        form: Mapping[str, Any],
        max_upload_bytes: int,
    ) -> dict[str, Any]:
        self.validate_box(box)
        ensure_structure(repo_root)
        pending: list[tuple[str, bytes]] = []
        for filename, file in form.items():
            if not isinstance(file, UploadFile):
                continue
            try:
                sanitize_filename(filename)
                effective_filename = sanitize_filename(file.filename or filename)
            except ValueError as exc:
                raise WorkspaceResourceError(400, str(exc)) from exc
            try:
                data = await read_upload_limited(
                    file, max_upload_bytes=max_upload_bytes
                )
            except OSError as exc:
                logger.warning("Failed to read upload: %s", exc)
                continue
            except ValueError as exc:
                raise WorkspaceResourceError(413, str(exc)) from exc
            pending.append((effective_filename, data))

        saved = []
        storage = ArtifactFileBoxStorage(repo_root)
        for filename, data in pending:
            try:
                path = storage.save_filebox_file(box, filename, data)
                saved.append(path.name)
            except ValueError as exc:
                raise WorkspaceResourceError(400, str(exc)) from exc
        return {"status": "ok", "saved": saved}

    def open_download(
        self, repo_root: Path, box: str, filename: str
    ) -> FileBoxDownloadResource:
        self.validate_box(box)
        result = ArtifactFileBoxStorage(repo_root).open_filebox_file(box, filename)
        if result is None:
            raise WorkspaceResourceError(404, "File not found")
        entry, handle = result
        return FileBoxDownloadResource(entry=entry, handle=handle)

    def open_delivery_artifact(
        self, repo_root: Path, delivery_id: str
    ) -> FileBoxDownloadResource:
        result = ArtifactDeliveryService(repo_root).inspect_with_artifact(delivery_id)
        if result is None or result[1] is None:
            raise WorkspaceResourceError(404, "Delivery artifact not found")
        _intent, artifact_or_none = result
        artifact = artifact_or_none
        assert artifact is not None
        try:
            path = artifact.storage_path
            stat_result = path.stat()
            handle = path.open("rb")
        except FileNotFoundError as exc:
            raise WorkspaceResourceError(
                404, "Delivery artifact file not found"
            ) from exc
        except OSError as exc:
            raise WorkspaceResourceError(
                500, "Failed to open delivery artifact"
            ) from exc
        entry = FileBoxEntry(
            name=artifact.filename,
            box="outbox",
            size=stat_result.st_size,
            modified_at=artifact.updated_at,
            source="artifact_delivery",
            path=path,
        )
        return FileBoxDownloadResource(entry=entry, handle=handle)

    def delete_file(self, repo_root: Path, box: str, filename: str) -> dict[str, Any]:
        self.validate_box(box)
        try:
            removed = ArtifactFileBoxStorage(repo_root).delete_filebox_file(
                box, filename
            )
        except ValueError as exc:
            raise WorkspaceResourceError(400, str(exc)) from exc
        except OSError as exc:
            raise WorkspaceResourceError(500, "Failed to delete file") from exc
        if not removed:
            raise WorkspaceResourceError(404, "File not found")
        return {"status": "ok"}

    def delete_box(self, repo_root: Path, box: str) -> dict[str, Any]:
        self.validate_box(box)
        try:
            entries = ArtifactFileBoxStorage(repo_root).list_filebox()
        except ValueError as exc:
            raise WorkspaceResourceError(400, str(exc)) from exc
        except OSError as exc:
            raise WorkspaceResourceError(500, "Failed to list files") from exc

        deleted_files: list[str] = []
        for entry in entries.get(box, []):
            try:
                entry.path.unlink()
                deleted_files.append(entry.name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise WorkspaceResourceError(500, "Failed to delete files") from exc
        return {
            "status": "ok",
            "deleted": deleted_files,
            "deleted_count": len(deleted_files),
        }

    def list_artifact_deliveries(
        self,
        repo_root: Path,
        *,
        url_scope: FileBoxUrlScope,
        state: str | None = None,
        surface: str | None = None,
        conversation: str | None = None,
        repo_id: str | None = None,
    ) -> dict[str, Any]:
        states = normalize_delivery_states(state)
        service = ArtifactDeliveryService(repo_root)
        deliveries = service.list_deliveries(
            states=states,
            target_surface=surface,
            target_conversation_key=conversation,
        )
        payload = {
            "root": str(repo_root),
            "deliveries": [
                add_delivery_download_url(
                    serialize_delivery(
                        intent,
                        artifact=service.store.get_artifact(intent.artifact_id),
                    ),
                    url_scope=url_scope,
                )
                for intent in deliveries
            ],
        }
        if repo_id is not None:
            payload["repo_id"] = repo_id
        return payload

    def validate_box(self, box: str) -> None:
        if box not in BOXES:
            raise WorkspaceResourceError(400, "Invalid box")


def serialize_filebox_listing(
    entries: dict[str, list[FileBoxEntry]], *, url_scope: FileBoxUrlScope
) -> dict[str, list[dict[str, Any]]]:
    return {
        box: [serialize_filebox_entry(entry, url_scope=url_scope) for entry in files]
        for box, files in entries.items()
    }


def serialize_filebox_entry(
    entry: FileBoxEntry, *, url_scope: FileBoxUrlScope
) -> dict[str, Any]:
    root_path = url_scope.root_path or ""
    if url_scope.repo_id:
        download = (
            f"{root_path}/hub/filebox/{quote(url_scope.repo_id, safe='')}/"
            f"{entry.box}/{quote(entry.name, safe='')}"
        )
    else:
        download = f"{root_path}/api/filebox/{entry.box}/{quote(entry.name, safe='')}"
    payload: dict[str, Any] = {
        "name": entry.name,
        "box": entry.box,
        "size": entry.size,
        "modified_at": entry.modified_at,
        "source": entry.source,
        "url": download,
    }
    if url_scope.repo_id is not None:
        payload["repo_id"] = url_scope.repo_id
    return payload


def add_delivery_download_url(
    delivery: dict[str, Any], *, url_scope: FileBoxUrlScope
) -> dict[str, Any]:
    delivery_id = delivery.get("delivery_id")
    artifact = delivery.get("artifact")
    if not isinstance(delivery_id, str) or not delivery_id:
        return delivery
    if not isinstance(artifact, dict) or not artifact:
        return delivery
    root_path = url_scope.root_path or ""
    if url_scope.repo_id:
        download = (
            f"{root_path}/hub/filebox/{quote(url_scope.repo_id, safe='')}/"
            f"artifacts/deliveries/{quote(delivery_id, safe='')}/download"
        )
    elif url_scope.hub_workspace:
        download = (
            f"{root_path}/hub/artifacts/deliveries/"
            f"{quote(delivery_id, safe='')}/download"
        )
    else:
        download = (
            f"{root_path}/api/artifacts/deliveries/"
            f"{quote(delivery_id, safe='')}/download"
        )
    artifact["url"] = download
    artifact["href"] = download
    delivery["download_url"] = download
    return delivery


def normalize_delivery_states(state: str | None) -> tuple[DeliveryState, ...] | None:
    values = tuple(item.strip() for item in (state or "").split(",") if item.strip())
    return values or None  # type: ignore[return-value]


async def read_upload_limited(file: UploadFile, *, max_upload_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_upload_bytes:
            raise ValueError(f"File too large (max {max_upload_bytes} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


def resolve_max_upload_bytes(raw: Any) -> int:
    if raw is None:
        return DEFAULT_FILEBOX_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_FILEBOX_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_FILEBOX_MAX_UPLOAD_BYTES


__all__ = [
    "ArchiveFileResource",
    "ArchiveResourceService",
    "ContextspaceResourceService",
    "DEFAULT_FILEBOX_MAX_UPLOAD_BYTES",
    "FileBoxDownloadResource",
    "FileBoxResourceService",
    "FileBoxUrlScope",
    "WorkspaceResourceError",
    "normalize_delivery_states",
    "read_upload_limited",
    "resolve_max_upload_bytes",
    "add_delivery_download_url",
    "serialize_filebox_entry",
    "serialize_filebox_listing",
]
