"""UpdateEngine: staged install, cutover, restart, health, and rollback orchestration."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from ..update_paths import resolve_update_paths
from ..update_targets import get_update_target_definition, normalize_update_target
from ..update_transaction import (
    restore_orchestration_db_snapshot,
    snapshot_orchestration_db_transaction,
)
from ..utils import resolve_executable
from .cutover import CutoverManager
from .detect import (
    SupervisorIdentity,
    detect_supervisor_identity,
    guard_self_update,
    resolve_backend,
    verify_cutover_routing,
)
from .health import HealthChecker, wait_hub_before_chat
from .install import (
    StagedInstaller,
    StagedInstallError,
    ensure_playwright_chromium,
    select_pip_extras,
)
from .launchd import (
    LaunchdPlistManager,
)
from .launchd import (
    discord_state as _launchd_discord_state,
)
from .launchd import (
    telegram_state as _launchd_telegram_state,
)
from .lock import UpdateInProgressError, acquire_lock, release_lock
from .source import prepare_update_source
from .status import StatusReporter
from .supervisors import (
    CommandAdapter,
    LaunchdManagedAdapter,
    LayeredSupervisor,
    SignalAdapter,
    UpdateServices,
    build_layered_supervisor,
)

_CMD_TIMEOUT_SECONDS = 300
_DEFAULT_PIPX_ROOT = Path("~/.local/pipx")
_DEFAULT_CURRENT_VENV_LINK = _DEFAULT_PIPX_ROOT / "venvs" / "codex-autorunner.current"
_DEFAULT_PREV_VENV_LINK = _DEFAULT_PIPX_ROOT / "venvs" / "codex-autorunner.prev"
_DEFAULT_LOCAL_BIN = Path("~/.local/bin")
_DEFAULT_SNAPSHOT_MAX_COUNT = 1
_DEFAULT_KEEP_OLD_VENVS = 3


@dataclass(frozen=True)
class PipxLayout:
    pipx_root: Path
    current_link: Path
    prev_link: Path
    active_venv: Path
    candidates: tuple[Path, ...] = ()

    def __iter__(self):
        yield self.pipx_root
        yield self.current_link
        yield self.prev_link


@dataclass
class UpdateEngineConfig:
    repo_url: str
    repo_ref: str
    update_dir: Path
    update_target: str = "all"
    update_backend: str = "auto"
    skip_checks: bool = True
    linux_service_names: Mapping[str, str] = field(default_factory=dict)
    restart_command: Sequence[str] | str | None = None
    systemctl_sudo: str | bool = "auto"
    allow_in_place: bool = False
    identity_hint: dict[str, Any] | None = None
    helper_python: str | None = None
    server_host: str = "127.0.0.1"
    server_port: int = 4173
    server_base_path: str = ""
    wait_hub_health_before_chat: bool = True


def _required_commands(backend: str) -> tuple[str, ...]:
    base = ("git", "curl")
    if backend == "launchd":
        return (*base, "launchctl")
    if backend.startswith("systemd"):
        return (*base, "systemctl")
    return base


def _services_for_target(target: str) -> UpdateServices:
    definition = get_update_target_definition(target)
    value = definition.value
    return UpdateServices(
        restart_hub=definition.includes_web,
        restart_telegram=value in {"all", "chat", "telegram"},
        restart_discord=value in {"all", "chat", "discord"},
    )


def _resolve_linux_service_names(
    identity: SupervisorIdentity,
    configured: Mapping[str, str],
) -> dict[str, str]:
    defaults = {
        "hub": identity.unit_name or "car-hub",
        "telegram": "car-telegram",
        "discord": "car-discord",
    }
    merged = dict(defaults)
    for key in ("hub", "telegram", "discord"):
        value = configured.get(key)
        if isinstance(value, str) and value.strip():
            merged[key] = value.strip()
    return merged


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return Path(raw).expanduser()


def _venv_from_bin_path(path: Path) -> Path | None:
    """Return the enclosing pipx codex-autorunner venv for a bin path."""
    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        resolved = path.expanduser()
    if resolved.parent.name != "bin":
        return None
    venv = resolved.parent.parent
    if venv.name != "codex-autorunner":
        return None
    if venv.parent.name != "venvs":
        return None
    return venv


def _looks_like_codex_venv(path: Path) -> bool:
    python_bin = path / "bin" / "python"
    return path.is_dir() and python_bin.exists() and os.access(python_bin, os.X_OK)


def _detect_active_codex_venv(
    car_wrapper_path: Path,
) -> tuple[Path | None, tuple[Path, ...]]:
    candidates: list[Path] = []

    executable = Path(sys.executable).expanduser() if sys.executable else None
    if executable is not None:
        executable_venv = _venv_from_bin_path(executable)
        if executable_venv is not None:
            candidates.append(executable_venv)

    wrapper_venv = _venv_from_bin_path(car_wrapper_path)
    if wrapper_venv is not None:
        candidates.append(wrapper_venv)

    default_roots = (
        Path("~/.local/share/pipx").expanduser(),
        _DEFAULT_PIPX_ROOT.expanduser(),
    )
    for root in default_roots:
        candidates.append(root / "venvs" / "codex-autorunner")

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)

    for candidate in deduped:
        if _looks_like_codex_venv(candidate):
            return candidate, tuple(deduped)
    return None, tuple(deduped)


def resolve_pipx_layout() -> PipxLayout:
    car_wrapper_path = _resolve_car_wrapper_path()
    explicit_pipx_venv = _env_path("PIPX_VENV")
    detected_venv, candidates = _detect_active_codex_venv(car_wrapper_path)
    active_venv = explicit_pipx_venv or detected_venv

    explicit_pipx_root = _env_path("PIPX_ROOT")
    if explicit_pipx_root is not None:
        pipx_root = explicit_pipx_root
        if explicit_pipx_venv is None:
            active_venv = pipx_root / "venvs" / "codex-autorunner"
    elif active_venv is not None and active_venv.parent.name == "venvs":
        pipx_root = active_venv.parent.parent
    else:
        pipx_root = _DEFAULT_PIPX_ROOT.expanduser()

    current = Path(
        os.environ.get(
            "CURRENT_VENV_LINK", str(pipx_root / "venvs" / "codex-autorunner.current")
        )
    ).expanduser()
    prev = Path(
        os.environ.get(
            "PREV_VENV_LINK", str(pipx_root / "venvs" / "codex-autorunner.prev")
        )
    ).expanduser()
    active_venv = active_venv or pipx_root / "venvs" / "codex-autorunner"
    return PipxLayout(
        pipx_root=pipx_root,
        current_link=current,
        prev_link=prev,
        active_venv=active_venv,
        candidates=candidates,
    )


def _resolve_venv_paths() -> tuple[Path, Path, Path]:
    layout = resolve_pipx_layout()
    return layout.pipx_root, layout.current_link, layout.prev_link


def _resolve_car_wrapper_path() -> Path:
    local_bin = Path(os.environ.get("LOCAL_BIN", str(_DEFAULT_LOCAL_BIN))).expanduser()
    return Path(os.environ.get("CAR_WRAPPER_PATH", str(local_bin / "car"))).expanduser()


def _resolve_python_bin(helper_python: str | None) -> str:
    if helper_python and Path(helper_python).exists():
        return helper_python
    if sys.executable:
        return sys.executable
    return "python3"


class UpdateEngine:
    """Orchestrates source prep, staged install, cutover, restart, and rollback."""

    def __init__(
        self,
        config: UpdateEngineConfig,
        *,
        logger: logging.Logger,
        status_path: Path | None = None,
        lock_path: Path | None = None,
    ) -> None:
        paths = resolve_update_paths()
        self.config = config
        self.logger = logger
        self.status_path = status_path or paths.status_path
        self.lock_path = lock_path or paths.lock_path
        self.run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
        self.reporter = StatusReporter(self.status_path, run_id=self.run_id)
        self._lock_acquired = False
        self._swap_completed = False
        self._cutover_committed = False
        self._db_snapshot_dir: Path | None = None
        self._current_target: Path | None = None
        self._candidate_venv: Path | None = None
        self._post_cutover_warnings: list[str] = []

    def run(self) -> None:
        try:
            self._run_inner()
        except UpdateInProgressError:
            return
        except Exception:
            self.logger.exception("System update failed")
            self.reporter.write(
                "error",
                "Update crashed; see hub logs for details.",
                phase="worker_crashed",
                error_type="worker_crashed",
            )
        finally:
            if self._lock_acquired:
                release_lock(self.lock_path)

    def _run_inner(self) -> None:
        try:
            update_target = normalize_update_target(self.config.update_target)
        except ValueError as exc:
            self.logger.error("%s", exc)
            self.reporter.write("error", str(exc))
            return

        repo_ref = str(self.config.repo_ref or "main").strip() or "main"
        from ._facade import _normalize_update_backend

        try:
            _normalize_update_backend(self.config.update_backend)
        except ValueError as exc:
            self.logger.error("%s", exc)
            self.reporter.write("error", str(exc))
            return

        try:
            self._lock_acquired = acquire_lock(
                self.lock_path,
                repo_url=self.config.repo_url,
                repo_ref=repo_ref,
                update_target=update_target,
                logger=self.logger,
            )
        except UpdateInProgressError:
            return

        resolved_backend = resolve_backend(
            self.config.update_backend,
            detect_supervisor_identity(
                hint=self.config.identity_hint,
                current_venv_link=_resolve_venv_paths()[1],
                car_wrapper_path=_resolve_car_wrapper_path(),
            ),
        )

        self.reporter.write(
            "running",
            "Update started.",
            phase="worker_start",
            repo_url=self.config.repo_url,
            update_dir=str(self.config.update_dir),
            repo_ref=repo_ref,
            update_target=update_target,
            update_backend=resolved_backend,
            update_run_id=self.run_id,
        )

        missing = [
            cmd
            for cmd in _required_commands(resolved_backend)
            if resolve_executable(cmd) is None
        ]
        if missing:
            msg = f"Missing required commands: {', '.join(missing)}"
            self.logger.error(msg)
            self.reporter.write("error", msg)
            return

        pipx_layout = resolve_pipx_layout()
        pipx_root = pipx_layout.pipx_root
        current_link = pipx_layout.current_link
        prev_link = pipx_layout.prev_link
        identity = detect_supervisor_identity(
            hint=self.config.identity_hint,
            current_venv_link=current_link,
            car_wrapper_path=_resolve_car_wrapper_path(),
        )
        resolved_backend = resolve_backend(self.config.update_backend, identity)

        if not self.config.allow_in_place and not self.config.restart_command:
            try:
                guard_self_update(identity)
            except RuntimeError as exc:
                self.logger.error("%s", exc)
                self.reporter.write("error", str(exc), error_type="guard_refused")
                return

        cutover_ok, remediation = verify_cutover_routing(identity)
        use_staged = cutover_ok
        if not cutover_ok:
            if self.config.allow_in_place:
                self.logger.warning(
                    "EMERGENCY in-place update enabled (update.allow_in_place=true). %s",
                    remediation,
                )
                self.reporter.write(
                    "running",
                    f"EMERGENCY in-place update: {remediation}",
                    phase="in_place_warning",
                )
                use_staged = False
            else:
                self.logger.error(remediation)
                self.reporter.write(
                    "error",
                    remediation,
                    phase="cutover_routing_refused",
                    error_type="cutover_routing_refused",
                )
                return

        self.config.update_dir.parent.mkdir(parents=True, exist_ok=True)
        with self.reporter.timed_phase("source_prep"):
            prepare_update_source(
                self.config.update_dir,
                self.config.repo_url,
                repo_ref,
                self.logger,
            )

        if not self.config.skip_checks:
            self.logger.info("Running checks...")
            try:
                subprocess.run(
                    ["./scripts/check.sh"],
                    cwd=self.config.update_dir,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=_CMD_TIMEOUT_SECONDS,
                )
            except (subprocess.CalledProcessError, OSError) as exc:
                self.logger.warning("Checks failed; continuing with update. %s", exc)

        services = _services_for_target(update_target)
        service_names = _resolve_linux_service_names(
            identity, self.config.linux_service_names
        )

        if use_staged:
            self._run_staged_update(
                identity=identity,
                resolved_backend=resolved_backend,
                update_target=update_target,
                services=services,
                service_names=service_names,
                pipx_root=pipx_root,
                current_link=current_link,
                prev_link=prev_link,
                active_venv=pipx_layout.active_venv,
                detected_venv_candidates=pipx_layout.candidates,
            )
        else:
            self._run_in_place_update(
                identity=identity,
                resolved_backend=resolved_backend,
                services=services,
                service_names=service_names,
            )

    def _run_in_place_update(
        self,
        *,
        identity: SupervisorIdentity,
        resolved_backend: str,
        services: UpdateServices,
        service_names: dict[str, str],
    ) -> None:
        helper = _resolve_python_bin(self.config.helper_python)
        package_src = self.config.update_dir
        extras = select_pip_extras(hub_root=identity.hub_root)
        install_spec = f"{package_src}{extras}"

        with self.reporter.timed_phase("pip_upgrade"):
            subprocess.run(
                [helper, "-m", "pip", "-q", "install", "--upgrade", "pip"],
                check=True,
                timeout=_CMD_TIMEOUT_SECONDS,
            )
        with self.reporter.timed_phase("pip_install"):
            subprocess.run(
                [helper, "-m", "pip", "-q", "install", "--upgrade", install_spec],
                check=True,
                timeout=_CMD_TIMEOUT_SECONDS,
            )
        with self.reporter.timed_phase("playwright_chromium"):
            ensure_playwright_chromium(helper)

        self._refresh_managed_repos(identity, helper)
        self._restart_and_health_check(
            identity=identity,
            resolved_backend=resolved_backend,
            services=services,
            service_names=service_names,
            staged_cutover=False,
        )

    def _run_staged_update(
        self,
        *,
        identity: SupervisorIdentity,
        resolved_backend: str,
        update_target: str,
        services: UpdateServices,
        service_names: dict[str, str],
        pipx_root: Path,
        current_link: Path,
        prev_link: Path,
        active_venv: Path,
        detected_venv_candidates: tuple[Path, ...],
    ) -> None:
        package_src = self.config.update_dir
        python_bin = Path(
            os.environ.get("PIPX_PYTHON")
            or _resolve_python_bin(self.config.helper_python)
        )
        extras = select_pip_extras(hub_root=identity.hub_root)
        installer = StagedInstaller(
            package_src=package_src,
            pipx_root=pipx_root,
            current_venv_link=current_link,
            prev_venv_link=prev_link,
            python_bin=python_bin,
            logger=self.logger,
            extras=extras,
        )
        cutover = CutoverManager(
            current_venv_link=current_link,
            prev_venv_link=prev_link,
            pipx_root=pipx_root,
            keep_old_venvs=int(
                os.environ.get("KEEP_OLD_VENVS", _DEFAULT_KEEP_OLD_VENVS)
            ),
            logger=self.logger,
        )

        self._current_target = cutover.initialize_current_link(
            active_venv,
            detected_candidates=detected_venv_candidates,
        )

        try:
            with self.reporter.timed_phase("venv_create"):
                self._candidate_venv = installer.create_staged_venv()
            wheel_dir = Path(f"{self._candidate_venv}.wheelhouse")
            with self.reporter.timed_phase("wheel_build"):
                wheel_path = installer.build_wheel(
                    self._candidate_venv / "bin" / "python", wheel_dir
                )
            with self.reporter.timed_phase("pip_install"):
                installer.install_wheel(
                    self._candidate_venv / "bin" / "python", wheel_path
                )
            with self.reporter.timed_phase("playwright_chromium"):
                ensure_playwright_chromium(str(self._candidate_venv / "bin" / "python"))
            shutil.rmtree(wheel_dir, ignore_errors=True)

            with self.reporter.timed_phase("candidate_validation"):
                installer.validate_candidate(self._candidate_venv)

            hub_root = identity.hub_root
            if hub_root is None:
                raise RuntimeError("Unable to determine HUB_ROOT for DB snapshot.")

            snapshot_root = self.status_path.parent / "update_snapshots"
            max_snapshots = int(
                os.environ.get("UPDATE_SNAPSHOT_MAX_COUNT", _DEFAULT_SNAPSHOT_MAX_COUNT)
            )
            with self.reporter.timed_phase("db_snapshot"):
                transaction = snapshot_orchestration_db_transaction(
                    hub_root,
                    snapshot_root=snapshot_root,
                    run_id=self.run_id,
                    max_snapshots=max_snapshots if max_snapshots > 0 else None,
                )
                self._db_snapshot_dir = Path(transaction.snapshot.snapshot_dir)

            cutover.prepare(self._current_target)
            cutover.flip_to(self._candidate_venv)
            self._swap_completed = True

            self._refresh_managed_repos(
                identity,
                str(current_link / "bin" / "python"),
            )

            committed = self._restart_and_health_check(
                identity=identity,
                resolved_backend=resolved_backend,
                services=services,
                service_names=service_names,
                staged_cutover=True,
            )
            if not committed:
                return

            self._cutover_committed = True
            local_bin = Path(
                os.environ.get("LOCAL_BIN", str(_DEFAULT_LOCAL_BIN))
            ).expanduser()
            try:
                with self.reporter.timed_phase("car_wrapper_sync"):
                    cutover.sync_car_wrapper(
                        package_src=package_src,
                        local_bin=local_bin,
                        car_wrapper_path=_resolve_car_wrapper_path(),
                    )
            except Exception as exc:
                warning = (
                    "Global car CLI wrapper update failed after cutover; "
                    "keeping the healthy staged venv active and skipping rollback."
                )
                self.logger.warning("%s %s", warning, exc)
                self._post_cutover_warnings.append(warning)

            with self.reporter.timed_phase("prune_old_venvs"):
                cutover.prune_old_venvs()

            message = "Update completed successfully."
            if self._post_cutover_warnings:
                message += " " + " ".join(self._post_cutover_warnings)
            self.reporter.write(
                "ok",
                message,
                phase="completed",
                update_target=update_target,
            )
        except (StagedInstallError, RuntimeError, subprocess.CalledProcessError) as exc:
            self.logger.error("Staged update failed: %s", exc)
            if self._swap_completed and not self._cutover_committed:
                self._attempt_rollback(
                    identity=identity,
                    resolved_backend=resolved_backend,
                    services=services,
                    service_names=service_names,
                    reason=str(exc),
                )
            elif not self._swap_completed:
                self.reporter.write(
                    "error",
                    f"Update failed: {exc}",
                    phase="staged_install_failed",
                    error_type="staged_install_failed",
                )

    def _refresh_managed_repos(
        self, identity: SupervisorIdentity, helper_python: str
    ) -> None:
        hub_root = identity.hub_root
        if hub_root is None:
            self.logger.warning("Skipping managed repo refresh; HUB_ROOT unknown.")
            return
        script = self.config.update_dir / "scripts" / "update-hub-managed-repos.sh"
        if not script.exists():
            self.logger.warning("Managed repo refresh script missing at %s", script)
            return
        with self.reporter.timed_phase("managed_repo_refresh"):
            subprocess.run(
                ["bash", str(script)],
                cwd=self.config.update_dir,
                env={
                    **os.environ,
                    "HUB_ROOT": str(hub_root),
                    "HELPER_PYTHON": helper_python,
                },
                check=True,
                timeout=_CMD_TIMEOUT_SECONDS,
            )

    def _launchd_chat_labels(self) -> tuple[str | None, str | None]:
        base_label = str(os.environ.get("LABEL", "com.codex.autorunner")).strip()
        telegram = str(
            os.environ.get("TELEGRAM_LABEL", f"{base_label}.telegram")
        ).strip()
        discord = str(os.environ.get("DISCORD_LABEL", f"{base_label}.discord")).strip()
        return telegram or None, discord or None

    def _build_launchd_manager(
        self, identity: SupervisorIdentity
    ) -> LaunchdPlistManager:
        telegram_label, discord_label = self._launchd_chat_labels()
        launch_agents = Path("~/Library/LaunchAgents").expanduser()
        label = identity.label or os.environ.get("LABEL", "com.codex.autorunner")
        hub_plist = Path(
            os.environ.get("PLIST_PATH", str(launch_agents / f"{label}.plist"))
        ).expanduser()
        telegram_plist = (
            Path(
                os.environ.get(
                    "TELEGRAM_PLIST_PATH",
                    str(launch_agents / f"{telegram_label}.plist"),
                )
            ).expanduser()
            if telegram_label
            else None
        )
        discord_plist = (
            Path(
                os.environ.get(
                    "DISCORD_PLIST_PATH",
                    str(launch_agents / f"{discord_label}.plist"),
                )
            ).expanduser()
            if discord_label
            else None
        )
        _pipx_root, current_link, _prev_link = _resolve_venv_paths()
        home = Path.home()
        opencode_bin = os.environ.get("OPENCODE_BIN", str(home / ".opencode" / "bin"))
        path_dirs = (
            opencode_bin,
            os.environ.get("NVM_BIN", ""),
            os.environ.get("LOCAL_BIN", str(_DEFAULT_LOCAL_BIN)),
            os.environ.get("PY39_BIN", ""),
        )
        return LaunchdPlistManager(
            label=label,
            hub_plist_path=hub_plist,
            current_venv_link=current_link,
            hub_root=identity.hub_root,
            uid=os.getuid(),
            opencode_bin=opencode_bin,
            path_dirs=tuple(d for d in path_dirs if d),
            telegram_label=telegram_label,
            telegram_plist_path=telegram_plist,
            discord_label=discord_label,
            discord_plist_path=discord_plist,
            stop_wait_seconds=float(os.environ.get("LAUNCHD_STOP_WAIT_SECONDS", "10")),
            environ=dict(os.environ),
            logger=self.logger,
        )

    def _build_supervisor(
        self,
        identity: SupervisorIdentity,
        service_names: dict[str, str],
    ):
        if identity.backend == "launchd":
            adapters: list = [
                LaunchdManagedAdapter(self._build_launchd_manager(identity))
            ]
            if self.config.restart_command:
                adapters.append(
                    CommandAdapter(restart_command=self.config.restart_command)
                )
            adapters.append(SignalAdapter(identity=identity))
            return LayeredSupervisor(adapters=adapters)
        return build_layered_supervisor(
            identity=identity,
            restart_command=self.config.restart_command,
            hub_service=service_names.get("hub"),
            telegram_service=service_names.get("telegram"),
            discord_service=service_names.get("discord"),
            launchd_label=identity.label,
            launchd_domain=f"gui/{os.getuid()}",
            systemctl_sudo=self.config.systemctl_sudo,
        )

    def _build_health_checker(self) -> HealthChecker:
        return HealthChecker(
            host=self.config.server_host,
            port=self.config.server_port,
            base_path=self.config.server_base_path,
            logger=self.logger,
        )

    def _restart_and_health_check(
        self,
        *,
        identity: SupervisorIdentity,
        resolved_backend: str,
        services: UpdateServices,
        service_names: dict[str, str],
        staged_cutover: bool,
    ) -> bool:
        supervisor = self._build_supervisor(identity, service_names)
        checker = self._build_health_checker()

        with self.reporter.timed_phase("service_restart"):
            restart_result = supervisor.restart(services)
            if not restart_result.success:
                msg = f"Service restart failed: {restart_result.error}"
                self.logger.error(msg)
                if staged_cutover and self._swap_completed:
                    self._attempt_rollback(
                        identity=identity,
                        resolved_backend=resolved_backend,
                        services=services,
                        service_names=service_names,
                        reason=msg,
                    )
                else:
                    self.reporter.write("error", msg, phase="restart_failed")
                return False

        hub_warm_ok = True
        if (
            services.restart_hub
            and self.config.wait_hub_health_before_chat
            and (services.restart_telegram or services.restart_discord)
        ):
            warm = wait_hub_before_chat(checker, enabled=True)
            hub_warm_ok = warm.ok
            if not warm.ok:
                self.logger.error("Hub health warmup failed: %s", warm.message)

        if hub_warm_ok and services.restart_hub:
            with self.reporter.timed_phase("hub_health_check"):
                hub_result = checker.wait_for_hub_health()
                if not hub_result.ok:
                    hub_warm_ok = False
                    self.logger.error("Hub health check failed: %s", hub_result.message)

        chat_ok = True
        if services.restart_telegram and service_names.get("telegram"):
            if resolved_backend == "launchd":
                state = _launchd_telegram_state(identity.hub_root, environ=os.environ)
                if state not in ("disabled", "missing_env"):
                    with self.reporter.timed_phase("telegram_health_check"):
                        _pipx, current_link, _prev = _resolve_venv_paths()
                        if identity.hub_root is not None:
                            chat_ok = checker.wait_telegram_cli_healthy(
                                python_bin=current_link / "bin" / "python",
                                hub_root=identity.hub_root,
                            ).ok
                        else:
                            label = os.environ.get(
                                "TELEGRAM_LABEL",
                                f"{os.environ.get('LABEL', 'com.codex.autorunner')}.telegram",
                            )
                            chat_ok = checker.wait_launchd_label_running(label).ok
            else:
                with self.reporter.timed_phase("telegram_health_check"):
                    telegram_scope: Literal["user", "system"] = (
                        "user" if resolved_backend == "systemd-user" else "system"
                    )
                    chat_ok = checker.systemd_service_active(
                        service_names["telegram"], scope=telegram_scope
                    )
        if services.restart_discord and service_names.get("discord"):
            if resolved_backend == "launchd":
                state = _launchd_discord_state(identity.hub_root, environ=os.environ)
                if state not in ("disabled", "missing_env"):
                    with self.reporter.timed_phase("discord_health_check"):
                        label = os.environ.get(
                            "DISCORD_LABEL",
                            f"{os.environ.get('LABEL', 'com.codex.autorunner')}.discord",
                        )
                        chat_ok = (
                            chat_ok and checker.wait_launchd_label_running(label).ok
                        )
            else:
                with self.reporter.timed_phase("discord_health_check"):
                    discord_scope: Literal["user", "system"] = (
                        "user" if resolved_backend == "systemd-user" else "system"
                    )
                    chat_ok = chat_ok and checker.systemd_service_active(
                        service_names["discord"], scope=discord_scope
                    )

        if hub_warm_ok and chat_ok:
            return True

        reason = "Health check failed after update."
        if staged_cutover and self._swap_completed:
            self._attempt_rollback(
                identity=identity,
                resolved_backend=resolved_backend,
                services=services,
                service_names=service_names,
                reason=reason,
            )
        else:
            self.reporter.write("error", reason, phase="health_check_failed")
        return False

    def _attempt_rollback(
        self,
        *,
        identity: SupervisorIdentity,
        resolved_backend: str,
        services: UpdateServices,
        service_names: dict[str, str],
        reason: str,
    ) -> None:
        if self._current_target is None:
            self.reporter.write("error", reason, phase="rollback_skipped")
            return

        pipx_root, current_link, prev_link = _resolve_venv_paths()
        cutover = CutoverManager(
            current_venv_link=current_link,
            prev_venv_link=prev_link,
            pipx_root=pipx_root,
            logger=self.logger,
        )
        supervisor = self._build_supervisor(identity, service_names)

        if self._db_snapshot_dir is not None:
            try:
                restore_orchestration_db_snapshot(self._db_snapshot_dir)
            except Exception as exc:
                self.logger.error("DB restore failed during rollback: %s", exc)
                self.reporter.write(
                    "error",
                    "Update failed; orchestration DB snapshot restore failed. "
                    "Keeping candidate venv active to avoid schema rollback mismatch.",
                    phase="db_restore_failed",
                    error_type="db_restore_failed",
                )
                supervisor.restart(services)
                return

        cutover.rollback_to(self._current_target)
        supervisor.restart(services)
        self.reporter.write(
            "rollback",
            f"Update failed; rollback attempted. {reason}",
            phase="rollback_attempted",
        )
