#!/usr/bin/env python3
"""Idle CPU soak harness for CAR service commands.

Reads profiles from scripts/idle_cpu_profiles.yml, starts the target
service(s) against a disposable root, samples CPU/RSS during the
observation window, and writes durable JSON artifacts under
.codex-autorunner/diagnostics/idle-cpu/.

Usage:
    python scripts/idle_cpu_soak.py --profile hub_only
    python scripts/idle_cpu_soak.py --profile hub_only --output /tmp/result.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
_PROFILES_PATH = _SCRIPT_DIR / "idle_cpu_profiles.yml"

sys.path.insert(0, str(_REPO_ROOT / "src"))

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import REPO_OVERRIDE_FILENAME
from codex_autorunner.core.diagnostics.cpu_sampler import (
    CpuSample,
    aggregate_samples,
    collect_cpu_sample,
    compute_per_process_aggregates,
    evaluate_signoff,
)
from codex_autorunner.core.diagnostics.process_snapshot import ProcessCategory
from codex_autorunner.core.utils import atomic_write


def _car_bin() -> list[str]:
    return [sys.executable, "-m", "codex_autorunner.cli"]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _load_profile(name: str) -> dict[str, Any]:
    if not _PROFILES_PATH.is_file():
        raise FileNotFoundError(f"Profiles file not found: {_PROFILES_PATH}")
    data = yaml.safe_load(_PROFILES_PATH.read_text(encoding="utf-8"))
    profiles = data.get("profiles", {})
    if name not in profiles:
        available = ", ".join(sorted(profiles.keys()))
        raise ValueError(f"Unknown profile {name!r}. Available: {available}")
    defaults = data.get("defaults", {})
    profile = profiles[name]
    profile.setdefault("warmup_seconds", defaults.get("warmup_seconds", 30))
    profile.setdefault("duration_seconds", defaults.get("duration_seconds", 300))
    profile.setdefault(
        "sample_interval_seconds",
        defaults.get("sample_interval_seconds", 5),
    )
    profile.setdefault("health_probe", defaults.get("health_probe", {}))
    profile.setdefault(
        "owned_process_categories",
        defaults.get("owned_process_categories", ["car_service"]),
    )
    return profile


def _write_repo_override(repo_root: Path) -> None:
    override_path = repo_root / REPO_OVERRIDE_FILENAME
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text(
        yaml.safe_dump(
            {
                "server": {"port": 0},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _seed_disposable_root(tmpdir: str) -> Path:
    hub_root = Path(tmpdir) / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    seed_hub_files(hub_root, force=True)
    repo_root = hub_root / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / ".git").mkdir()
    _write_repo_override(repo_root)
    return hub_root


def _health_probe(
    base_url: str,
    probe_config: dict[str, Any],
    logger: logging.Logger,
) -> dict[str, Any]:
    path = probe_config.get("path", "/health")
    timeout = probe_config.get("timeout_seconds", 5)
    retries = probe_config.get("retries", 3)
    url = f"{base_url}{path}"
    total = 0
    successful = 0
    failed = 0
    last_status: Optional[int] = None
    for attempt in range(retries):
        total += 1
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                last_status = resp.status
                successful += 1
                logger.debug("health probe ok: %s (attempt %d)", url, attempt + 1)
                break
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            failed += 1
            last_status = getattr(getattr(exc, "code", None), "__int__", lambda: None)()
            if isinstance(exc, urllib.error.HTTPError):
                last_status = exc.code
            logger.debug(
                "health probe failed: %s (attempt %d): %s", url, attempt + 1, exc
            )
    return {
        "total_probes": total,
        "successful_probes": successful,
        "failed_probes": failed,
        "last_probe_status": last_status,
    }


def _wait_for_health(
    base_url: str,
    probe_config: dict[str, Any],
    timeout: float,
    logger: logging.Logger,
) -> bool:
    path = probe_config.get("path", "/health")
    url = f"{base_url}{path}"
    probe_timeout = probe_config.get("timeout_seconds", 5)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=probe_timeout) as resp:
                if resp.status == 200:
                    logger.info("health check passed: %s", url)
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass
        time.sleep(0.5)
    logger.error("health check timed out after %.1fs: %s", timeout, url)
    return False


def _parse_services(profile: dict[str, Any], port: int) -> list[dict[str, Any]]:
    services = []
    car_bin = _car_bin()
    for svc in profile.get("services", []):
        cmd_str = svc["command"]
        alias = svc.get("alias", "service")
        parts = cmd_str.split()
        if parts[0:2] == ["car", "hub"] and "serve" in parts:
            cmd = car_bin + ["hub", "serve", "--port", str(port)]
        elif parts[0:2] == ["car", "discord"] and "serve" in parts:
            cmd = car_bin + ["discord", "serve"]
        elif parts[0:2] == ["car", "telegram"] and "serve" in parts:
            cmd = car_bin + ["telegram", "serve"]
        else:
            cmd = car_bin + parts[1:]
        services.append({"alias": alias, "command": cmd, "original": cmd_str})
    return services


def _collect_service_pids(
    procs: list[subprocess.Popen], logger: logging.Logger
) -> list[int]:
    pids: list[int] = []
    for proc in procs:
        service_pids: list[int] = []
        try:
            sid = os.getsid(proc.pid)
            result = subprocess.run(
                ["ps", "-A", "-o", "pid=", "-o", "sid="],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) == 2 and parts[1].isdigit():
                        if int(parts[1]) == sid and parts[0].isdigit():
                            service_pids.append(int(parts[0]))
            if not service_pids:
                logger.warning(
                    "no session peers found for pid=%d sid=%d; "
                    "falling back to descendant scan",
                    proc.pid,
                    sid,
                )
                result = subprocess.run(
                    ["ps", "-o", "pid=", "-o", "ppid="],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode == 0:
                    pp_to_pid: dict[int, list[int]] = {}
                    for line in result.stdout.splitlines():
                        parts = line.strip().split()
                        if (
                            len(parts) == 2
                            and parts[0].isdigit()
                            and parts[1].isdigit()
                        ):
                            pp_to_pid.setdefault(int(parts[1]), []).append(
                                int(parts[0])
                            )
                    queue = [proc.pid]
                    visited = set()
                    while queue:
                        p = queue.pop(0)
                        if p in visited:
                            continue
                        visited.add(p)
                        service_pids.append(p)
                        queue.extend(pp_to_pid.get(p, []))
        except ProcessLookupError:
            service_pids.append(proc.pid)
        pids.extend(service_pids)
    return list(set(pids))


def _start_service(
    svc: dict[str, Any],
    hub_root: Path,
    logger: logging.Logger,
) -> subprocess.Popen:
    cmd = list(svc["command"])
    if "hub" in cmd and "serve" in cmd:
        cmd.extend(["--path", str(hub_root)])
    env = os.environ.copy()
    env["CAR_HUB_ROOT"] = str(hub_root)
    env["CAR_DEV_INCLUDE_ROOT_REPO"] = "1"
    logger.info("starting service: %s (%s)", svc["alias"], " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        preexec_fn=os.setsid,
    )
    return proc


def _stop_service(proc: subprocess.Popen, logger: logging.Logger) -> None:
    if proc.poll() is not None:
        return
    logger.info("stopping service pid=%d", proc.pid)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _collect_environment() -> dict[str, Any]:
    git_sha: Optional[str] = None
    git_dirty = False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(_REPO_ROOT),
        )
        if result.returncode == 0:
            git_sha = result.stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(_REPO_ROOT),
        )
        if status.returncode == 0 and status.stdout.strip():
            git_dirty = True
    except OSError:
        pass

    total_memory_gb = 0.0
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip().isdigit():
            total_memory_gb = int(result.stdout.strip()) / (1024**3)
    except OSError:
        pass

    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "car_version": "dev",
        "car_git_ref": f"{git_sha}{'-dirty' if git_dirty else ''}" if git_sha else None,
        "cpu_count": os.cpu_count() or 0,
        "total_memory_gb": round(total_memory_gb, 2),
    }


def _category_names_to_enums(
    names: list[str],
) -> list[ProcessCategory]:
    mapping = {
        "car_service": ProcessCategory.CAR_SERVICE,
        "opencode": ProcessCategory.OPENCODE,
        "app_server": ProcessCategory.APP_SERVER,
    }
    return [mapping[n] for n in names if n in mapping]


def _write_artifacts(
    artifact: dict[str, Any],
    profile_name: str,
    output_root: Optional[Path],
    logger: logging.Logger,
) -> Path:
    if output_root is None:
        output_root = _REPO_ROOT / ".codex-autorunner" / "diagnostics" / "idle-cpu"

    output_root.mkdir(parents=True, exist_ok=True)

    latest_path = output_root / "latest.json"
    atomic_write(latest_path, json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    logger.info("wrote %s", latest_path)

    history_dir = output_root / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    ts = (
        artifact.get("started_at", "")
        .replace(":", "")
        .replace("-", "")
        .replace("T", "-")
    )
    ts = ts.replace("Z", "")
    history_path = history_dir / f"{ts}-{profile_name}.json"
    atomic_write(history_path, json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    logger.info("wrote %s", history_path)

    return latest_path


def _print_summary(artifact: dict[str, Any]) -> None:
    agg = artifact.get("aggregate_metrics", {})
    signoff = artifact.get("signoff", {})
    env = artifact.get("environment", {})
    print("=" * 60)
    print("IDLE CPU SOAK SUMMARY")
    print("=" * 60)
    print(f"  Profile:    {artifact.get('profile', '?')}")
    print(f"  Git ref:    {env.get('car_git_ref', 'unknown')}")
    print(f"  Platform:   {env.get('platform', 'unknown')}")
    print(f"  Python:     {env.get('python_version', 'unknown')}")
    print(f"  Samples:    {agg.get('car_owned_cpu_samples', 0)}")
    print(f"  CPU mean:   {agg.get('car_owned_cpu_mean_percent', 0):.3f}%")
    print(f"  CPU p95:    {agg.get('car_owned_cpu_p95_percent', 0):.3f}%")
    print(f"  CPU max:    {agg.get('car_owned_cpu_max_percent', 0):.3f}%")
    print(f"  RSS mean:   {agg.get('car_owned_memory_mean_mb', 0):.1f} MB")
    print(f"  RSS max:    {agg.get('car_owned_memory_max_mb', 0):.1f} MB")
    print("-" * 60)
    signoff_passed = signoff.get("passed", False)
    print(
        f"  SIGNOFF:    {'PASS' if signoff_passed else 'FAIL'} "
        f"({signoff.get('budget_type', 'none')}: "
        f"{signoff.get('message', '')})"
    )
    print("=" * 60)


def run_soak(
    profile_name: str,
    output: Optional[Path] = None,
    artifact_dir: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
) -> dict[str, Any]:
    if logger is None:
        logger = logging.getLogger("idle_cpu_soak")

    profile = _load_profile(profile_name)
    warmup = profile.get("warmup_seconds", 30)
    duration = profile.get("duration_seconds", 300)
    interval = profile.get("sample_interval_seconds", 5)
    probe_config = profile.get("health_probe", {})
    owned_cats = profile.get("owned_process_categories", ["car_service"])
    cat_enums = _category_names_to_enums(owned_cats)

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with tempfile.TemporaryDirectory(prefix="idle-cpu-soak-") as tmpdir:
        hub_root = _seed_disposable_root(tmpdir)
        repo_root = hub_root / "repo"

        services = _parse_services(profile, port)
        procs: list[subprocess.Popen] = []

        try:
            for svc in services:
                proc = _start_service(svc, hub_root, logger)
                procs.append(proc)
                svc["pid"] = proc.pid

            health_ok = _wait_for_health(
                base_url, probe_config, timeout=60.0, logger=logger
            )
            if not health_ok:
                for proc in procs:
                    _stop_service(proc, logger)
                raise RuntimeError(
                    f"Service(s) failed to become healthy at {base_url}/health"
                )

            logger.info("warming up for %ds ...", warmup)
            time.sleep(warmup)

            service_pids = _collect_service_pids(procs, logger)
            logger.info("restricting measurement to PIDs: %s", service_pids)

            logger.info("sampling for %ds (interval=%ds) ...", duration, interval)
            samples: list[CpuSample] = []
            health_probes: list[dict[str, Any]] = []
            deadline = time.monotonic() + duration

            while time.monotonic() < deadline:
                sample = collect_cpu_sample(
                    repo_root=repo_root,
                    owned_categories=cat_enums,
                    restrict_pids=service_pids,
                )
                samples.append(sample)
                probe_result = _health_probe(base_url, probe_config, logger)
                health_probes.append(probe_result)
                remaining = deadline - time.monotonic()
                if remaining > interval:
                    time.sleep(interval)
                elif remaining > 0:
                    time.sleep(remaining)

        finally:
            for proc in procs:
                _stop_service(proc, logger)

        finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    agg = aggregate_samples(samples)
    per_process = compute_per_process_aggregates(samples)

    for svc in services:
        for pp in per_process:
            if svc.get("alias") and not pp.get("alias"):
                pp["alias"] = svc["alias"]

    last_probe = (
        health_probes[-1]
        if health_probes
        else {
            "total_probes": 0,
            "successful_probes": 0,
            "failed_probes": 0,
            "last_probe_status": None,
        }
    )

    signoff = evaluate_signoff(
        agg,
        hard_budget=profile.get("hard_budget"),
        comparison_budget=profile.get("comparison_budget"),
    )

    artifact: dict[str, Any] = {
        "version": 1,
        "profile": profile_name,
        "started_at": started_at,
        "finished_at": finished_at,
        "environment": _collect_environment(),
        "services": [
            {
                "alias": s.get("alias", ""),
                "command": s.get("original", ""),
                "pid": s.get("pid"),
            }
            for s in services
        ],
        "config": {
            "warmup_seconds": warmup,
            "duration_seconds": duration,
            "sample_interval_seconds": interval,
        },
        "aggregate_metrics": agg,
        "per_process_metrics": per_process,
        "health_probe_summary": {
            "total_probes": sum(p["total_probes"] for p in health_probes),
            "successful_probes": sum(p["successful_probes"] for p in health_probes),
            "failed_probes": sum(p["failed_probes"] for p in health_probes),
            "last_probe_status": last_probe.get("last_probe_status"),
        },
        "signoff": signoff,
    }

    _write_artifacts(artifact, profile_name, artifact_dir, logger)

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    _print_summary(artifact)
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run an idle CPU soak against a CAR service profile and "
            "write a JSON artifact set."
        )
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="Profile name from scripts/idle_cpu_profiles.yml",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write the JSON artifact",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=None,
        help="Override artifact directory (default: .codex-autorunner/diagnostics/idle-cpu/)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("idle_cpu_soak")

    try:
        artifact = run_soak(
            profile_name=args.profile,
            output=args.output,
            artifact_dir=args.artifact_dir,
            logger=logger,
        )
    except Exception as exc:
        logger.error("soak failed: %s", exc, exc_info=True)
        return 1

    return 0 if artifact.get("signoff", {}).get("passed", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
