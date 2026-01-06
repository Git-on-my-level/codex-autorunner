"""Compatibility shim for moved core locks helpers."""

from .core.locks import LockInfo, process_alive, read_lock_info, write_lock_info

__all__ = ["LockInfo", "process_alive", "read_lock_info", "write_lock_info"]
