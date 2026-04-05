from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .....core.flows.workspace_root import resolve_ticket_flow_workspace_root

_logger = logging.getLogger(__name__)


def resolve_outbox_for_record(repo_root: Path, record: Any) -> Any:
    from .....tickets.outbox import resolve_outbox_paths

    input_data = dict(getattr(record, "input_data", {}) or {})
    try:
        workspace_root = resolve_ticket_flow_workspace_root(input_data, repo_root)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return resolve_outbox_paths(workspace_root=workspace_root, run_id=record.id)


def get_diff_stats_by_dispatch_seq(
    repo_root: Path, record: Any, outbox_paths: Any = None
) -> dict[int, dict[str, int]]:
    from .....core.flows import FlowEventType
    from ...services import flow_store as flow_store_service

    store = flow_store_service.require_flow_store(repo_root, logger=_logger)
    if store is None:
        return {}

    diff_stats: dict[int, dict[str, int]] = {}
    try:
        events = store.get_events_by_type(record.id, FlowEventType.DIFF_UPDATED)
        for event in events:
            data = event.data or {}
            try:
                dispatch_seq = int(data.get("dispatch_seq") or 0)
            except Exception:
                continue
            if dispatch_seq <= 0:
                continue
            diff_stats[dispatch_seq] = {
                "insertions": int(data.get("insertions") or 0),
                "deletions": int(data.get("deletions") or 0),
                "files_changed": int(data.get("files_changed") or 0),
            }
    except Exception:
        _logger.debug(
            "Failed to read diff stats events for run %s", record.id, exc_info=True
        )
    finally:
        try:
            store.close()
        except Exception:
            _logger.debug(
                "Failed to close flow store after reading diff stats", exc_info=True
            )

    return diff_stats


def get_dispatch_history(
    repo_root: Path,
    run_id: str,
    flow_type: str,
) -> dict[str, Any]:
    from .....tickets.outbox import parse_dispatch
    from ...services.flow_store import get_flow_record

    record = get_flow_record(repo_root, run_id)

    outbox_paths = resolve_outbox_for_record(repo_root, record)

    diff_by_seq = get_diff_stats_by_dispatch_seq(repo_root, record, outbox_paths)
    history_entries = []
    history_dir = outbox_paths.dispatch_history_dir
    if history_dir.exists() and history_dir.is_dir():
        for entry in sorted(
            [p for p in history_dir.iterdir() if p.is_dir()],
            key=lambda p: p.name,
            reverse=True,
        ):
            dispatch_path = entry / "DISPATCH.md"
            dispatch, errors = (
                parse_dispatch(dispatch_path)
                if dispatch_path.exists()
                else (None, ["Dispatch file missing"])
            )
            dispatch_dict = asdict(dispatch) if dispatch else None
            if dispatch_dict and dispatch:
                dispatch_dict["is_handoff"] = dispatch.is_handoff
                try:
                    entry_seq = int(entry.name)
                except Exception:
                    entry_seq = 0
                if entry_seq and entry_seq in diff_by_seq:
                    dispatch_dict["diff_stats"] = diff_by_seq[entry_seq]
            history_entries.append(
                {
                    "seq": entry.name,
                    "dispatch": dispatch_dict,
                    "errors": errors,
                    "path": str(entry),
                }
            )

    return {"run_id": run_id, "history": history_entries}


def get_dispatch_history_file(
    repo_root: Path,
    run_id: str,
    seq: int,
    file_path: str,
    flow_type: str,
):
    from .....core.safe_paths import SafePathError, validate_single_filename
    from ...services.flow_store import get_flow_record

    record = get_flow_record(repo_root, run_id)

    try:
        validated = validate_single_filename(file_path)
    except SafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    outbox_paths = resolve_outbox_for_record(repo_root, record)
    target = outbox_paths.dispatch_history_dir / f"{seq:04d}" / validated
    if target.exists():
        from fastapi.responses import FileResponse

        return FileResponse(target)

    raise HTTPException(status_code=404, detail="File not found")


def get_reply_history(
    repo_root: Path,
    run_id: str,
    seq: int,
    file_path: str,
    flow_type: str,
):
    from .....core.safe_paths import SafePathError, validate_single_filename
    from ...services.flow_store import get_flow_record

    record = get_flow_record(repo_root, run_id)

    try:
        validated = validate_single_filename(file_path)
    except SafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    outbox_paths = resolve_outbox_for_record(repo_root, record)
    target = outbox_paths.run_dir / "reply_history" / f"{seq:04d}" / validated
    if target.exists():
        from fastapi.responses import FileResponse

        return FileResponse(target)

    raise HTTPException(status_code=404, detail="File not found")


def get_artifacts(
    repo_root: Path,
    run_id: str,
    flow_type: str,
) -> dict[str, Any]:
    from ...services.flow_store import get_flow_record

    record = get_flow_record(repo_root, run_id)

    outbox_paths = resolve_outbox_for_record(repo_root, record)

    artifacts = []
    history_dir = outbox_paths.dispatch_history_dir
    if history_dir.exists():
        try:
            for item in history_dir.iterdir():
                if item.is_dir():
                    artifacts.append({"path": str(item), "seq": item.name})
        except OSError:
            pass

    artifacts.sort(key=lambda x: x.get("seq", 0) or 0, reverse=True)

    return {
        "run_id": run_id,
        "artifacts": artifacts,
    }


def get_artifact(
    repo_root: Path,
    run_id: str,
    seq: int,
    file_path: str,
    flow_type: str,
):
    from .....core.safe_paths import SafePathError, validate_single_filename
    from ...services.flow_store import get_flow_record

    record = get_flow_record(repo_root, run_id)

    try:
        validated = validate_single_filename(file_path)
    except SafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    outbox_paths = resolve_outbox_for_record(repo_root, record)
    target = outbox_paths.dispatch_history_dir / f"{seq:04d}" / validated
    if target.exists():
        from fastapi.responses import FileResponse

        return FileResponse(target)

    raise HTTPException(status_code=404, detail="File not found")


_HISTORY_ARTIFACT_ROUTE_API = (
    get_diff_stats_by_dispatch_seq,
    get_dispatch_history_file,
    get_reply_history,
)
