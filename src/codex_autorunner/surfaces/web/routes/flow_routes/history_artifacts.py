from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import HTTPException

_logger = logging.getLogger(__name__)


def resolve_outbox_for_record(repo_root: Path, record: Any) -> list[Path]:
    from ....tickets.outbox import resolve_outbox_paths

    return resolve_outbox_paths(repo_root)


def get_diff_stats_by_dispatch_seq(
    repo_root: Path, record: Any, outbox_paths: list[Path]
) -> dict[int, dict[str, int]]:
    from ....core.flows import FlowEventType
    from ...services import flow_store as flow_store_service

    store = flow_store_service.require_flow_store(repo_root, logger=_logger)
    if store is None:
        return {}

    diff_stats: dict[int, dict[str, int]] = {}
    try:
        events = store.get_events_by_type(record.id, FlowEventType.DIFF_UPDATED)
        for event in events:
            seq = event.seq
            data = event.data or {}
            diff_stats[seq] = {
                "insertions": data.get("insertions", 0),
                "deletions": data.get("deletions", 0),
                "files_changed": data.get("files_changed", 0),
            }
    except Exception:
        pass

    return diff_stats


def get_dispatch_history(
    repo_root: Path,
    run_id: str,
    flow_type: str,
) -> dict[str, Any]:
    from ....tickets.outbox import parse_dispatch
    from .definitions import get_flow_record

    record = get_flow_record(repo_root, run_id, None)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")

    outbox_paths = resolve_outbox_for_record(repo_root, record)

    dispatches = []
    for outbox_path in outbox_paths:
        if not outbox_path.exists():
            continue
        try:
            dispatch = parse_dispatch(outbox_path)
            if dispatch:
                dispatches.append(
                    {
                        "path": str(outbox_path),
                        "seq": dispatch.get("seq"),
                        "timestamp": dispatch.get("timestamp"),
                        "kind": dispatch.get("kind"),
                    }
                )
        except Exception:
            pass

    dispatches.sort(key=lambda x: x.get("seq", 0) or 0, reverse=True)

    return {
        "run_id": run_id,
        "dispatches": dispatches,
    }


def get_dispatch_history_file(
    repo_root: Path,
    run_id: str,
    seq: int,
    file_path: str,
    flow_type: str,
):
    from fastapi import HTTPException

    from ....core.safe_paths import SafePathError, validate_single_filename
    from .definitions import get_flow_record

    record = get_flow_record(repo_root, run_id, None)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")

    try:
        validated = validate_single_filename(file_path)
    except SafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    outbox_paths = resolve_outbox_for_record(repo_root, record)

    for outbox_path in outbox_paths:
        target = outbox_path / f"{seq}" / validated
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
    from fastapi import HTTPException

    from ....core.safe_paths import SafePathError, validate_single_filename
    from .definitions import get_flow_record

    record = get_flow_record(repo_root, run_id, None)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")

    try:
        validated = validate_single_filename(file_path)
    except SafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    outbox_paths = resolve_outbox_for_record(repo_root, record)

    for outbox_path in outbox_paths:
        reply_dir = outbox_path / f"{seq}" / "replies"
        if not reply_dir.exists():
            continue
        target = reply_dir / validated
        if target.exists():
            from fastapi.responses import FileResponse

            return FileResponse(target)

    raise HTTPException(status_code=404, detail="File not found")


def get_artifacts(
    repo_root: Path,
    run_id: str,
    flow_type: str,
) -> dict[str, Any]:
    from fastapi import HTTPException

    from .definitions import get_flow_record

    record = get_flow_record(repo_root, run_id, None)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")

    outbox_paths = resolve_outbox_for_record(repo_root, record)

    artifacts = []
    for outbox_path in outbox_paths:
        if not outbox_path.exists():
            continue
        try:
            for item in outbox_path.iterdir():
                if item.is_dir():
                    artifacts.append(
                        {
                            "path": str(item),
                            "seq": item.name,
                        }
                    )
        except Exception:
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
    from fastapi import HTTPException

    from ....core.safe_paths import SafePathError, validate_single_filename
    from .definitions import get_flow_record

    record = get_flow_record(repo_root, run_id, None)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")

    try:
        validated = validate_single_filename(file_path)
    except SafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    outbox_paths = resolve_outbox_for_record(repo_root, record)

    for outbox_path in outbox_paths:
        target = outbox_path / f"{seq}" / validated
        if target.exists():
            from fastapi.responses import FileResponse

            return FileResponse(target)

    raise HTTPException(status_code=404, detail="File not found")


_HISTORY_ARTIFACT_ROUTE_API = (
    get_diff_stats_by_dispatch_seq,
    get_dispatch_history_file,
    get_reply_history,
)
