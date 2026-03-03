from __future__ import annotations

import pytest
from fastapi import HTTPException

from codex_autorunner.surfaces.web.routes import templates as template_routes
from codex_autorunner.surfaces.web.services.responses import (
    error_detail,
    error_response,
    ok_response,
)
from codex_autorunner.surfaces.web.services.validation import (
    normalize_agent_id,
    normalize_optional_string,
    normalize_required_string,
    normalize_string_lower,
)


def test_normalize_optional_string_allows_blank_by_default() -> None:
    assert normalize_optional_string("  ", "field") is None
    assert normalize_optional_string("  value  ", "field") == "value"


def test_normalize_optional_string_can_reject_blank() -> None:
    with pytest.raises(HTTPException) as exc_info:
        normalize_optional_string("  ", "field", allow_blank=False)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "field must not be empty"


def test_normalize_optional_string_uses_detail_builder() -> None:
    with pytest.raises(HTTPException) as exc_info:
        normalize_optional_string(
            "a\nb",
            "field",
            require_single_line=True,
            detail_builder=lambda msg: {"code": "validation_error", "message": msg},
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "code": "validation_error",
        "message": "field must be single-line",
    }


def test_normalize_required_string_validates_type_and_content() -> None:
    assert normalize_required_string(" value ", "field") == "value"
    with pytest.raises(HTTPException):
        normalize_required_string(None, "field")
    with pytest.raises(HTTPException):
        normalize_required_string(" ", "field")


def test_normalize_string_lower_and_agent_id() -> None:
    assert normalize_string_lower(" Codex ") == "codex"
    assert normalize_string_lower(123) is None
    assert normalize_agent_id(" OpenCode ") == "opencode"
    assert normalize_agent_id(None) == "codex"


def test_response_helpers_build_stable_shapes() -> None:
    assert ok_response(saved=3) == {"status": "ok", "saved": 3}
    assert error_response("failed", source="route") == {
        "status": "error",
        "detail": "failed",
        "source": "route",
    }
    assert error_detail("validation_error", "bad value", meta={"field": "id"}) == {
        "code": "validation_error",
        "message": "bad value",
        "meta": {"field": "id"},
    }


def test_templates_string_normalizers_keep_validation_error_shape() -> None:
    with pytest.raises(HTTPException) as exc_info:
        template_routes._normalize_required_string(" ", "id")
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "code": "validation_error",
        "message": "id must not be empty",
    }

    with pytest.raises(HTTPException) as exc_info2:
        template_routes._normalize_optional_string("\n", "url")
    assert exc_info2.value.status_code == 400
    assert exc_info2.value.detail == {
        "code": "validation_error",
        "message": "url must not be empty",
    }
