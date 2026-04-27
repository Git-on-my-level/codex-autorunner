from __future__ import annotations

from pathlib import Path

import yaml

from codex_autorunner.core.config import CONFIG_FILENAME, load_repo_config
from codex_autorunner.core.notifications import NotificationManager


class _DummyResponse:
    def raise_for_status(self) -> None:
        return None


class _DummyClient:
    def __init__(self, calls: list[tuple[str, dict]]) -> None:
        self._calls = calls

    def __enter__(self) -> "_DummyClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        return False

    def post(self, url: str, json: dict) -> _DummyResponse:
        self._calls.append((url, json))
        return _DummyResponse()


def _make_config(tmp_path: Path, overrides: dict) -> object:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    config_path = hub_root / CONFIG_FILENAME
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            {"mode": "hub", "repo_defaults": overrides},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    repo_root = hub_root / "repo"
    repo_root.mkdir()
    return load_repo_config(repo_root, hub_path=hub_root)


def test_notifications_send_with_thread_map(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.notifications.httpx.Client",
        lambda timeout: _DummyClient(calls),
    )
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("CHAT_ID", "chat")

    config = _make_config(
        tmp_path,
        overrides={
            "notifications": {
                "enabled": True,
                "events": ["tui_idle"],
                "telegram": {
                    "bot_token_env": "BOT_TOKEN",
                    "chat_id_env": "CHAT_ID",
                    "thread_id_map": {str(tmp_path): 99},
                },
            }
        },
    )
    manager = NotificationManager(config)  # type: ignore[arg-type]
    manager.notify_tui_idle(session_id="abc", idle_seconds=5, repo_path=str(tmp_path))

    assert calls
    payload = calls[0][1]
    assert payload["message_thread_id"] == 99


def test_notifications_respect_event_filter(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.notifications.httpx.Client",
        lambda timeout: _DummyClient(calls),
    )
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("CHAT_ID", "chat")

    config = _make_config(
        tmp_path,
        overrides={
            "notifications": {
                "enabled": True,
                "events": ["run_finished"],
                "telegram": {
                    "bot_token_env": "BOT_TOKEN",
                    "chat_id_env": "CHAT_ID",
                },
            }
        },
    )
    manager = NotificationManager(config)  # type: ignore[arg-type]
    manager.notify_tui_idle(session_id="abc", idle_seconds=5, repo_path=str(tmp_path))

    assert calls == []
