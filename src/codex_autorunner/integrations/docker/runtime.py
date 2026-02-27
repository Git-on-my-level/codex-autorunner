from __future__ import annotations

import dataclasses
import datetime as dt
import fnmatch
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from ...core.destinations import DockerReadiness, probe_docker_readiness
from ...core.utils import subprocess_env

logger = logging.getLogger("codex_autorunner.integrations.docker.runtime")


class DockerRuntimeError(RuntimeError):
    """Raised when a docker command fails."""


class DockerUnavailableError(DockerRuntimeError):
    """Raised when docker is not installed or cannot be executed."""


RunFn = Callable[..., subprocess.CompletedProcess[str]]


@dataclasses.dataclass(frozen=True)
class DockerMount:
    source: str
    target: str
    read_only: bool = False

    def to_bind_spec(self) -> str:
        mode = ":ro" if self.read_only else ""
        return f"{self.source}:{self.target}{mode}"


@dataclasses.dataclass(frozen=True)
class DockerContainerSpec:
    name: str
    image: str
    mounts: tuple[DockerMount, ...]
    env: dict[str, str]
    workdir: str


def _expand_template(value: str, *, repo_root: Path, home_dir: Path) -> str:
    out = str(value)
    out = out.replace("${REPO_ROOT}", str(repo_root.resolve()))
    out = out.replace("${HOME}", str(home_dir))
    return out


