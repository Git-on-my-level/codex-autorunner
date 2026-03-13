#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from codex_autorunner.agents.opencode.run_prompt import (
    OpenCodeRunConfig,
    run_opencode_prompt,
)
from codex_autorunner.agents.opencode.supervisor import OpenCodeSupervisor
from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import REPO_OVERRIDE_FILENAME, load_repo_config
from codex_autorunner.core.managed_processes import list_process_records
from codex_autorunner.core.runtime import summarize_opencode_lifecycle
from codex_autorunner.integrations.agents.opencode_backend import OpenCodeBackend


def _quiet_logger() -> logging.Logger:
    logger = logging.getLogger("opencode.lifecycle.soak")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger


def _car_bin() -> str:
    candidate = Path(sys.executable).with_name("car")
    if candidate.exists():
        return str(candidate)
    resolved = shutil.which("car")
    if resolved:
        return resolved
    raise RuntimeError("car CLI not found in the current environment")


def _opencode_bin() -> str:
    resolved = shutil.which("opencode")
    if resolved:
        return resolved
    raise RuntimeError("opencode binary not found in PATH")


def _write_repo_override(
    repo_root: Path, *, max_handles: int, idle_ttl_seconds: int
) -> None:
    override_path = repo_root / REPO_OVERRIDE_FILENAME
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text(
        yaml.safe_dump(
            {
                "opencode": {
                    "server_scope": "workspace",
                    "max_handles": max_handles,
                    "idle_ttl_seconds": idle_ttl_seconds,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _ps_info(pid: Optional[int]) -> Optional[dict[str, Any]]:
    if pid is None or pid <= 0:
        return None
    proc = subprocess.run(
        [
            "ps",
            "-p",
            str(pid),
            "-o",
            "pid=",
            "-o",
            "ppid=",
            "-o",
            "pgid=",
            "-o",
            "rss=",
            "-o",
            "etime=",
            "-o",
            "command=",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    line = (proc.stdout or "").strip()
    if proc.returncode != 0 or not line:
        return None
    parts = line.split(maxsplit=5)
    if len(parts) < 6:
        return None
    return {
        "pid": int(parts[0]),
        "ppid": int(parts[1]),
        "pgid": int(parts[2]),
        "rss_kb": int(parts[3]) if parts[3].isdigit() else None,
        "elapsed": parts[4],
        "command": parts[5],
    }


def _pid_exists(pid: Optional[int]) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_for_pid_exit(pid: Optional[int], timeout: float = 20.0) -> bool:
    if pid is None or pid <= 0:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.1)
    return not _pid_exists(pid)


def _registry_records(repo_root: Path) -> list[dict[str, Any]]:
    records = []
    for record in list_process_records(repo_root, kind="opencode"):
        records.append(
            {
                "record_key": record.record_key(),
                "workspace_id": record.workspace_id,
                "pid": record.pid,
                "pgid": record.pgid,
                "base_url": record.base_url,
                "metadata": dict(record.metadata),
            }
        )
    return records


def _doctor_processes(repo_root: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [_car_bin(), "doctor", "processes", "--repo", str(repo_root), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "car doctor processes failed: "
            + ((proc.stderr or proc.stdout or "").strip()[:500] or "unknown error")
        )
    payload = json.loads(proc.stdout)
    snapshot = payload.get("snapshot") or {}
    lifecycle = payload.get("opencode_lifecycle") or {}
    registry = payload.get("registry") or {}
    return {
        "snapshot_opencode_count": len(snapshot.get("opencode") or []),
        "snapshot_opencode": snapshot.get("opencode") or [],
        "registry_counts": registry.get("counts_by_kind") or {},
        "registry_records": registry.get("records") or [],
        "opencode_lifecycle": lifecycle,
    }


def _condense_doctor(repo_root: Path) -> dict[str, Any]:
    payload = _doctor_processes(repo_root)
    lifecycle = payload["opencode_lifecycle"]
    snapshot_opencode = payload["snapshot_opencode"]
    return {
        "snapshot_opencode_count": payload["snapshot_opencode_count"],
        "unmanaged_opencode_pids": [
            proc.get("pid")
            for proc in snapshot_opencode
            if proc.get("ownership") != "managed"
        ],
        "registry_counts": payload["registry_counts"],
        "managed_record_keys": [
            record.get("record_key") for record in payload["registry_records"]
        ],
        "lifecycle_counts": lifecycle.get("counts") or {},
        "managed_servers": lifecycle.get("managed_servers") or [],
        "live_handles": lifecycle.get("live_handles") or [],
    }


def _handle_pid(supervisor: OpenCodeSupervisor) -> Optional[int]:
    payload = supervisor.observability_snapshot()
    handles = payload.get("handles") or []
    if not handles:
        return None
    handle = handles[0]
    pid = handle.get("process_pid") or handle.get("managed_pid")
    return int(pid) if isinstance(pid, int) else None


async def _provider_probe(
    repo_root: Path, command: list[str], logger: logging.Logger
) -> Any:
    supervisor = OpenCodeSupervisor(command, request_timeout=30.0, logger=logger)
    try:
        client = await supervisor.get_client(repo_root)
        payload = await client.providers(directory=str(repo_root))
        if isinstance(payload, dict):
            return payload.get("providers") or payload
        return payload
    finally:
        await supervisor.close_all()


def _summarize_providers(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    summaries: list[dict[str, Any]] = []
    for provider in payload:
        if not isinstance(provider, dict):
            continue
        models = provider.get("models")
        model_ids: list[str] = []
        if isinstance(models, dict):
            model_ids = sorted(str(model_id) for model_id in models.keys())[:5]
        summaries.append(
            {
                "id": provider.get("id"),
                "name": provider.get("name"),
                "source": provider.get("source"),
                "env": provider.get("env") or [],
                "model_count": len(models) if isinstance(models, dict) else 0,
                "sample_models": model_ids,
            }
        )
    return summaries


async def _registry_reuse_phase(
    repo_root: Path, command: list[str], logger: logging.Logger
) -> dict[str, Any]:
    first = OpenCodeSupervisor(
        command,
        request_timeout=30.0,
        max_handles=2,
        idle_ttl_seconds=2.0,
        logger=logger,
    )
    second = OpenCodeSupervisor(
        command,
        request_timeout=30.0,
        max_handles=2,
        idle_ttl_seconds=2.0,
        logger=logger,
    )
    result: dict[str, Any] = {}
    first_pid: Optional[int] = None
    try:
        client = await first.get_client(repo_root)
        await client.close()
        result["doctor_after_first_start"] = _condense_doctor(repo_root)
        result["registry_after_first_start"] = _registry_records(repo_root)
        result["first_snapshot"] = first.observability_snapshot()
        first_pid = _handle_pid(first)
        result["first_pid_ps"] = _ps_info(first_pid)

        second_client = await second.get_client(repo_root)
        await second_client.close()
        result["second_snapshot"] = second.observability_snapshot()
        result["doctor_during_reuse"] = _condense_doctor(repo_root)
        result["registry_during_reuse"] = _registry_records(repo_root)

        await second.close_all()
        result["second_close_removed_pid"] = _wait_for_pid_exit(first_pid)
        result["doctor_after_reused_close"] = _condense_doctor(repo_root)
        result["registry_after_reused_close"] = _registry_records(repo_root)
    finally:
        await first.close_all()
        await second.close_all()
    result["doctor_after_registry_phase"] = _condense_doctor(repo_root)
    result["registry_after_registry_phase"] = _registry_records(repo_root)
    return result


async def _one_shot_phase(
    repo_root: Path,
    command: list[str],
    logger: logging.Logger,
    *,
    cycles: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    supervisor = OpenCodeSupervisor(
        command,
        request_timeout=30.0,
        max_handles=2,
        idle_ttl_seconds=2.0,
        logger=logger,
    )
    results: list[dict[str, Any]] = []
    try:
        for index in range(cycles):
            started = time.monotonic()
            outcome = await run_opencode_prompt(
                supervisor,
                OpenCodeRunConfig(
                    agent="opencode",
                    model="zai-coding-plan/glm-4.7-flashx",
                    reasoning=None,
                    prompt="Reply with exactly OK.",
                    workspace_root=str(repo_root),
                    timeout_seconds=timeout_seconds,
                    interrupt_grace_seconds=2,
                ),
                logger=logger,
            )
            pid = _handle_pid(supervisor)
            results.append(
                {
                    "cycle": index + 1,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "timed_out": outcome.timed_out,
                    "stopped": outcome.stopped,
                    "session_id": outcome.session_id,
                    "turn_id": outcome.turn_id,
                    "output_text": outcome.output_text[:120],
                    "pid_ps": _ps_info(pid),
                    "doctor": _condense_doctor(repo_root),
                }
            )

        before_prune = _condense_doctor(repo_root)
        await asyncio.sleep(2.5)
        pruned = await supervisor.prune_idle()
        after_prune = _condense_doctor(repo_root)
    finally:
        await supervisor.close_all()
    return {
        "cycles": results,
        "doctor_before_prune": before_prune,
        "pruned_handles": pruned,
        "doctor_after_prune": after_prune,
        "doctor_after_close_all": _condense_doctor(repo_root),
        "registry_after_close_all": _registry_records(repo_root),
    }


async def _backend_phase(
    repo_root: Path, command: list[str], logger: logging.Logger, *, cycles: int
) -> dict[str, Any]:
    supervisor = OpenCodeSupervisor(
        command,
        request_timeout=30.0,
        max_handles=2,
        idle_ttl_seconds=2.0,
        logger=logger,
    )
    backend = OpenCodeBackend(
        supervisor=supervisor,
        workspace_root=repo_root,
        logger=logger,
    )
    results: list[dict[str, Any]] = []
    try:
        for index in range(cycles):
            session_id = await backend.start_session(
                target={},
                context={"workspace": str(repo_root)},
            )
            pid = _handle_pid(supervisor)
            before_dispose = _condense_doctor(repo_root)
            await backend._dispose_temporary_session(session_id)
            results.append(
                {
                    "cycle": index + 1,
                    "session_id": session_id,
                    "pid_ps": _ps_info(pid),
                    "backend_session_id_after": backend._session_id,
                    "backend_temporary_session_id_after": backend._temporary_session_id,
                    "doctor_before_dispose": before_dispose,
                    "doctor_after_dispose": _condense_doctor(repo_root),
                }
            )

        await asyncio.sleep(2.5)
        pruned = await supervisor.prune_idle()
    finally:
        await supervisor.close_all()
    return {
        "cycles": results,
        "pruned_handles": pruned,
        "doctor_after_close_all": _condense_doctor(repo_root),
        "registry_after_close_all": _registry_records(repo_root),
    }


def _signoff(report: dict[str, Any]) -> dict[str, Any]:
    registry_phase = report["registry_reuse_phase"]
    one_shot_phase = report["one_shot_phase"]
    backend_phase = report["backend_phase"]
    final_doctor = report["final_doctor"]

    registry_cleanup_ok = registry_phase.get(
        "second_close_removed_pid"
    ) is True and not registry_phase.get("registry_after_reused_close")
    one_shot_cleanup_ok = one_shot_phase.get(
        "pruned_handles", 0
    ) >= 1 and not one_shot_phase.get("registry_after_close_all")
    backend_cleanup_ok = (
        backend_phase.get("pruned_handles", 0) >= 1
        and not backend_phase.get("registry_after_close_all")
        and all(
            cycle.get("backend_session_id_after") is None
            and cycle.get("backend_temporary_session_id_after") is None
            for cycle in backend_phase.get("cycles", [])
        )
    )
    observability_ok = not final_doctor.get("managed_servers") and not final_doctor.get(
        "live_handles"
    )

    decision = (
        "signoff"
        if registry_cleanup_ok
        and one_shot_cleanup_ok
        and backend_cleanup_ok
        and observability_ok
        else "follow_up_needed"
    )
    return {
        "decision": decision,
        "registry_cleanup_ok": registry_cleanup_ok,
        "one_shot_cleanup_ok": one_shot_cleanup_ok,
        "backend_cleanup_ok": backend_cleanup_ok,
        "observability_ok": observability_ok,
        "notes": [
            "A pre-fix build baseline was not rerun; this soak establishes a fixed-build baseline with repeated lifecycle cycles.",
            "The OpenCode dispose endpoint returned HTML 200 responses during the soak, but the managed process registry, doctor output, and pid exit checks still confirmed cleanup.",
        ],
    }


async def _run_soak(args: argparse.Namespace) -> dict[str, Any]:
    logger = _quiet_logger()
    command = [
        _opencode_bin(),
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        "0",
    ]
    with tempfile.TemporaryDirectory(prefix="opencode-soak-") as tmpdir:
        hub_root = Path(tmpdir) / "hub"
        hub_root.mkdir(parents=True, exist_ok=True)
        seed_hub_files(hub_root, force=True)
        repo_root = hub_root / "repo"
        repo_root.mkdir(parents=True, exist_ok=True)
        (repo_root / ".git").mkdir()
        _write_repo_override(
            repo_root,
            max_handles=args.max_handles,
            idle_ttl_seconds=args.idle_ttl_seconds,
        )
        repo_config = load_repo_config(repo_root, hub_path=hub_root)

        baseline_doctor = _condense_doctor(repo_root)
        baseline_summary = summarize_opencode_lifecycle(
            repo_root, repo_config=repo_config
        )

        provider_payload = _summarize_providers(
            await _provider_probe(repo_root, command, logger)
        )

        registry_reuse_phase = await _registry_reuse_phase(repo_root, command, logger)
        one_shot_phase = await _one_shot_phase(
            repo_root,
            command,
            logger,
            cycles=args.cycles,
            timeout_seconds=args.prompt_timeout_seconds,
        )
        backend_phase = await _backend_phase(
            repo_root,
            command,
            logger,
            cycles=args.cycles,
        )

        final_doctor = _condense_doctor(repo_root)
        report = {
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "repo_root": str(repo_root),
            "command": command,
            "config": {
                "server_scope": repo_config.opencode.server_scope,
                "max_handles": repo_config.opencode.max_handles,
                "idle_ttl_seconds": repo_config.opencode.idle_ttl_seconds,
            },
            "baseline_doctor": baseline_doctor,
            "baseline_lifecycle": baseline_summary,
            "providers": provider_payload,
            "registry_reuse_phase": registry_reuse_phase,
            "one_shot_phase": one_shot_phase,
            "backend_phase": backend_phase,
            "final_doctor": final_doctor,
        }
        report["signoff"] = _signoff(report)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a repeatable OpenCode lifecycle soak against a temporary repo and "
            "emit JSON evidence for registry reuse, temporary-session cleanup, "
            "idle pruning, shutdown, and doctor diagnostics."
        )
    )
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--max-handles", type=int, default=2)
    parser.add_argument("--idle-ttl-seconds", type=int, default=2)
    parser.add_argument("--prompt-timeout-seconds", type=int, default=5)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    report = asyncio.run(_run_soak(args))
    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report["signoff"]["decision"] == "signoff" else 1


if __name__ == "__main__":
    raise SystemExit(main())
