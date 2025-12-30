from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from .config import Config


DEFAULT_EVENTS = {"run_finished", "run_error", "tui_idle"}
KNOWN_EVENTS = {"run_finished", "run_error", "tui_idle", "tui_session_finished", "all"}
DEFAULT_TIMEOUT_SECONDS = 5.0


class NotificationManager:
    def __init__(self, config: Config, *, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        raw = config.raw.get("notifications")
        self._cfg = raw if isinstance(raw, dict) else {}
        self._warned_missing: set[str] = set()
        self._enabled_mode = self._parse_enabled(self._cfg.get("enabled"))
        self._events = self._normalize_events(self._cfg.get("events"))
        self._warn_unknown_events(self._events)
        self._discord = (
            self._cfg.get("discord") if isinstance(self._cfg.get("discord"), dict) else {}
        )
        self._telegram = (
            self._cfg.get("telegram") if isinstance(self._cfg.get("telegram"), dict) else {}
        )
        self._discord_enabled = self._discord.get("enabled") is not False
        self._telegram_enabled = self._telegram.get("enabled") is not False

    def set_logger(self, logger: logging.Logger) -> None:
        self.logger = logger

    def notify_run_finished(self, *, run_id: int, exit_code: Optional[int]) -> None:
        event = "run_finished" if exit_code == 0 else "run_error"
        message = self._format_run_message(run_id=run_id, exit_code=exit_code)
        self._notify_sync(event, message)

    async def notify_run_finished_async(
        self, *, run_id: int, exit_code: Optional[int]
    ) -> None:
        event = "run_finished" if exit_code == 0 else "run_error"
        message = self._format_run_message(run_id=run_id, exit_code=exit_code)
        await self._notify_async(event, message)

    def notify_tui_session_finished(
        self,
        *,
        session_id: Optional[str],
        exit_code: Optional[int],
        repo_path: Optional[str] = None,
    ) -> None:
        message = self._format_tui_message(
            session_id=session_id, exit_code=exit_code, repo_path=repo_path
        )
        self._notify_sync("tui_session_finished", message)

    async def notify_tui_session_finished_async(
        self,
        *,
        session_id: Optional[str],
        exit_code: Optional[int],
        repo_path: Optional[str] = None,
    ) -> None:
        message = self._format_tui_message(
            session_id=session_id, exit_code=exit_code, repo_path=repo_path
        )
        await self._notify_async("tui_session_finished", message)

    def notify_tui_idle(
        self,
        *,
        session_id: Optional[str],
        idle_seconds: float,
        repo_path: Optional[str] = None,
    ) -> None:
        message = self._format_tui_idle_message(
            session_id=session_id,
            idle_seconds=idle_seconds,
            repo_path=repo_path,
        )
        self._notify_sync("tui_idle", message)

    async def notify_tui_idle_async(
        self,
        *,
        session_id: Optional[str],
        idle_seconds: float,
        repo_path: Optional[str] = None,
    ) -> None:
        message = self._format_tui_idle_message(
            session_id=session_id,
            idle_seconds=idle_seconds,
            repo_path=repo_path,
        )
        await self._notify_async("tui_idle", message)

    def _normalize_events(self, raw_events) -> set[str]:
        if raw_events is None:
            return set(DEFAULT_EVENTS)
        if not isinstance(raw_events, list):
            return set(DEFAULT_EVENTS)
        normalized = {
            item.strip()
            for item in raw_events
            if isinstance(item, str) and item.strip()
        }
        return normalized

    def _warn_unknown_events(self, events: set[str]) -> None:
        unknown = {event for event in events if event not in KNOWN_EVENTS}
        if not unknown:
            return
        details = ", ".join(sorted(unknown))
        self._warn_once(
            "notifications.unknown_events",
            f"Unknown notification events configured: {details}",
        )

    def _should_notify(self, event: str) -> bool:
        enabled = self._is_enabled()
        if not enabled:
            return False
        if not self._events:
            return False
        if "all" in self._events:
            return True
        return event in self._events

    def _parse_enabled(self, raw) -> bool | str:
        if isinstance(raw, bool):
            return raw
        if raw is None:
            return "auto"
        if isinstance(raw, str) and raw.strip().lower() == "auto":
            return "auto"
        return False

    def _is_enabled(self) -> bool:
        if self._enabled_mode is True:
            return True
        if self._enabled_mode is False:
            return False
        return self._targets_available()

    def _format_run_message(self, *, run_id: int, exit_code: Optional[int]) -> str:
        repo_label = self._repo_label()
        if exit_code == 0:
            status = "complete"
            summary_text = "summary finalized"
        else:
            status = "failed"
            summary_text = None
        code_text = (
            f"exit {exit_code}" if exit_code is not None else "exit unknown"
        )
        if summary_text:
            details = f"{summary_text}, {code_text}"
        else:
            details = code_text
        return f"CAR run {run_id} {status} ({details}) in {repo_label}"

    def _format_tui_message(
        self,
        *,
        session_id: Optional[str],
        exit_code: Optional[int],
        repo_path: Optional[str],
    ) -> str:
        repo_label = repo_path or self._repo_label()
        session_text = f"session {session_id}" if session_id else "session"
        code_text = (
            f"exit {exit_code}" if exit_code is not None else "exit unknown"
        )
        return f"CAR TUI session ended ({session_text}, {code_text}) in {repo_label}"

    def _format_tui_idle_message(
        self,
        *,
        session_id: Optional[str],
        idle_seconds: float,
        repo_path: Optional[str],
    ) -> str:
        repo_label = repo_path or self._repo_label()
        session_text = f"session {session_id}" if session_id else "session"
        idle_text = f"idle {int(idle_seconds)}s"
        return f"CAR TUI idle ({session_text}, {idle_text}) in {repo_label}"

    def _repo_label(self) -> str:
        name = self.config.root.name
        return name or str(self.config.root)

    def _notify_sync(self, event: str, message: str) -> None:
        if not self._should_notify(event):
            return
        targets = self._resolve_targets()
        if not targets:
            return
        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
                self._send_sync(client, targets, message)
        except Exception as exc:
            self._log_warning("Notification delivery failed", exc)

    async def _notify_async(self, event: str, message: str) -> None:
        if not self._should_notify(event):
            return
        targets = self._resolve_targets()
        if not targets:
            return
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
                await self._send_async(client, targets, message)
        except Exception as exc:
            self._log_warning("Notification delivery failed", exc)

    def _resolve_targets(self) -> dict[str, dict[str, str]]:
        targets: dict[str, dict[str, str]] = {}
        discord_url = self._resolve_discord_webhook()
        if discord_url:
            targets["discord"] = {"webhook_url": discord_url}
        telegram = self._resolve_telegram()
        if telegram:
            targets["telegram"] = telegram
        if not targets:
            self._warn_once(
                "notifications.none_configured",
                "Notifications enabled but no targets configured",
            )
        return targets

    def _targets_available(self) -> bool:
        if self._discord_enabled and self._peek_discord_webhook():
            return True
        if self._telegram_enabled and self._peek_telegram():
            return True
        return False

    def _peek_discord_webhook(self) -> bool:
        env_key = self._discord.get("webhook_url_env")
        if not env_key or not isinstance(env_key, str):
            return False
        return bool(os.environ.get(env_key))

    def _peek_telegram(self) -> bool:
        token_key = self._telegram.get("bot_token_env")
        chat_id_key = self._telegram.get("chat_id_env")
        if not token_key or not chat_id_key:
            return False
        if not isinstance(token_key, str) or not isinstance(chat_id_key, str):
            return False
        return bool(os.environ.get(token_key) and os.environ.get(chat_id_key))

    def _resolve_discord_webhook(self) -> Optional[str]:
        if not self._discord_enabled:
            return None
        env_key = self._discord.get("webhook_url_env")
        if env_key and isinstance(env_key, str):
            value = os.environ.get(env_key)
            if value:
                return value
            if self._discord.get("enabled") is True:
                self._warn_once(
                    "discord.webhook_url_env.missing",
                    f"Discord webhook env var missing: {env_key}",
                )
        return None

    def _resolve_telegram(self) -> Optional[dict[str, str]]:
        if not self._telegram_enabled:
            return None
        token_key = self._telegram.get("bot_token_env")
        chat_id_key = self._telegram.get("chat_id_env")
        token = os.environ.get(token_key) if isinstance(token_key, str) else None
        chat_id = os.environ.get(chat_id_key) if isinstance(chat_id_key, str) else None
        if token and chat_id:
            return {"bot_token": token, "chat_id": chat_id}
        if self._telegram.get("enabled") is True:
            if not token and token_key:
                self._warn_once(
                    "telegram.bot_token_env.missing",
                    f"Telegram bot token env var missing: {token_key}",
                )
            if not chat_id and chat_id_key:
                self._warn_once(
                    "telegram.chat_id_env.missing",
                    f"Telegram chat id env var missing: {chat_id_key}",
                )
        return None

    def _send_sync(
        self, client: httpx.Client, targets: dict[str, dict[str, str]], message: str
    ) -> None:
        if "discord" in targets:
            try:
                self._send_discord_sync(
                    client, targets["discord"]["webhook_url"], message
                )
            except Exception as exc:
                self._log_delivery_failure("discord", exc)
        if "telegram" in targets:
            telegram = targets["telegram"]
            try:
                self._send_telegram_sync(
                    client,
                    telegram["bot_token"],
                    telegram["chat_id"],
                    message,
                )
            except Exception as exc:
                self._log_delivery_failure("telegram", exc)

    async def _send_async(
        self,
        client: httpx.AsyncClient,
        targets: dict[str, dict[str, str]],
        message: str,
    ) -> None:
        if "discord" in targets:
            try:
                await self._send_discord_async(
                    client, targets["discord"]["webhook_url"], message
                )
            except Exception as exc:
                self._log_delivery_failure("discord", exc)
        if "telegram" in targets:
            telegram = targets["telegram"]
            try:
                await self._send_telegram_async(
                    client,
                    telegram["bot_token"],
                    telegram["chat_id"],
                    message,
                )
            except Exception as exc:
                self._log_delivery_failure("telegram", exc)

    def _send_discord_sync(
        self, client: httpx.Client, webhook_url: str, message: str
    ) -> None:
        response = client.post(webhook_url, json={"content": message})
        response.raise_for_status()

    async def _send_discord_async(
        self, client: httpx.AsyncClient, webhook_url: str, message: str
    ) -> None:
        response = await client.post(webhook_url, json={"content": message})
        response.raise_for_status()

    def _send_telegram_sync(
        self, client: httpx.Client, bot_token: str, chat_id: str, message: str
    ) -> None:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        response = client.post(url, json=payload)
        response.raise_for_status()

    async def _send_telegram_async(
        self, client: httpx.AsyncClient, bot_token: str, chat_id: str, message: str
    ) -> None:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        response = await client.post(url, json=payload)
        response.raise_for_status()

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned_missing:
            return
        self._warned_missing.add(key)
        self._log_warning(message)

    def _log_delivery_failure(self, target: str, exc: Exception) -> None:
        self._log_warning(f"Notification delivery failed for {target}", exc)

    def _log_warning(self, message: str, exc: Optional[Exception] = None) -> None:
        try:
            if exc is not None:
                self.logger.warning("%s: %s", message, exc)
            else:
                self.logger.warning("%s", message)
        except Exception:
            pass