def select_passthrough_env(
    patterns: Sequence[str],
    *,
    source_env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    src = source_env or {}
    selected: dict[str, str] = {}
    normalized_patterns = [str(p).strip() for p in patterns if str(p).strip()]
    if not normalized_patterns:
        return selected
    for key in sorted(src.keys()):
        if not isinstance(key, str):
            continue
        value = src.get(key)
        if value is None:
            continue
        for pattern in normalized_patterns:
            if fnmatch.fnmatchcase(key, pattern):
                selected[key] = value
                break
    return selected


def normalize_mounts(
    repo_root: Path,
    mounts: Optional[Sequence[DockerMount | Mapping[str, Any]]] = None,
) -> tuple[DockerMount, ...]:
    repo_path = str(repo_root.resolve())
    normalized: list[DockerMount] = [DockerMount(source=repo_path, target=repo_path)]
    seen = {(repo_path, repo_path, False)}
    home_dir = Path.home()
    for mount in mounts or ():
        if isinstance(mount, DockerMount):
            candidate = mount
        elif isinstance(mount, Mapping):
            source = mount.get("source")
            target = mount.get("target")
            if not isinstance(source, str) or not source.strip():
                continue
            if not isinstance(target, str) or not target.strip():
                continue
            read_only = bool(mount.get("read_only", False))
            candidate = DockerMount(
                source=_expand_template(
                    source.strip(), repo_root=repo_root, home_dir=home_dir
                ),
                target=_expand_template(
                    target.strip(), repo_root=repo_root, home_dir=home_dir
                ),
                read_only=read_only,
            )
        else:
            continue
        key = (candidate.source, candidate.target, candidate.read_only)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(candidate)
    return tuple(normalized)


def build_docker_container_spec(
    *,
    name: str,
    image: str,
    repo_root: Path,
    mounts: Optional[Sequence[DockerMount | Mapping[str, Any]]] = None,
    env_passthrough_patterns: Optional[Sequence[str]] = None,
    explicit_env: Optional[Mapping[str, str]] = None,
    source_env: Optional[Mapping[str, str]] = None,
    workdir: Optional[str] = None,
) -> DockerContainerSpec:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("container name is required")
    if not isinstance(image, str) or not image.strip():
        raise ValueError("docker image is required")

    repo_abs = repo_root.resolve()
    home_dir = Path.home()
    passthrough = select_passthrough_env(
        env_passthrough_patterns or (),
        source_env=source_env,
    )
    merged_env = dict(sorted(passthrough.items()))
    for key, value in (explicit_env or {}).items():
        if not isinstance(key, str) or not key.strip():
            continue
        if value is None:
            continue
        merged_env[key.strip()] = str(value)
    return DockerContainerSpec(
        name=name.strip(),
        image=image.strip(),
        mounts=normalize_mounts(repo_abs, mounts),
        env=merged_env,
        workdir=(
            _expand_template(str(workdir), repo_root=repo_abs, home_dir=home_dir)
            if workdir
            else str(repo_abs)
        ),
    )


class DockerRuntime:
    def __init__(
        self,
        *,
        docker_binary: str = "docker",
        run_fn: RunFn = subprocess.run,
    ) -> None:
        self._docker_binary = docker_binary
        self._run_fn = run_fn

    def _run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        timeout_seconds: Optional[float] = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self._docker_binary, *[str(a) for a in args]]
        try:
            proc = self._run_fn(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=subprocess_env(),
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise DockerUnavailableError(
                f"Docker binary '{self._docker_binary}' not found"
            ) from exc
        if check and proc.returncode != 0:
            details = (proc.stderr or proc.stdout or "").strip() or "unknown error"
            raise DockerRuntimeError(
                f"Docker command failed ({proc.returncode}): {' '.join(cmd)} :: {details}"
            )
        return proc

    def is_available(self) -> bool:
        return self.probe_readiness().binary_available

    def probe_readiness(self, *, timeout_seconds: float = 10.0) -> DockerReadiness:
        return probe_docker_readiness(
            docker_binary=self._docker_binary,
            run_fn=self._run_fn,
            timeout_seconds=timeout_seconds,
        )

    def ensure_container_running(self, spec: DockerContainerSpec) -> None:
        inspect_proc = self._run(
            ["inspect", "--format", "{{.State.Running}}", spec.name],
            check=False,
            timeout_seconds=15,
        )
        if inspect_proc.returncode == 0:
            running = (inspect_proc.stdout or "").strip().lower() == "true"
            if running:
                return
            self._run(["start", spec.name], timeout_seconds=30)
            return

        details = (inspect_proc.stderr or inspect_proc.stdout or "").lower()
        if "no such object" not in details and "no such container" not in details:
            raise DockerRuntimeError(
                f"Unable to inspect container {spec.name}: "
                f"{(inspect_proc.stderr or inspect_proc.stdout or '').strip()}"
            )

        cmd: list[str] = [
            "run",
            "-d",
            "--name",
            spec.name,
            "--label",
            "ca.managed=true",
        ]
        for mount in spec.mounts:
            cmd.extend(["-v", mount.to_bind_spec()])
        for key, value in sorted(spec.env.items()):
            cmd.extend(["-e", f"{key}={value}"])
        if spec.workdir:
            cmd.extend(["-w", spec.workdir])
        cmd.extend([spec.image, "tail", "-f", "/dev/null"])
        run_proc = self._run(cmd, check=False, timeout_seconds=120)
        if run_proc.returncode == 0:
            return
        run_details = (run_proc.stderr or run_proc.stdout or "").lower()
        if "already in use by container" in run_details:
            self._run(["start", spec.name], timeout_seconds=30)
            return
        raise DockerRuntimeError(
            f"Failed to create container {spec.name}: "
            f"{(run_proc.stderr or run_proc.stdout or '').strip()}"
        )

    def build_exec_command(
        self,
        container_name: str,
        command: Sequence[str],
        *,
        workdir: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> list[str]:
        if not command:
            raise ValueError("command must not be empty")
        cmd: list[str] = [self._docker_binary, "exec"]
        if workdir:
            cmd.extend(["-w", str(workdir)])
        for key, value in sorted((env or {}).items()):
            if not key:
                continue
            cmd.extend(["-e", f"{key}={value}"])
        cmd.append(container_name)
        cmd.extend([str(part) for part in command])
        return cmd

    def run_exec(
        self,
        container_name: str,
        command: Sequence[str],
        *,
        workdir: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout_seconds: Optional[float] = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        args = self.build_exec_command(
            container_name,
            command,
            workdir=workdir,
            env=env,
        )
        try:
            proc = self._run_fn(
                args,
                capture_output=True,
                text=True,
                check=False,
                env=subprocess_env(),
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise DockerUnavailableError(
                f"Docker binary '{self._docker_binary}' not found"
            ) from exc
        if check and proc.returncode != 0:
            details = (proc.stderr or proc.stdout or "").strip() or "unknown error"
            raise DockerRuntimeError(
                "Docker exec failed "
                f"({proc.returncode}) in {container_name}: {details}"
            )
        return proc

    def stop_container(
        self,
        container_name: str,
        *,
        remove: bool = True,
        timeout_seconds: int = 10,
    ) -> bool:
        inspect_proc = self._run(
            ["inspect", "--format", "{{.State.Running}}", container_name],
            check=False,
            timeout_seconds=15,
        )
        if inspect_proc.returncode != 0:
            details = (inspect_proc.stderr or inspect_proc.stdout or "").lower()
            if "no such object" in details or "no such container" in details:
                return False
            raise DockerRuntimeError(
                f"Unable to inspect container {container_name}: "
                f"{(inspect_proc.stderr or inspect_proc.stdout or '').strip()}"
            )

        running = (inspect_proc.stdout or "").strip().lower() == "true"
        if running:
            self._run(
                ["stop", "-t", str(timeout_seconds), container_name],
                timeout_seconds=max(10, timeout_seconds + 5),
            )
        if remove:
            self._run(
                ["rm", "-f", container_name],
                timeout_seconds=30,
            )
        return True

    def reap_container_if_expired(
        self,
        container_name: str,
        *,
        ttl_seconds: int,
        now: Optional[dt.datetime] = None,
    ) -> bool:
        if ttl_seconds <= 0:
            return False

        started_proc = self._run(
            ["inspect", "--format", "{{.State.StartedAt}}", container_name],
            check=False,
            timeout_seconds=15,
        )
        if started_proc.returncode != 0:
            details = (started_proc.stderr or started_proc.stdout or "").lower()
            if "no such object" in details or "no such container" in details:
                return False
            raise DockerRuntimeError(
                f"Unable to inspect container {container_name} start time: "
                f"{(started_proc.stderr or started_proc.stdout or '').strip()}"
            )

        started_raw = (started_proc.stdout or "").strip()
        if not started_raw:
            return False
        try:
            started_at = dt.datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning(
                "Invalid docker started-at value for %s: %r",
                container_name,
                started_raw,
            )
            return False
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=dt.timezone.utc)
        now_dt = now or dt.datetime.now(dt.timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=dt.timezone.utc)
        age_seconds = (now_dt - started_at).total_seconds()
        if age_seconds < float(ttl_seconds):
            return False

        self.stop_container(container_name, remove=True)
        return True


__all__ = [
    "DockerContainerSpec",
    "DockerMount",
    "DockerReadiness",
    "DockerRuntime",
    "DockerRuntimeError",
    "DockerUnavailableError",
    "build_docker_container_spec",
    "normalize_mounts",
    "select_passthrough_env",
]
