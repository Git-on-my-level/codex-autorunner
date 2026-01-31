from datetime import datetime, timezone


def now_iso_utc_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


now_iso = now_iso_utc_z


__all__ = ["now_iso_utc_z", "now_iso"]
