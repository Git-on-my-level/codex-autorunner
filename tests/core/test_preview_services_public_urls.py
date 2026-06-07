from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from codex_autorunner.core.preview_services.public_urls import (
    resolve_public_hub_base_url,
    resolve_user_facing_preview_url,
)


@dataclass
class ConfigStub:
    server_base_path: str = ""
    server_allowed_origins: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def test_explicit_preview_public_base_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAR_PREVIEW_PUBLIC_BASE_URL", raising=False)
    config = ConfigStub(
        raw={"preview_services": {"public_base_url": "https://car.example.test/car/"}}
    )

    assert resolve_public_hub_base_url(config) == "https://car.example.test/car"
    assert (
        resolve_user_facing_preview_url(config, "/preview/p/token/")
        == "https://car.example.test/car/preview/p/token/"
    )


def test_public_base_url_derives_from_single_non_loopback_https_origin() -> None:
    config = ConfigStub(
        server_base_path="/car",
        server_allowed_origins=[
            "http://127.0.0.1:4517",
            "http://localhost:4517",
            "https://davids-mac-mini-m4.tail76ea03.ts.net",
        ],
    )

    assert (
        resolve_public_hub_base_url(config)
        == "https://davids-mac-mini-m4.tail76ea03.ts.net/car"
    )
    assert (
        resolve_user_facing_preview_url(config, "/preview/p/token/")
        == "https://davids-mac-mini-m4.tail76ea03.ts.net/car/preview/p/token/"
    )


def test_public_base_url_deduplicates_identical_non_loopback_origins() -> None:
    config = ConfigStub(
        server_base_path="/car",
        server_allowed_origins=[
            "https://davids-mac-mini-m4.tail76ea03.ts.net",
            "https://davids-mac-mini-m4.tail76ea03.ts.net",
        ],
    )

    assert (
        resolve_public_hub_base_url(config)
        == "https://davids-mac-mini-m4.tail76ea03.ts.net/car"
    )


def test_base_path_override_applies_to_derived_public_base_url() -> None:
    config = ConfigStub(
        server_allowed_origins=["https://davids-mac-mini-m4.tail76ea03.ts.net"],
    )

    assert (
        resolve_user_facing_preview_url(
            config,
            "/preview/p/token/",
            base_path_override="/car",
        )
        == "https://davids-mac-mini-m4.tail76ea03.ts.net/car/preview/p/token/"
    )


def test_base_path_override_applies_to_relative_fallback_url() -> None:
    config = ConfigStub()

    assert (
        resolve_user_facing_preview_url(
            config,
            "/preview/p/token/",
            base_path_override="/car",
        )
        == "/car/preview/p/token/"
    )


def test_ambiguous_public_origins_fall_back_to_base_path_relative_url() -> None:
    config = ConfigStub(
        server_base_path="/car",
        server_allowed_origins=[
            "https://one.example.test",
            "https://two.example.test",
        ],
    )

    assert resolve_public_hub_base_url(config) is None
    assert resolve_user_facing_preview_url(config, "/preview/p/token/") == (
        "/car/preview/p/token/"
    )


def test_loopback_only_origins_fall_back_to_base_path_relative_url() -> None:
    config = ConfigStub(
        server_base_path="/car",
        server_allowed_origins=[
            "http://127.0.0.1:4517",
            "http://localhost:4517",
        ],
    )

    assert resolve_public_hub_base_url(config) is None
    assert resolve_user_facing_preview_url(config, "/preview/p/token/") == (
        "/car/preview/p/token/"
    )
