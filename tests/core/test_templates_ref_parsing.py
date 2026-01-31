import pytest

from codex_autorunner.core.templates.git_mirror import (
    TemplateRef,
    parse_template_ref,
)


def test_parse_template_ref_basic():
    """Test parsing basic REPO_ID:PATH format."""
    parsed = parse_template_ref("blessed:tickets/TICKET-REVIEW.md")
    assert parsed == TemplateRef(
        repo_id="blessed",
        path="tickets/TICKET-REVIEW.md",
        ref=None,
    )


def test_parse_template_ref_with_ref():
    """Test parsing REPO_ID:PATH@REF format."""
    parsed = parse_template_ref("my-team:milestones/001-bootstrap.md@main")
    assert parsed == TemplateRef(
        repo_id="my-team",
        path="milestones/001-bootstrap.md",
        ref="main",
    )


def test_parse_template_ref_with_commit():
    """Test parsing REPO_ID:PATH@COMMIT_SHA format."""
    parsed = parse_template_ref("my-team:milestones/001-bootstrap.md@a1b2c3d4")
    assert parsed == TemplateRef(
        repo_id="my-team",
        path="milestones/001-bootstrap.md",
        ref="a1b2c3d4",
    )


def test_parse_template_ref_no_colon():
    """Test that missing colon raises ValueError."""
    with pytest.raises(ValueError, match="must be formatted as REPO_ID:PATH"):
        parse_template_ref("blessed")


def test_parse_template_ref_empty_repo_id():
    """Test that empty repo_id raises ValueError."""
    with pytest.raises(ValueError, match="missing repo_id"):
        parse_template_ref(":tickets/TICKET-REVIEW.md")


def test_parse_template_ref_empty_path_before_at():
    """Test that empty path before @ raises ValueError."""
    with pytest.raises(ValueError, match="missing path"):
        parse_template_ref("blessed:@main")


def test_parse_template_ref_empty_path_no_at():
    """Test that empty path without @ raises ValueError."""
    with pytest.raises(ValueError, match="missing path"):
        parse_template_ref("blessed:")


def test_parse_template_ref_empty_ref():
    """Test that empty ref after @ raises ValueError."""
    with pytest.raises(ValueError, match="missing ref after '@'"):
        parse_template_ref("blessed:tickets/TICKET-REVIEW.md@")


def test_parse_template_ref_multiple_at():
    """Test parsing with multiple @ symbols uses last one as ref separator."""
    parsed = parse_template_ref("my-team:path@with@symbol@test")
    assert parsed == TemplateRef(
        repo_id="my-team",
        path="path@with@symbol",
        ref="test",
    )


def test_parse_template_ref_special_characters_in_path():
    """Test parsing with special characters in path."""
    parsed = parse_template_ref("local:path/with-dashes_and_underscores.md")
    assert parsed == TemplateRef(
        repo_id="local",
        path="path/with-dashes_and_underscores.md",
        ref=None,
    )


def test_parse_template_ref_nested_path():
    """Test parsing with deeply nested path."""
    parsed = parse_template_ref("blessed:deep/nested/path/to/template.md@v1.0.0")
    assert parsed == TemplateRef(
        repo_id="blessed",
        path="deep/nested/path/to/template.md",
        ref="v1.0.0",
    )
