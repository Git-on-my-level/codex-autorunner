from __future__ import annotations

from typing import Any

CHAT_EXECUTION_JOURNAL_NOTICE_KIND = "chat_execution_journal"
COMPACTION_SUMMARY_NOTICE_KIND = "compaction_summary"
DECODE_FAILURE_NOTICE_KIND = "decode_failure"

INTERNAL_RUN_NOTICE_KINDS = frozenset(
    {
        CHAT_EXECUTION_JOURNAL_NOTICE_KIND,
        COMPACTION_SUMMARY_NOTICE_KIND,
        DECODE_FAILURE_NOTICE_KIND,
    }
)


def is_internal_run_notice_kind(kind: Any) -> bool:
    return str(kind or "").strip().lower() in INTERNAL_RUN_NOTICE_KINDS
