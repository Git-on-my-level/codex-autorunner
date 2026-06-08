"""Staged venv install: wheel build, candidate validation, Playwright setup."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Optional

from ...voice.provider_catalog import normalize_voice_provider
from ..utils import resolve_executable

_WHEEL_REQUIRED_PREFIXES = (
    "codex_autorunner/workspace/",
    "codex_autorunner/tickets/",
    "codex_autorunner/adapters/docker/",
)
_WHEEL_REQUIRED_FILES = ("codex_autorunner/web_static/index.html",)
_REQUIRED_IMPORT_MODULES = (
    "codex_autorunner.workspace",
    "codex_autorunner.tickets",
    "codex_autorunner.adapters.docker",
)
_INSTALL_CMD_TIMEOUT_SECONDS = 600
_PIP_CMD_TIMEOUT_SECONDS = 300


class StagedInstallError(RuntimeError):
    """Raised when staged install or candidate validation fails."""


def select_pip_extras(
    *,
    hub_root: Optional[Path] = None,
    voice_provider: Optional[str] = None,
) -> str:
    """Return pip extras spec from hub config/env (default ``[browser]``)."""
    provider = voice_provider
    if provider is None and hub_root is not None:
        provider = _voice_provider_for_hub_root(hub_root)
    normalized = normalize_voice_provider(provider or "")
    if normalized == "local_whisper":
        return "[browser,voice-local]"
    if normalized == "mlx_whisper":
        return "[browser,voice-mlx]"
    return "[browser]"


def _voice_provider_for_hub_root(hub_root: Path) -> str:
    provider = _config_value(hub_root, "repo_defaults.voice.provider")
    if not provider:
        provider = _config_value(hub_root, "voice.provider")
    if not provider:
        provider = _dotenv_value(hub_root, "CODEX_AUTORUNNER_VOICE_PROVIDER")
    return normalize_voice_provider(provider or "local_whisper")


def _config_value(hub_root: Path, dotted_key: str) -> str:
    config_path = hub_root / ".codex-autorunner" / "config.yml"
    if not config_path.exists():
        return ""
    try:
        import yaml
    except ImportError:
        return ""
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError:
        return ""
    if not isinstance(data, dict):
        return ""
    value: object = data
    for part in dotted_key.split("."):
        if not isinstance(value, dict):
            return ""
        value = value.get(part)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _dotenv_value(hub_root: Path, key: str) -> str:
    for env_path in (hub_root / ".env", hub_root / ".codex-autorunner" / ".env"):
        if not env_path.exists():
            continue
        try:
            from dotenv import dotenv_values
        except ImportError:
            pass
        else:
            values = dotenv_values(env_path)
            if isinstance(values, dict):
                raw = values.get(key)
                if raw is not None and str(raw).strip():
                    return str(raw).strip()
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[len("export ") :].strip()
            if "=" not in stripped:
                continue
            name, value = stripped.split("=", 1)
            if name.strip() != key:
                continue
            value = value.strip()
            if value and value[0] in {"'", '"'} and value[-1] == value[0]:
                value = value[1:-1]
            if value:
                return value
    return ""


def ensure_playwright_chromium(python_bin: Path | str) -> None:
    """Install Playwright Chromium when the browser extra is present but missing."""
    python = Path(python_bin)
    if not python.is_file():
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return
    try:
        playwright = sync_playwright().start()
        chromium_path = playwright.chromium.executable_path
        playwright.stop()
        if chromium_path and Path(chromium_path).exists():
            return
    except Exception:
        pass
    subprocess.run(
        [str(python), "-m", "playwright", "install", "chromium"],
        check=True,
        timeout=_INSTALL_CMD_TIMEOUT_SECONDS,
    )


class StagedInstaller:
    """Build and validate a staged pipx venv before atomic cutover."""

    def __init__(
        self,
        *,
        package_src: Path,
        pipx_root: Path,
        current_venv_link: Path,
        prev_venv_link: Path,
        python_bin: Path,
        logger: logging.Logger,
        extras: str = "[browser]",
    ) -> None:
        self.package_src = package_src.expanduser().resolve()
        self.pipx_root = pipx_root.expanduser()
        self.current_venv_link = current_venv_link.expanduser()
        self.prev_venv_link = prev_venv_link.expanduser()
        self.python_bin = python_bin.expanduser()
        self.logger = logger
        self.extras = extras

    def create_staged_venv(self) -> Path:
        """Create ``codex-autorunner.next-<timestamp>`` under pipx venvs."""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        next_venv = self.pipx_root / "venvs" / f"codex-autorunner.next-{timestamp}"
        self.logger.info(
            "Creating staged venv at %s (python: %s)", next_venv, self.python_bin
        )
        subprocess.run(
            [str(self.python_bin), "-m", "venv", str(next_venv)],
            check=True,
            timeout=_INSTALL_CMD_TIMEOUT_SECONDS,
        )
        staged_python = next_venv / "bin" / "python"
        subprocess.run(
            [str(staged_python), "-m", "pip", "-q", "install", "--upgrade", "pip"],
            check=True,
            timeout=_PIP_CMD_TIMEOUT_SECONDS,
        )
        return next_venv

    def build_wheel(self, staged_venv_python: Path, wheel_dir: Path) -> Path:
        """Build frontend assets, pip wheel, and validate wheel contents."""
        wheel_dir = wheel_dir.expanduser()
        if wheel_dir.exists():
            shutil.rmtree(wheel_dir)
        wheel_dir.mkdir(parents=True, exist_ok=True)

        self._build_web_static_assets()

        self.logger.info(
            "Building codex-autorunner wheel from %s into %s",
            self.package_src,
            wheel_dir,
        )
        subprocess.run(
            [
                str(staged_venv_python),
                "-m",
                "pip",
                "-q",
                "wheel",
                "--no-deps",
                "--no-cache-dir",
                "--wheel-dir",
                str(wheel_dir),
                str(self.package_src),
            ],
            check=True,
            timeout=_INSTALL_CMD_TIMEOUT_SECONDS,
        )

        wheels = sorted(wheel_dir.glob("codex_autorunner-*.whl"))
        if not wheels:
            raise StagedInstallError(
                f"Unable to locate freshly built codex-autorunner wheel in {wheel_dir}."
            )
        wheel_path = wheels[-1]
        _validate_wheel_contents(wheel_path)
        self.logger.info("Built wheel %s", wheel_path)
        return wheel_path

    def install_wheel(self, staged_venv_python: Path, wheel_path: Path) -> None:
        """Install the staged wheel with configured extras into the candidate venv."""
        wheel_path = wheel_path.expanduser().resolve()
        if not wheel_path.is_file() or wheel_path.suffix != ".whl":
            raise StagedInstallError(
                f"Invalid staged wheel path for pip install: {wheel_path}"
            )
        wheel_uri = wheel_path.resolve().as_uri()
        install_spec = f"codex-autorunner{self.extras} @ {wheel_uri}"
        self.logger.info(
            "Installing codex-autorunner from staged wheel %s into candidate venv",
            wheel_path,
        )
        subprocess.run(
            [
                str(staged_venv_python),
                "-m",
                "pip",
                "-q",
                "install",
                "--force-reinstall",
                install_spec,
            ],
            check=True,
            timeout=_INSTALL_CMD_TIMEOUT_SECONDS,
        )

    def validate_candidate(self, staged_venv: Path) -> None:
        """Smoke-check imports, hub startup, and telegram module in staged venv."""
        staged_venv = staged_venv.expanduser().resolve()
        console_script = staged_venv / "bin" / "codex-autorunner"
        if not os.access(console_script, os.X_OK):
            raise StagedInstallError(
                "Staged venv is missing codex-autorunner console script."
            )

        staged_python = staged_venv / "bin" / "python"
        self._validate_imports(staged_python)
        self.logger.info("Smoke-checking hub startup lifecycle...")
        self._validate_hub_startup(staged_python)
        self.logger.info("Smoke-checking telegram module...")
        self._validate_telegram_module(staged_python)

    def _build_web_static_assets(self) -> None:
        package_json = self.package_src / "package.json"
        if not package_json.is_file():
            raise StagedInstallError(
                f"Package source {self.package_src} is missing package.json; "
                "cannot build Web Hub static assets."
            )
        if resolve_executable("pnpm") is None:
            raise StagedInstallError(
                "pnpm is required to build Web Hub static assets before packaging."
            )
        self.logger.info("Building Web Hub static assets from %s...", self.package_src)
        subprocess.run(
            ["pnpm", "install", "--frozen-lockfile"],
            cwd=self.package_src,
            check=True,
            timeout=_INSTALL_CMD_TIMEOUT_SECONDS,
        )
        subprocess.run(
            ["pnpm", "run", "build"],
            cwd=self.package_src,
            check=True,
            timeout=_INSTALL_CMD_TIMEOUT_SECONDS,
        )

    def _validate_imports(self, staged_python: Path) -> None:
        script = """
