from __future__ import annotations

from typing import Any

CHAT_EXECUTION_JOURNAL_NOTICE_KIND = "chat_execution_journal"
COMPACTION_SUMMARY_NOTICE_KIND = "compaction_summary"
DECODE_FAILURE_NOTICE_KIND = "decode_failure"
PROVIDER_CONTEXT_COMPACTION_NOTICE_KIND = "provider_context_compaction"
CONTEXT_COMPACTION_NOTICE_KIND = "context_compaction"

INTERNAL_RUN_NOTICE_KINDS = frozenset(
    {
        CHAT_EXECUTION_JOURNAL_NOTICE_KIND,
        COMPACTION_SUMMARY_NOTICE_KIND,
        DECODE_FAILURE_NOTICE_KIND,
    }
)


def is_internal_run_notice_kind(kind: Any) -> bool:
    text = str(kind or "").strip().lower()
    token = "_".join(text.replace(".", "_").replace("-", "_").split())
    return token in INTERNAL_RUN_NOTICE_KINDS


def is_context_compaction_notice_kind(kind: Any) -> bool:
    text = str(kind or "").strip().lower()
    token = "_".join(text.replace(".", "_").replace("-", "_").split())
    return token in {
        PROVIDER_CONTEXT_COMPACTION_NOTICE_KIND,
        CONTEXT_COMPACTION_NOTICE_KIND,
        "runtime_context_compaction",
        "provider_compaction",
        "runtime_compaction",
    }
