from __future__ import annotations

from pathlib import Path


def _birthtime_ns_from_stat(stat_obj: object) -> int:
    birth_ns = getattr(stat_obj, "st_birthtime_ns", None)
    if isinstance(birth_ns, int) and birth_ns > 0:
        return birth_ns
    birth = getattr(stat_obj, "st_birthtime", None)
    if isinstance(birth, (int, float)) and birth > 0:
        return int(birth * 1_000_000_000)
    return 0


def ticket_instance_token(path: Path) -> str:
    """Return a stable token for one ticket file instance.

    The token is stable for in-place edits, but changes when the file is deleted
    and recreated at the same path, preventing chat/thread state reuse.
    """
    if not path.exists():
        return "missing"
    try:
        stat_obj = path.stat()
    except OSError:
        return "missing"

    inode = int(getattr(stat_obj, "st_ino", 0) or 0)
    birth_ns = _birthtime_ns_from_stat(stat_obj)
    if inode > 0 and birth_ns > 0:
        return f"ino{inode:x}_birth{birth_ns:x}"
    if inode > 0:
        return f"ino{inode:x}"
    if birth_ns > 0:
        return f"birth{birth_ns:x}"

    # Last-resort fallback for filesystems without inode/birthtime support.
    mtime_ns = int(getattr(stat_obj, "st_mtime_ns", 0) or 0)
    size = int(getattr(stat_obj, "st_size", 0) or 0)
    return f"mtime{mtime_ns:x}_size{size:x}"


def ticket_chat_scope(index: int, path: Path) -> str:
    return f"ticket:{index}:{ticket_instance_token(path)}"


def ticket_state_key(index: int, path: Path) -> str:
    return f"ticket_{index:03d}_{ticket_instance_token(path)}"
