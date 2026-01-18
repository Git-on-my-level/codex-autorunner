from codex_autorunner.core.review import ReviewService


def test_review_module_imports():
    """Test that ReviewService module can be imported."""
    from codex_autorunner.core.review import ReviewError

    assert ReviewError is not None
    assert ReviewService is not None


def test_review_service_can_be_imported():
    """Test that ReviewService can be imported without errors."""
    from codex_autorunner.core.review import ReviewService

    assert ReviewService is not None
