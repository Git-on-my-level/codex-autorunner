import re

_UNSAFE_REPO_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_repo_id(display_name: str) -> str:
    """Return a URL-safe repo id derived from display_name."""
    raw = (display_name or "").strip()
    if not raw:
        return "repo"
    safe = _UNSAFE_REPO_ID_CHARS.sub("-", raw).strip("-")
    safe = safe.strip(".")
    if not safe or safe in (".", ".."):
        return "repo"
    return safe


def ensure_unique_repo_id(candidate: str, existing: set[str]) -> str:
    if candidate not in existing:
        existing.add(candidate)
        return candidate
    suffix = 2
    while True:
        updated = f"{candidate}-{suffix}"
        if updated not in existing:
            existing.add(updated)
            return updated
        suffix += 1


def reserve_repo_id(display_name: str, existing: set[str]) -> str:
    return ensure_unique_repo_id(sanitize_repo_id(display_name), existing)


def repo_id_is_safe(value: str) -> bool:
    if not value:
        return False
    return sanitize_repo_id(value) == value