import importlib.util

required_modules = {required_modules!r}
for module_name in required_modules:
    if importlib.util.find_spec(module_name) is None:
        raise SystemExit(f"required staged module missing: {{module_name}}")

import codex_autorunner  # noqa: F401
from codex_autorunner.server import create_hub_app  # noqa: F401

print("staged imports ok")
""".format(required_modules=_REQUIRED_IMPORT_MODULES)
        self._run_python_script(staged_python, script)

    def _validate_hub_startup(self, staged_python: Path) -> None:
        script = """
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.server import create_hub_app

with TemporaryDirectory() as tmp:
    hub_root = Path(tmp)
    seed_hub_files(hub_root, force=True)
    app = create_hub_app(hub_root)
    with TestClient(app) as client:
        for path in ("/health", "/car/health"):
            response = client.get(path)
            if response.status_code == 200:
                break
        else:
            raise SystemExit("hub startup smoke failed: /health endpoint unavailable")
print("hub startup ok")
"""
        self._run_python_script(staged_python, script)

    def _validate_telegram_module(self, staged_python: Path) -> None:
        script = """
import importlib.util
import py_compile

spec = importlib.util.find_spec("codex_autorunner.adapters.telegram.service")
if spec is None or spec.origin is None:
    raise SystemExit("telegram service module not found in staged venv")
py_compile.compile(spec.origin, doraise=True)
print("telegram service ok")
"""
        self._run_python_script(staged_python, script)

    def _run_python_script(self, staged_python: Path, script: str) -> None:
        result = subprocess.run(
            [str(staged_python), "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=_INSTALL_CMD_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise StagedInstallError(detail or "Staged candidate validation failed.")


def _validate_wheel_contents(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as zf:
        names = {PurePosixPath(name).as_posix() for name in zf.namelist()}
    missing: list[str] = []
    for prefix in _WHEEL_REQUIRED_PREFIXES:
        if not any(name.startswith(prefix) for name in names):
            missing.append(prefix.rstrip("/"))
    missing.extend(path for path in _WHEEL_REQUIRED_FILES if path not in names)
    if missing:
        raise StagedInstallError(
            "built wheel is missing required packages: " + ", ".join(missing)
        )
