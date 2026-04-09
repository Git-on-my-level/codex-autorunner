# ruff: noqa: F401

import pytest

from tests.pma_routes_support import (
    test_pma_context_snapshot,
    test_pma_context_snapshot_writes_via_to_thread,
    test_pma_docs_disabled,
    test_pma_docs_get,
    test_pma_docs_get_nonexistent,
    test_pma_docs_list,
    test_pma_docs_list_includes_auto_prune_metadata,
    test_pma_docs_list_migrates_legacy_doc_into_canonical,
    test_pma_docs_put,
    test_pma_docs_put_invalid_content_type,
    test_pma_docs_put_invalid_name,
    test_pma_docs_put_too_large,
    test_pma_docs_put_writes_via_to_thread,
    test_pma_files_bulk_delete_leaves_legacy_duplicates_hidden,
    test_pma_files_bulk_delete_removes_only_canonical_entries,
    test_pma_files_delete_removes_only_resolved_file,
    test_pma_files_download_rejects_legacy_path,
    test_pma_files_invalid_box,
    test_pma_files_list_empty,
    test_pma_files_list_ignores_legacy_sources,
    test_pma_files_list_marks_old_inbox_files_as_likely_stale,
    test_pma_files_list_waits_for_bulk_delete_to_finish,
    test_pma_files_outbox,
    test_pma_files_rejects_invalid_filenames,
    test_pma_files_returns_404_for_nonexistent_files,
    test_pma_files_size_limit,
    test_pma_files_upload_list_download_delete,
)

pytestmark = pytest.mark.slow
