from __future__ import annotations

from typing import Any, Optional


def _is_int_token(value: str) -> bool:
    return bool(value) and value.lstrip("-").isdigit()


def _parse_telegram_target(
    chat_id_raw: str, thread_id_raw: Optional[str] = None
) -> Optional[dict[str, Any]]:
    chat_id = chat_id_raw.strip()
    if not _is_int_token(chat_id):
        return None
    target: dict[str, Any] = {
        "kind": "chat",
        "platform": "telegram",
        "chat_id": str(int(chat_id)),
    }
    if thread_id_raw is not None:
        thread_id = thread_id_raw.strip()
        if not _is_int_token(thread_id):
            return None
        target["thread_id"] = str(int(thread_id))
    return target


def parse_pma_target_ref(ref: str) -> Optional[dict[str, Any]]:
    value = ref.strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered == "web":
        return {"kind": "web"}
    if lowered.startswith("local:"):
        local_path = value.split(":", 1)[1].strip()
        if not local_path:
            return None
        return {"kind": "local", "path": local_path}
    if lowered.startswith("telegram:"):
        parts = value.split(":")
        if len(parts) not in {2, 3}:
            return None
        return _parse_telegram_target(parts[1], parts[2] if len(parts) == 3 else None)
    if lowered.startswith("discord:"):
        channel_id = value.split(":", 1)[1].strip()
        if not channel_id:
            return None
        return {"kind": "chat", "platform": "discord", "chat_id": channel_id}
    if lowered.startswith("chat:"):
        parts = value.split(":")
        if len(parts) not in {3, 4}:
            return None
        platform = parts[1].strip().lower()
        chat_id = parts[2].strip()
        if platform == "telegram":
            return _parse_telegram_target(
                chat_id, parts[3] if len(parts) == 4 else None
            )
        if platform == "discord":
            if len(parts) != 3 or not chat_id:
                return None
            return {"kind": "chat", "platform": "discord", "chat_id": chat_id}
    return None


def format_pma_target_label(target: dict[str, Any]) -> str:
    label = target.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    kind = target.get("kind")
    if kind == "web":
        return "web target"
    if kind == "local":
        path = target.get("path")
        if isinstance(path, str) and path.strip():
            return path.strip()
        return ""
    if kind == "chat":
        platform = target.get("platform")
        chat_id = target.get("chat_id")
        thread_id = target.get("thread_id")
        if isinstance(platform, str) and isinstance(chat_id, str):
            if isinstance(thread_id, str) and thread_id:
                return f"{platform}:{chat_id}:{thread_id}"
            return f"{platform}:{chat_id}"
    return ""


def pma_targets_usage() -> str:
    return "\n".join(
        [
            "Refs:",
            "web",
            "local:<path>",
            "telegram:<chat_id>[:<thread_id>]",
            "discord:<channel_id>",
            "chat:telegram:<chat_id>[:<thread_id>] (target key format)",
            "chat:discord:<channel_id> (target key format)",
        ]
    )


__all__ = [
    "format_pma_target_label",
    "parse_pma_target_ref",
    "pma_targets_usage",
]
