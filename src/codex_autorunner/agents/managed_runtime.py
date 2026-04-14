from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Optional, Protocol, Sequence

from ..core.car_context import CarContextBundle, render_runtime_compat_agents_md
from ..core.utils import atomic_write, subprocess_env

RuntimePreflightStatus = Literal["ready", "missing_binary", "incompatible", "deferred"]
RuntimeLaunchMode = Literal["session_state_file", "workspace_only"]

ZEROCLAW_RUNTIME_ID = "zeroclaw"
ZEROCLAW_SESSION_STATE_FILE_MODE: RuntimeLaunchMode = "session_state_file"
_PROBE_TIMEOUT_SECONDS = 10
_ZEROCLAW_WORKSPACE_ENV = "ZEROCLAW_WORKSPACE"
_ZEROCLAW_CONFIG_DIR_ENV = "ZEROCLAW_CONFIG_DIR"
_ZEROCLAW_CONFIG_DIRNAME = ".zeroclaw"
_COMPAT_AGENTS_FILENAME = "AGENTS.md"


@dataclass(frozen=True)
class RuntimePreflightResult:
    runtime_id: str
    status: RuntimePreflightStatus
    version: Optional[str]
    launch_mode: Optional[RuntimeLaunchMode]
    message: str
    fix: Optional[str] = None


@dataclass(frozen=True)
class RuntimeLaunchSpec:
    runtime_id: str
    command: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    launch_mode: Optional[RuntimeLaunchMode]
    runtime_version: Optional[str] = None
    runtime_workspace_root: Optional[Path] = None
    session_state_file: Optional[Path] = None


class ManagedWorkspaceRuntimeError(RuntimeError):
    def __init__(self, result: RuntimePreflightResult) -> None:
        self.result = result
        text = result.message
        if result.fix:
            text = f"{text} {result.fix}"
        super().__init__(text)


class ManagedWorkspaceRuntimeAdapter(Protocol):
    runtime_id: str

    def preflight(
        self,
        *,
        command: Sequence[str],
        base_env: Optional[Mapping[str, str]] = None,
    ) -> RuntimePreflightResult: ...

    def build_launch_spec(
        self,
        *,
        command: Sequence[str],
        runtime_workspace_root: Path,
        session_state_file: Path,
        base_env: Optional[Mapping[str, str]] = None,
        embed_workspace_env: bool = True,
    ) -> RuntimeLaunchSpec: ...


@dataclass(frozen=True)
class _ProbeResult:
    output: str
    returncode: Optional[int]
    missing_binary: bool = False


def _run_probe(
    command: Sequence[str],
    args: Sequence[str],
    *,
    base_env: Optional[Mapping[str, str]] = None,
) -> _ProbeResult:
    try:
        result = subprocess.run(
            [*command, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
            env=subprocess_env(base_env=base_env),
        )
    except FileNotFoundError:
        return _ProbeResult(output="", returncode=None, missing_binary=True)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return _ProbeResult(output=str(exc), returncode=None)
    return _ProbeResult(
        output="\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part and part.strip()
        ).strip(),
        returncode=result.returncode,
    )


def _first_nonempty_line(text: str) -> Optional[str]:
    for line in text.splitlines():
        normalized = line.strip()
        if normalized:
            return normalized
    return None


def _default_zeroclaw_config_dir(
    *, base_env: Optional[Mapping[str, str]] = None
) -> str:
    env = subprocess_env(base_env=base_env)
    configured = str(env.get(_ZEROCLAW_CONFIG_DIR_ENV) or "").strip()
    if configured:
        return configured
    home_dir = str(env.get("HOME") or "").strip()
    if home_dir:
        return str(Path(home_dir).expanduser() / _ZEROCLAW_CONFIG_DIRNAME)
    return str(Path.home() / _ZEROCLAW_CONFIG_DIRNAME)


