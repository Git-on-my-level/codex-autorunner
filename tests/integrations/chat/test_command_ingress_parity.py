from __future__ import annotations

from codex_autorunner.integrations.chat.command_ingress import (
    canonicalize_command_ingress,
)


def test_command_ingress_parity_between_path_and_string_forms() -> None:
    path_form = canonicalize_command_ingress(
        command_path=("car", "model"),
        options={"name": "gpt-5", "effort": "medium"},
    )
    string_form = canonicalize_command_ingress(
        command="car:model",
        options={"name": "gpt-5", "effort": "medium"},
    )

    assert path_form is not None
    assert string_form is not None
    assert path_form.command_path == string_form.command_path
    assert path_form.command == string_form.command
    assert path_form.options == string_form.options


def test_command_ingress_normalizes_whitespace_and_invalid_options() -> None:
    ingress = canonicalize_command_ingress(
        command="  car : agent ",
        options=["invalid-options-shape"],
    )

    assert ingress is not None
    assert ingress.command_path == ("car", "agent")
    assert ingress.options == {}


def test_command_ingress_returns_none_for_missing_command() -> None:
    assert canonicalize_command_ingress(command=None) is None
    assert canonicalize_command_ingress(command="") is None
    assert canonicalize_command_ingress(command_path=()) is None


def test_command_ingress_rejects_malformed_colon_command_strings() -> None:
    assert canonicalize_command_ingress(command=":car:agent") is None
    assert canonicalize_command_ingress(command="car::agent") is None
    assert canonicalize_command_ingress(command="  :  ") is None


def test_command_ingress_rejects_non_string_segments_in_path() -> None:
    assert canonicalize_command_ingress(command_path=("car", 7, "agent")) is None
