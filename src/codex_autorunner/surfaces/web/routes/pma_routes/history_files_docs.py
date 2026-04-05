from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from .....bootstrap import (
    ensure_pma_docs,
    pma_about_content,
    pma_active_context_content,
    pma_agents_content,
    pma_context_log_content,
    pma_doc_path,
    pma_docs_dir,
    pma_prompt_content,
)
from .....core import filebox
from .....core.filebox import BOXES
from .....core.freshness import build_freshness_payload, resolve_stale_threshold_seconds
from .....core.orchestration.turn_timeline import list_turn_timeline
from .....core.pma_audit import PmaActionType
from .....core.pma_context import (
    enrich_pma_file_inbox_entry,
    get_active_context_auto_prune_meta,
)
from .....core.pma_dispatches import (
    find_pma_dispatch_path,
    list_pma_dispatches,
    resolve_pma_dispatch,
)
from .....core.pma_transcripts import PmaTranscriptStore
from .....core.time_utils import now_iso
from .....core.utils import atomic_write

logger = logging.getLogger(__name__)

PMA_CONTEXT_SNAPSHOT_MAX_BYTES = 200_000
PMA_CONTEXT_LOG_SOFT_LIMIT_BYTES = 5_000_000
PMA_BULK_DELETE_SAMPLE_LIMIT = 10