def zeroclaw_managed_workspace_env(
    runtime_workspace_root: Path,
    *,
    base_env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    return {
        _ZEROCLAW_WORKSPACE_ENV: str(runtime_workspace_root),
        _ZEROCLAW_CONFIG_DIR_ENV: _default_zeroclaw_config_dir(base_env=base_env),
    }


class _ZeroClawManagedWorkspaceRuntime:
    runtime_id = ZEROCLAW_RUNTIME_ID

    def preflight(
        self,
        *,
        command: Sequence[str],
        base_env: Optional[Mapping[str, str]] = None,
    ) -> RuntimePreflightResult:
        version_probe = _run_probe(command, ["--version"], base_env=base_env)
        if version_probe.missing_binary:
            binary = str(command[0]) if command else ZEROCLAW_RUNTIME_ID
            return RuntimePreflightResult(
                runtime_id=self.runtime_id,
                status="missing_binary",
                version=None,
                launch_mode=None,
                message=f"ZeroClaw binary '{binary}' is not available on PATH.",
                fix=(
                    "Install ZeroClaw on the host or update "
                    "agents.zeroclaw.binary to a working executable path."
                ),
            )
        version = _first_nonempty_line(version_probe.output)

        help_probe = _run_probe(command, ["agent", "--help"], base_env=base_env)
        if help_probe.missing_binary:
            binary = str(command[0]) if command else ZEROCLAW_RUNTIME_ID
            return RuntimePreflightResult(
                runtime_id=self.runtime_id,
                status="missing_binary",
                version=version,
                launch_mode=None,
                message=f"ZeroClaw binary '{binary}' is not available on PATH.",
                fix=(
                    "Install ZeroClaw on the host or update "
                    "agents.zeroclaw.binary to a working executable path."
                ),
            )
        if help_probe.returncode not in {0, None} and not help_probe.output:
            return RuntimePreflightResult(
                runtime_id=self.runtime_id,
                status="incompatible",
                version=version,
                launch_mode=None,
                message=(
                    "CAR could not inspect `zeroclaw agent --help` to verify the "
                    "durable launch contract."
                ),
                fix=(
                    "Run `zeroclaw agent --help` directly and install a ZeroClaw "
                    "build that advertises `--session-state-file`."
                ),
            )
        if "--session-state-file" not in help_probe.output:
            version_hint = version or "unknown version"
            return RuntimePreflightResult(
                runtime_id=self.runtime_id,
                status="incompatible",
                version=version,
                launch_mode=None,
                message=(
                    f"ZeroClaw {version_hint} does not advertise "
                    "`zeroclaw agent --session-state-file` in `zeroclaw agent --help`."
                ),
                fix=(
                    "Install a ZeroClaw build that supports "
                    "`zeroclaw agent --session-state-file`, or keep the agent "
                    "workspace disabled until CAR supports another durable launch mode."
                ),
            )
        return RuntimePreflightResult(
            runtime_id=self.runtime_id,
            status="ready",
            version=version,
            launch_mode=ZEROCLAW_SESSION_STATE_FILE_MODE,
            message=(
                f"ZeroClaw {version or 'version unknown'} supports durable "
                "`--session-state-file` launches."
            ),
        )

    def build_launch_spec(
        self,
        *,
        command: Sequence[str],
        runtime_workspace_root: Path,
        session_state_file: Path,
        base_env: Optional[Mapping[str, str]] = None,
        embed_workspace_env: bool = True,
    ) -> RuntimeLaunchSpec:
        preflight = self.preflight(command=command, base_env=base_env)
        if preflight.status != "ready":
            raise ManagedWorkspaceRuntimeError(preflight)
        env = subprocess_env(base_env=base_env)
        if embed_workspace_env:
            env.update(
                zeroclaw_managed_workspace_env(
                    runtime_workspace_root,
                    base_env=env,
                )
            )
        return RuntimeLaunchSpec(
            runtime_id=self.runtime_id,
            command=tuple(
                [
                    *[str(part) for part in command],
                    "agent",
                    "--session-state-file",
                    str(session_state_file),
                ]
            ),
            cwd=runtime_workspace_root,
            env=env,
            launch_mode=preflight.launch_mode,
            runtime_version=preflight.version,
            runtime_workspace_root=runtime_workspace_root,
            session_state_file=session_state_file,
        )


_ZEROCLAW_ADAPTER = _ZeroClawManagedWorkspaceRuntime()


def get_managed_workspace_runtime_adapter(
    runtime_id: str,
) -> ManagedWorkspaceRuntimeAdapter:
    normalized = str(runtime_id or "").strip().lower()
    if normalized == ZEROCLAW_RUNTIME_ID:
        return _ZEROCLAW_ADAPTER
    raise ValueError(f"Unknown managed workspace runtime: {runtime_id!r}")


def preflight_managed_workspace_runtime(
    runtime_id: str,
    *,
    command: Sequence[str],
    base_env: Optional[Mapping[str, str]] = None,
) -> RuntimePreflightResult:
    return get_managed_workspace_runtime_adapter(runtime_id).preflight(
        command=command,
        base_env=base_env,
    )


def build_managed_workspace_launch_spec(
    runtime_id: str,
    *,
    command: Sequence[str],
    runtime_workspace_root: Path,
    session_state_file: Path,
    base_env: Optional[Mapping[str, str]] = None,
    embed_workspace_env: bool = True,
) -> RuntimeLaunchSpec:
    return get_managed_workspace_runtime_adapter(runtime_id).build_launch_spec(
        command=command,
        runtime_workspace_root=runtime_workspace_root,
        session_state_file=session_state_file,
        base_env=base_env,
        embed_workspace_env=embed_workspace_env,
    )


def known_agent_workspace_runtime_ids() -> tuple[str, ...]:
    return (ZEROCLAW_RUNTIME_ID,)


def sync_managed_workspace_compat_files(
    runtime_id: str,
    *,
    runtime_workspace_root: Path,
    bundle: CarContextBundle,
) -> None:
    normalized_runtime = str(runtime_id or "").strip().lower()
    if normalized_runtime != ZEROCLAW_RUNTIME_ID:
        return
    runtime_workspace_root.mkdir(parents=True, exist_ok=True)
    agents_path = runtime_workspace_root / _COMPAT_AGENTS_FILENAME
    rendered = render_runtime_compat_agents_md(bundle)
    if rendered:
        atomic_write(agents_path, rendered)
        return
    try:
        agents_path.unlink()
    except FileNotFoundError:
        pass


def preflight_agent_workspace_runtime(
    *,
    config,
    workspace,
) -> RuntimePreflightResult:
    from ..core.destinations import resolve_effective_agent_workspace_destination

    runtime_id = str(getattr(workspace, "runtime", "") or "").strip().lower()
    if not runtime_id:
        return RuntimePreflightResult(
            runtime_id="",
            status="incompatible",
            version=None,
            launch_mode=None,
            message="Agent workspace runtime is not configured.",
            fix="Set a supported runtime id on the agent workspace manifest entry.",
        )

    resolution = resolve_effective_agent_workspace_destination(workspace)
    if resolution.destination.kind != "local":
        return RuntimePreflightResult(
            runtime_id=runtime_id,
            status="deferred",
            version=None,
            launch_mode=None,
            message=(
                "Runtime compatibility for non-local agent-workspace destinations "
                "is deferred until launch."
            ),
            fix=(
                "Run `car doctor` from the hub directory (or pass `--repo`) and validate the runtime contract again "
                "when launching the first managed thread."
            ),
        )

    try:
        binary = config.agent_binary(runtime_id).strip()
    except Exception:  # intentional: config.agent_binary may raise from unknown impl
        binary = ""
    if not binary:
        return RuntimePreflightResult(
            runtime_id=runtime_id,
            status="missing_binary",
            version=None,
            launch_mode=None,
            message=f"{runtime_id} binary is not configured.",
            fix=f"Set agents.{runtime_id}.binary in the hub config.",
        )
    return preflight_managed_workspace_runtime(runtime_id, command=[binary])


def probe_agent_workspace_runtime(
    *,
    hub_config,
    workspace,
) -> RuntimePreflightResult:
    return preflight_agent_workspace_runtime(config=hub_config, workspace=workspace)


__all__ = [
    "ManagedWorkspaceRuntimeAdapter",
    "ManagedWorkspaceRuntimeError",
    "RuntimeLaunchSpec",
    "RuntimePreflightResult",
    "ZEROCLAW_RUNTIME_ID",
    "ZEROCLAW_SESSION_STATE_FILE_MODE",
    "build_managed_workspace_launch_spec",
    "get_managed_workspace_runtime_adapter",
    "known_agent_workspace_runtime_ids",
    "preflight_agent_workspace_runtime",
    "preflight_managed_workspace_runtime",
    "probe_agent_workspace_runtime",
    "sync_managed_workspace_compat_files",
    "zeroclaw_managed_workspace_env",
]
