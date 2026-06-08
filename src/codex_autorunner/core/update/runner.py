"""CLI entry for the update worker (spawned detached from the hub)."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from ._facade import _system_update_worker


def _build_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("codex_autorunner.system_update")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger


def _parse_identity_hint(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run codex-autorunner update worker.")
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--repo-ref", default="main")
    parser.add_argument("--update-dir", required=True)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--target", default="all")
    parser.add_argument("--backend", default="auto")
    parser.add_argument("--hub-service-name")
    parser.add_argument("--telegram-service-name")
    parser.add_argument("--discord-service-name")
    parser.add_argument("--restart-command")
    parser.add_argument("--systemctl-sudo", default="auto")
    parser.add_argument(
        "--allow-in-place", action=argparse.BooleanOptionalAction, default=False
    )
    parser.add_argument("--identity-hint")
    parser.add_argument("--server-host", default="127.0.0.1")
    parser.add_argument("--server-port", type=int, default=4173)
    parser.add_argument("--server-base-path", default="")
    parser.add_argument(
        "--skip-checks", action=argparse.BooleanOptionalAction, default=True
    )
    args = parser.parse_args(argv)

    update_dir = Path(args.update_dir).expanduser()
    log_path = Path(args.log_path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = _build_logger(log_path)

    linux_names: dict[str, str] = {}
    if args.hub_service_name:
        linux_names["hub"] = args.hub_service_name
    if args.telegram_service_name:
        linux_names["telegram"] = args.telegram_service_name
    if args.discord_service_name:
        linux_names["discord"] = args.discord_service_name

    restart_command: str | list[str] | None = args.restart_command
    if isinstance(restart_command, str) and restart_command.startswith("["):
        try:
            parsed = json.loads(restart_command)
            if isinstance(parsed, list):
                restart_command = [str(part) for part in parsed]
        except json.JSONDecodeError:
            pass

    _system_update_worker(
        repo_url=args.repo_url,
        repo_ref=args.repo_ref,
        update_dir=update_dir,
        logger=logger,
        update_target=args.target,
        update_backend=args.backend,
        skip_checks=bool(args.skip_checks),
        linux_hub_service_name=linux_names.get("hub"),
        linux_telegram_service_name=linux_names.get("telegram"),
        linux_discord_service_name=linux_names.get("discord"),
        restart_command=restart_command,
        systemctl_sudo=args.systemctl_sudo,
        allow_in_place=bool(args.allow_in_place),
        identity_hint=_parse_identity_hint(args.identity_hint),
        server_host=args.server_host,
        server_port=args.server_port,
        server_base_path=args.server_base_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