def build_history_files_docs_router(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build PMA history, files, docs, and dispatches routes.

    This includes:
    - /history - List recent transcripts
    - /history/{turn_id} - Get specific transcript
    - /files - List inbox/outbox files
    - /files/{box} - Upload file
    - /files/{box}/{filename} - Download/delete file
    - /context/snapshot - Snapshot active context
    - /docs - List docs
    - /docs/{name} - Get/update doc
    - /docs/default/{name} - Get default doc content
    - /docs/history/{name} - List doc history
    - /docs/history/{name}/{version_id} - Get doc version
    - /dispatches - List dispatches
    - /dispatches/{dispatch_id} - Get dispatch
    - /dispatches/{dispatch_id}/resolve - Resolve dispatch
    """

    def _get_pma_config(request: Request) -> dict[str, Any]:
        from ...services.pma.common import pma_config_from_raw

        raw = getattr(request.app.state.config, "raw", {})
        return pma_config_from_raw(raw)

    async def _get_pma_lock():
        runtime_state = get_runtime_state()
        return await runtime_state.get_pma_lock()

    def _get_safety_checker(request: Request):
        runtime_state = get_runtime_state()
        hub_root = request.app.state.config.root
        return runtime_state.get_safety_checker(hub_root, request)

    async def _append_text_file(path: Path, content: str) -> None:
        def _append() -> None:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(content)

        await asyncio.to_thread(_append)

    async def _atomic_write_async(path: Path, content: str) -> None:
        await asyncio.to_thread(atomic_write, path, content)

    def _pma_docs_dir(hub_root: Path) -> Path:
        return pma_docs_dir(hub_root)

    def _normalize_doc_name(name: str) -> str:
        try:
            return filebox.sanitize_filename(name)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid doc name: {name}"
            ) from exc

    def _sorted_doc_names(docs_dir: Path) -> list[str]:
        names: set[str] = set()
        if docs_dir.exists():
            try:
                for path in docs_dir.iterdir():
                    if not path.is_file():
                        continue
                    if path.name.startswith("."):
                        continue
                    names.add(path.name)
            except OSError:
                pass
        ordered: list[str] = []
        for doc_name in (
            "AGENTS.md",
            "active_context.md",
            "context_log.md",
            "ABOUT_CAR.md",
            "prompt.md",
        ):
            if doc_name in names:
                ordered.append(doc_name)
        remaining = sorted(name for name in names if name not in ordered)
        ordered.extend(remaining)
        return ordered

    async def _write_doc_history(
        hub_root: Path, doc_name: str, content: str
    ) -> Optional[Path]:
        docs_dir = _pma_docs_dir(hub_root)
        history_root = docs_dir / "_history" / doc_name

        def _write() -> Path:
            history_root.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            history_path = history_root / f"{timestamp}.md"
            atomic_write(history_path, content)
            return history_path

        try:
            return await asyncio.to_thread(_write)
        except Exception:
            logger.exception("Failed to write PMA doc history for %s", doc_name)
            return None

    @router.get("/history")
    def list_pma_history(request: Request, limit: int = 50) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        # The transcript store serves the canonical sqlite mirror and falls back to
        # legacy files only while older history is being migrated.
        store = PmaTranscriptStore(hub_root)
        entries = store.list_recent(limit=limit)
        return {"entries": entries}

    @router.get("/history/{turn_id}")
    def get_pma_history(turn_id: str, request: Request) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        store = PmaTranscriptStore(hub_root)
        transcript = store.read_transcript(turn_id)
        if not transcript:
            raise HTTPException(status_code=404, detail="Transcript not found")
        transcript["timeline"] = list_turn_timeline(hub_root, execution_id=turn_id)
        return transcript

    def _serialize_pma_entry(
        entry: filebox.FileBoxEntry,
        *,
        request: Request,
        generated_at: str,
        stale_threshold_seconds: int,
    ) -> dict[str, Any]:
        base = request.scope.get("root_path", "") or ""
        box = entry.box
        filename = entry.name
        download = f"{base}/hub/pma/files/{box}/{filename}"
        payload: dict[str, Any] = {
            "item_type": "pma_file",
            "name": filename,
            "box": box,
            "size": entry.size,
            "modified_at": entry.modified_at,
            "source": entry.source,
            "url": download,
        }
        if box == "inbox":
            payload["freshness"] = build_freshness_payload(
                generated_at=generated_at,
                stale_threshold_seconds=stale_threshold_seconds,
                candidates=[("file_modified_at", entry.modified_at)],
            )
            return enrich_pma_file_inbox_entry(payload)
        return payload

    @router.get("/files")
    async def list_pma_files(request: Request) -> dict[str, list[dict[str, Any]]]:
        hub_root = request.app.state.config.root
        result: dict[str, list[dict[str, Any]]] = {box: [] for box in BOXES}
        generated_at = now_iso()
        stale_threshold_seconds = resolve_stale_threshold_seconds(
            getattr(
                getattr(request.app.state.config, "pma", None),
                "freshness_stale_threshold_seconds",
                None,
            )
        )
        async with await _get_pma_lock():
            listing = await asyncio.to_thread(filebox.list_filebox, hub_root)
        for box in BOXES:
            entries = listing.get(box, [])
            result[box] = [
                _serialize_pma_entry(
                    entry,
                    request=request,
                    generated_at=generated_at,
                    stale_threshold_seconds=stale_threshold_seconds,
                )
                for entry in sorted(entries, key=lambda item: item.name)
            ]
        return result

    @router.post("/files/{box}")
    async def upload_pma_file(box: str, request: Request):
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        hub_root = request.app.state.config.root
        max_upload_bytes = request.app.state.config.pma.max_upload_bytes

        form = await request.form()
        saved = []
        for _form_field_name, file in form.items():
            try:
                if isinstance(file, UploadFile):
                    content = await file.read()
                    filename = file.filename or ""
                else:
                    content = file if isinstance(file, bytes) else str(file).encode()
                    filename = ""
            except Exception as exc:
                logger.warning("Failed to read PMA upload: %s", exc)
                raise HTTPException(
                    status_code=400, detail="Failed to read file"
                ) from exc
            if len(content) > max_upload_bytes:
                logger.warning(
                    "File too large for PMA upload: %s (%d bytes)",
                    filename,
                    len(content),
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"File too large (max {max_upload_bytes} bytes)",
                )
            try:
                async with await _get_pma_lock():
                    target_path = await asyncio.to_thread(
                        filebox.save_file, hub_root, box, filename, content
                    )
                saved.append(target_path.name)
                _get_safety_checker(request).record_action(
                    action_type=PmaActionType.FILE_UPLOADED,
                    details={
                        "box": box,
                        "filename": target_path.name,
                        "size": len(content),
                    },
                )
            except ValueError as exc:
                logger.warning("Invalid PMA upload target: %s (%s)", filename, exc)
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                logger.warning("Failed to write PMA file: %s", exc)
                raise HTTPException(
                    status_code=500, detail="Failed to save file"
                ) from exc
        return {"status": "ok", "saved": saved}

    @router.get("/files/{box}/{filename}")
    def download_pma_file(box: str, filename: str, request: Request):
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        hub_root = request.app.state.config.root
        try:
            entry = filebox.resolve_file(hub_root, box, filename)
        except ValueError as exc:
            logger.warning("Invalid filename in PMA download: %s (%s)", filename, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if entry is None:
            logger.warning("File not found in PMA download: %s", filename)
            raise HTTPException(status_code=404, detail="File not found")
        _get_safety_checker(request).record_action(
            action_type=PmaActionType.FILE_DOWNLOADED,
            details={
                "box": box,
                "filename": entry.name,
                "size": entry.size,
            },
        )
        return FileResponse(entry.path, filename=entry.name)

    @router.delete("/files/{box}/{filename}")
    async def delete_pma_file(box: str, filename: str, request: Request):
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        hub_root = request.app.state.config.root
        entry: Optional[filebox.FileBoxEntry] = None
        try:
            async with await _get_pma_lock():
                entry = await asyncio.to_thread(
                    filebox.resolve_file, hub_root, box, filename
                )
                if entry is None:
                    logger.warning("File not found in PMA delete: %s", filename)
                    raise HTTPException(status_code=404, detail="File not found")
                await asyncio.to_thread(entry.path.unlink)
        except ValueError as exc:
            logger.warning("Invalid filename in PMA delete: %s (%s)", filename, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except FileNotFoundError:
            logger.warning("File not found in PMA delete: %s", filename)
            raise HTTPException(status_code=404, detail="File not found") from None
        except Exception as exc:
            logger.warning("Failed to delete PMA file: %s", exc)
            raise HTTPException(
                status_code=500, detail="Failed to delete file"
            ) from exc
        if entry is not None:
            _get_safety_checker(request).record_action(
                action_type=PmaActionType.FILE_DELETED,
                details={"box": box, "filename": entry.name, "size": entry.size},
            )
        return {"status": "ok"}

    @router.delete("/files/{box}")
    async def delete_pma_box(box: str, request: Request):
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        hub_root = request.app.state.config.root
        deleted_files: list[str] = []
        async with await _get_pma_lock():
            entries = await asyncio.to_thread(filebox.list_filebox, hub_root)
            for entry in entries.get(box, []):
                try:
                    await asyncio.to_thread(entry.path.unlink)
                    deleted_files.append(entry.name)
                except FileNotFoundError:
                    continue
        _get_safety_checker(request).record_action(
            action_type=PmaActionType.FILE_BULK_DELETED,
            details={
                "box": box,
                "count": len(deleted_files),
                "sample": deleted_files[:PMA_BULK_DELETE_SAMPLE_LIMIT],
            },
        )
        return {"status": "ok"}

    @router.post("/context/snapshot")
    async def snapshot_pma_context(
        request: Request, body: Optional[dict[str, Any]] = None
    ):
        hub_root = request.app.state.config.root
        try:
            await asyncio.to_thread(ensure_pma_docs, hub_root)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to ensure PMA docs: {exc}"
            ) from exc

        reset = False
        if isinstance(body, dict):
            reset = bool(body.get("reset", False))

        docs_dir = _pma_docs_dir(hub_root)
        try:
            await asyncio.to_thread(docs_dir.mkdir, parents=True, exist_ok=True)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to prepare PMA docs directory: {exc}"
            ) from exc
        active_context_path = docs_dir / "active_context.md"
        if not await asyncio.to_thread(active_context_path.exists):
            raise HTTPException(
                status_code=404, detail="Doc not found: active_context.md"
            )
        context_log_path = docs_dir / "context_log.md"

        try:
            active_content = await asyncio.to_thread(
                active_context_path.read_text, encoding="utf-8"
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to read active_context.md: {exc}"
            ) from exc

        timestamp = now_iso()
        snapshot_header = f"\n\n## Snapshot: {timestamp}\n\n"
        snapshot_content = snapshot_header + active_content
        snapshot_bytes = len(snapshot_content.encode("utf-8"))
        if snapshot_bytes > PMA_CONTEXT_SNAPSHOT_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Snapshot too large (max {PMA_CONTEXT_SNAPSHOT_MAX_BYTES} bytes)"
                ),
            )

        try:
            await _append_text_file(context_log_path, snapshot_content)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to append context_log.md: {exc}"
            ) from exc

        if reset:
            try:
                await _atomic_write_async(
                    active_context_path, pma_active_context_content()
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=500, detail=f"Failed to reset active_context.md: {exc}"
                ) from exc

        line_count = len(active_content.splitlines())
        response: dict[str, Any] = {
            "status": "ok",
            "timestamp": timestamp,
            "active_context_line_count": line_count,
            "reset": reset,
        }
        try:
            context_log_bytes = (await asyncio.to_thread(context_log_path.stat)).st_size
            response["context_log_bytes"] = context_log_bytes
            if context_log_bytes > PMA_CONTEXT_LOG_SOFT_LIMIT_BYTES:
                response["warning"] = (
                    "context_log.md is large "
                    f"({context_log_bytes} bytes); consider pruning"
                )
        except Exception:
            logger.debug("Failed to stat context_log.md", exc_info=True)

        return response

    PMA_DOC_ORDER = (
        "AGENTS.md",
        "active_context.md",
        "context_log.md",
        "ABOUT_CAR.md",
        "prompt.md",
    )
    PMA_DOC_SET = set(PMA_DOC_ORDER)
    PMA_DOC_DEFAULTS = {
        "AGENTS.md": pma_agents_content,
        "active_context.md": pma_active_context_content,
        "context_log.md": pma_context_log_content,
        "ABOUT_CAR.md": pma_about_content,
        "prompt.md": pma_prompt_content,
    }

    @router.get("/docs/default/{name}")
    def get_pma_doc_default(name: str, request: Request) -> dict[str, str]:
        if name not in PMA_DOC_SET:
            raise HTTPException(status_code=400, detail=f"Unknown doc name: {name}")
        content_fn = PMA_DOC_DEFAULTS.get(name)
        if content_fn is None:
            raise HTTPException(status_code=404, detail=f"Default not found: {name}")
        return {"name": name, "content": content_fn()}

    @router.get("/docs")
    def list_pma_docs(request: Request) -> dict[str, Any]:
        pma_config = _get_pma_config(request)
        hub_root = request.app.state.config.root
        try:
            ensure_pma_docs(hub_root)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to ensure PMA docs: {exc}"
            ) from exc
        docs_dir = _pma_docs_dir(hub_root)
        result: list[dict[str, Any]] = []
        for doc_name in _sorted_doc_names(docs_dir):
            doc_path = docs_dir / doc_name
            entry: dict[str, Any] = {"name": doc_name}
            if doc_path.exists():
                entry["exists"] = True
                stat = doc_path.stat()
                entry["size"] = stat.st_size
                entry["mtime"] = datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat()
                if doc_name == "active_context.md":
                    try:
                        entry["line_count"] = len(
                            doc_path.read_text(encoding="utf-8").splitlines()
                        )
                    except Exception:
                        entry["line_count"] = 0
            else:
                entry["exists"] = False
            result.append(entry)
        return {
            "docs": result,
            "active_context_max_lines": int(
                pma_config.get("active_context_max_lines", 200)
            ),
            "active_context_auto_prune": get_active_context_auto_prune_meta(hub_root),
        }

    @router.get("/docs/{name}")
    def get_pma_doc(name: str, request: Request) -> dict[str, str]:
        name = _normalize_doc_name(name)
        hub_root = request.app.state.config.root
        doc_path = pma_doc_path(hub_root, name)
        if not doc_path.exists():
            raise HTTPException(status_code=404, detail=f"Doc not found: {name}")
        try:
            content = doc_path.read_text(encoding="utf-8")
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to read doc: {exc}"
            ) from exc
        return {"name": name, "content": content}

    @router.put("/docs/{name}")
    async def update_pma_doc(
        name: str, request: Request, body: dict[str, str]
    ) -> dict[str, str]:
        name = _normalize_doc_name(name)
        hub_root = request.app.state.config.root
        docs_dir = _pma_docs_dir(hub_root)
        if name not in PMA_DOC_SET:
            raise HTTPException(status_code=400, detail=f"Unknown doc name: {name}")
        content = body.get("content", "")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="content must be a string")
        MAX_DOC_SIZE = 500_000
        if len(content) > MAX_DOC_SIZE:
            raise HTTPException(
                status_code=413, detail=f"Content too large (max {MAX_DOC_SIZE} bytes)"
            )
        docs_dir.mkdir(parents=True, exist_ok=True)
        doc_path = docs_dir / name
        try:
            await _atomic_write_async(doc_path, content)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to write doc: {exc}"
            ) from exc
        await _write_doc_history(hub_root, name, content)
        details = {
            "name": name,
            "size": len(content.encode("utf-8")),
            "source": "web",
        }
        if name == "active_context.md":
            details["line_count"] = len(content.splitlines())
        _get_safety_checker(request).record_action(
            action_type=PmaActionType.DOC_UPDATED,
            details=details,
        )
        return {"name": name, "status": "ok"}

    @router.get("/docs/history/{name}")
    def list_pma_doc_history(
        name: str, request: Request, limit: int = 50
    ) -> dict[str, Any]:
        name = _normalize_doc_name(name)
        hub_root = request.app.state.config.root
        docs_dir = _pma_docs_dir(hub_root)
        history_dir = docs_dir / "_history" / name
        entries: list[dict[str, Any]] = []
        if history_dir.exists():
            try:
                for path in sorted(
                    (p for p in history_dir.iterdir() if p.is_file()),
                    key=lambda p: p.name,
                    reverse=True,
                ):
                    if len(entries) >= limit:
                        break
                    try:
                        stat = path.stat()
                        entries.append(
                            {
                                "id": path.name,
                                "size": stat.st_size,
                                "mtime": datetime.fromtimestamp(
                                    stat.st_mtime, tz=timezone.utc
                                ).isoformat(),
                            }
                        )
                    except OSError:
                        continue
            except OSError:
                pass
        return {"name": name, "entries": entries}

    @router.get("/docs/history/{name}/{version_id}")
    def get_pma_doc_history(
        name: str, version_id: str, request: Request
    ) -> dict[str, str]:
        name = _normalize_doc_name(name)
        version_id = _normalize_doc_name(version_id)
        hub_root = request.app.state.config.root
        docs_dir = _pma_docs_dir(hub_root)
        history_path = docs_dir / "_history" / name / version_id
        if not history_path.exists():
            raise HTTPException(status_code=404, detail="History entry not found")
        try:
            content = history_path.read_text(encoding="utf-8")
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to read history entry: {exc}"
            ) from exc
        return {"name": name, "version_id": version_id, "content": content}

    @router.get("/dispatches")
    def list_pma_dispatches_endpoint(
        request: Request, include_resolved: bool = False, limit: int = 100
    ) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        dispatches = list_pma_dispatches(
            hub_root, include_resolved=include_resolved, limit=limit
        )
        return {
            "items": [
                {
                    "id": item.dispatch_id,
                    "title": item.title,
                    "body": item.body,
                    "priority": item.priority,
                    "links": item.links,
                    "created_at": item.created_at,
                    "resolved_at": item.resolved_at,
                    "source_turn_id": item.source_turn_id,
                }
                for item in dispatches
            ]
        }

    @router.get("/dispatches/{dispatch_id}")
    def get_pma_dispatch(dispatch_id: str, request: Request) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        path = find_pma_dispatch_path(hub_root, dispatch_id)
        if not path:
            raise HTTPException(status_code=404, detail="Dispatch not found")
        items = list_pma_dispatches(hub_root, include_resolved=True)
        match = next((item for item in items if item.dispatch_id == dispatch_id), None)
        if not match:
            raise HTTPException(status_code=404, detail="Dispatch not found")
        return {
            "dispatch": {
                "id": match.dispatch_id,
                "title": match.title,
                "body": match.body,
                "priority": match.priority,
                "links": match.links,
                "created_at": match.created_at,
                "resolved_at": match.resolved_at,
                "source_turn_id": match.source_turn_id,
            }
        }

    @router.post("/dispatches/{dispatch_id}/resolve")
    def resolve_pma_dispatch_endpoint(
        dispatch_id: str, request: Request
    ) -> dict[str, Any]:
        hub_root = request.app.state.config.root
        path = find_pma_dispatch_path(hub_root, dispatch_id)
        if not path:
            raise HTTPException(status_code=404, detail="Dispatch not found")
        dispatch, errors = resolve_pma_dispatch(path)
        if errors or dispatch is None:
            raise HTTPException(
                status_code=500,
                detail="Failed to resolve dispatch: " + "; ".join(errors),
            )
        return {
            "dispatch": {
                "id": dispatch.dispatch_id,
                "title": dispatch.title,
                "body": dispatch.body,
                "priority": dispatch.priority,
                "links": dispatch.links,
                "created_at": dispatch.created_at,
                "resolved_at": dispatch.resolved_at,
                "source_turn_id": dispatch.source_turn_id,
            }
        }
