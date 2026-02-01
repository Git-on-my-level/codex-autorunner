import yaml

from codex_autorunner.core.templates import FetchedTemplate, inject_provenance
from codex_autorunner.core.templates.scan_cache import TemplateScanRecord


def test_inject_provenance_no_frontmatter() -> None:
    content = "# Template\nHello"
    fetched = FetchedTemplate(
        repo_id="test",
        url="https://example.com/repo",
        trusted=True,
        path="tickets/TICKET-REVIEW.md",
        ref="main",
        commit_sha="abc123",
        blob_sha="def456",
        content=content,
    )

    result = inject_provenance(content, fetched, None)

    assert result.startswith("---")
    assert result.endswith("---\n# Template\nHello")

    frontmatter_yaml = result[result.index("---") + 3 : result.index("---", 3)].strip()
    parsed = yaml.safe_load(frontmatter_yaml)
    assert parsed["template"] == "test:tickets/TICKET-REVIEW.md@main"
    assert parsed["template_commit"] == "abc123"
    assert parsed["template_blob"] == "def456"
    assert parsed["template_trusted"] is True
    assert parsed["template_scan"] == "skipped"


def test_inject_provenance_existing_frontmatter_merges() -> None:
    content = """---
agent: opencode
done: false
---

# Template
Hello
"""
    fetched = FetchedTemplate(
        repo_id="test",
        url="https://example.com/repo",
        trusted=False,
        path="tickets/TICKET-REVIEW.md",
        ref="main",
        commit_sha="abc123",
        blob_sha="def456",
        content=content,
    )

    scan_record = TemplateScanRecord(
        blob_sha="def456",
        repo_id="test",
        path="tickets/TICKET-REVIEW.md",
        ref="main",
        commit_sha="abc123",
        trusted=False,
        decision="approve",
        severity="info",
        reason="Safe",
        evidence=None,
        scanned_at="2024-01-01T00:00:00Z",
        scanner=None,
    )

    result = inject_provenance(content, fetched, scan_record)

    assert result.startswith("---")
    assert result.endswith("---\n\n# Template\nHello")

    frontmatter_yaml = result[result.index("---") + 3 : result.index("---", 3)].strip()
    parsed = yaml.safe_load(frontmatter_yaml)
    assert parsed["agent"] == "opencode"
    assert parsed["done"] is False
    assert parsed["template"] == "test:tickets/TICKET-REVIEW.md@main"
    assert parsed["template_commit"] == "abc123"
    assert parsed["template_blob"] == "def456"
    assert parsed["template_trusted"] is False
    assert parsed["template_scan"] == "approve"


def test_inject_provenance_stable_output() -> None:
    content = """---
agent: opencode
done: false
---

# Template
Hello
"""
    fetched = FetchedTemplate(
        repo_id="test",
        url="https://example.com/repo",
        trusted=True,
        path="tickets/TICKET-REVIEW.md",
        ref="main",
        commit_sha="abc123",
        blob_sha="def456",
        content=content,
    )

    result1 = inject_provenance(content, fetched, None)
    result2 = inject_provenance(content, fetched, None)

    assert result1 == result2
