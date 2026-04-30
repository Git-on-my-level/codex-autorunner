from __future__ import annotations

import textwrap

import pytest

from codex_autorunner.core.apps.manifest import (
    ManifestError,
    load_app_manifest,
    parse_app_manifest,
)
from codex_autorunner.core.apps.paths import AppPathError, validate_app_path
from codex_autorunner.core.apps.refs import AppRef, parse_app_ref, validate_app_id


def _minimal_manifest(**overrides):
    base = {
        "schema_version": 1,
        "id": "test.example",
        "name": "Test App",
        "version": "0.1.0",
    }
    base.update(overrides)
    return base


class TestParseAppRef:
    def test_basic(self):
        ref = parse_app_ref("blessed:apps/autoresearch@main")
        assert ref == AppRef(
            repo_id="blessed", app_path="apps/autoresearch", ref="main"
        )

    def test_no_ref(self):
        ref = parse_app_ref("blessed:apps/autoresearch")
        assert ref == AppRef(repo_id="blessed", app_path="apps/autoresearch", ref=None)

    def test_no_colon(self):
        with pytest.raises(ValueError, match="REPO_ID:APP_PATH"):
            parse_app_ref("blessed")

    def test_empty_repo_id(self):
        with pytest.raises(ValueError, match="missing repo_id"):
            parse_app_ref(":apps/autoresearch")

    def test_empty_path(self):
        with pytest.raises(ValueError, match="missing app_path"):
            parse_app_ref("blessed:")

    def test_empty_ref_after_at(self):
        with pytest.raises(ValueError, match="missing ref"):
            parse_app_ref("blessed:apps/app@")


class TestValidateAppId:
    def test_valid(self):
        assert validate_app_id("blessed.autoresearch") == "blessed.autoresearch"

    def test_valid_simple(self):
        assert validate_app_id("my-app_v2") == "my-app_v2"

    def test_invalid_starts_with_dot(self):
        with pytest.raises(ValueError, match="invalid app id"):
            validate_app_id(".bad")

    def test_invalid_uppercase(self):
        with pytest.raises(ValueError, match="invalid app id"):
            validate_app_id("Bad")

    def test_invalid_too_short(self):
        with pytest.raises(ValueError, match="invalid app id"):
            validate_app_id("a")


class TestValidateAppPath:
    def test_valid_relative(self):
        p = validate_app_path("scripts/run.py")
        assert str(p) == "scripts/run.py"

    def test_rejects_absolute(self):
        with pytest.raises(AppPathError, match="absolute"):
            validate_app_path("/etc/passwd")

    def test_rejects_dot_dot(self):
        with pytest.raises(AppPathError, match=r"\.\."):
            validate_app_path("../etc/passwd")

    def test_rejects_backslash(self):
        with pytest.raises(AppPathError, match="backslash"):
            validate_app_path("a\\b")

    def test_rejects_empty(self):
        with pytest.raises(AppPathError, match="empty"):
            validate_app_path("")

    def test_rejects_glob_by_default(self):
        with pytest.raises(AppPathError, match="glob"):
            validate_app_path("state/**")


class TestMinimalValidManifest:
    def test_parses(self):
        m = parse_app_manifest(_minimal_manifest())
        assert m.schema_version == 1
        assert m.id == "test.example"
        assert m.name == "Test App"
        assert m.version == "0.1.0"
        assert m.tools == {}
        assert m.hooks == []
        assert m.templates == {}


class TestFullManifest:
    def test_parses_full_spec(self):
        data = _minimal_manifest(
            description="Full app.",
            entrypoint={"template": "templates/bootstrap.md"},
            inputs={
                "goal": {"required": True, "description": "The goal."},
                "metric": {"required": False, "description": "A metric."},
            },
            templates={
                "bootstrap": {"path": "templates/bootstrap.md", "description": "Boot."},
                "iteration": {"path": "templates/iteration.md", "description": "Iter."},
            },
            tools={
                "record-iteration": {
                    "description": "Record one iteration.",
                    "argv": ["python3", "scripts/record.py"],
                    "timeout_seconds": 30,
                },
                "render-card": {
                    "description": "Render.",
                    "argv": ["python3", "scripts/render.py"],
                    "timeout_seconds": 120,
                    "outputs": [
                        {
                            "kind": "image",
                            "path": "artifacts/summary.png",
                            "label": "Card",
                        },
                        {
                            "kind": "markdown",
                            "path": "artifacts/summary.md",
                            "label": "Summary",
                        },
                    ],
                },
            },
            hooks={
                "after_ticket_done": [
                    {
                        "tool": "record-iteration",
                        "when": {"ticket_frontmatter": {"app": "test.example"}},
                        "failure": "pause",
                    }
                ],
                "after_flow_terminal": [
                    {
                        "tool": "render-card",
                        "when": {"status": "completed"},
                        "failure": "warn",
                    }
                ],
                "before_chat_wrapup": [
                    {"artifacts": ["artifacts/summary.png", "artifacts/summary.md"]}
                ],
                "after_flow_archive": [
                    {
                        "cleanup_paths": [
                            "state/run.json",
                            "state/iterations.jsonl",
                            "artifacts/summary.md",
                        ],
                        "failure": "warn",
                    }
                ],
            },
            permissions={
                "network": False,
                "writes": ["state/**", "artifacts/**"],
                "reads": ["**"],
            },
        )
        m = parse_app_manifest(data)

        assert m.description == "Full app."
        assert m.entrypoint is not None
        assert len(m.inputs) == 2
        assert m.inputs["goal"].required is True
        assert len(m.templates) == 2
        assert m.templates["bootstrap"].path == "templates/bootstrap.md"

        assert len(m.tools) == 2
        assert m.tools["record-iteration"].argv == ["python3", "scripts/record.py"]
        assert m.tools["render-card"].outputs[0].kind == "image"
        assert m.tools["render-card"].outputs[0].path == "artifacts/summary.png"

        assert len(m.hooks) == 4
        hook_points = {h.point for h in m.hooks}
        assert hook_points == {
            "after_ticket_done",
            "after_flow_terminal",
            "after_flow_archive",
            "before_chat_wrapup",
        }
        archive_hook = next(h for h in m.hooks if h.point == "after_flow_archive")
        assert archive_hook.entries[0].cleanup_paths == [
            "state/run.json",
            "state/iterations.jsonl",
            "artifacts/summary.md",
        ]

        assert m.permissions.network is False
        assert m.permissions.writes == ["state/**", "artifacts/**"]
        assert m.permissions.reads == ["**"]


class TestInvalidManifest:
    def test_invalid_app_id(self):
        with pytest.raises(ManifestError, match="invalid app id"):
            parse_app_manifest(_minimal_manifest(id="BAD"))

    def test_missing_name(self):
        with pytest.raises(ManifestError, match="must be a string"):
            parse_app_manifest(_minimal_manifest(name=None))

    def test_empty_name(self):
        with pytest.raises(ManifestError, match="must not be empty"):
            parse_app_manifest(_minimal_manifest(name="  "))

    def test_missing_version(self):
        with pytest.raises(ManifestError, match="must be a string"):
            parse_app_manifest(_minimal_manifest(version=None))

    def test_empty_version(self):
        with pytest.raises(ManifestError, match="must not be empty"):
            parse_app_manifest(_minimal_manifest(version=""))

    def test_unsupported_schema_version(self):
        with pytest.raises(ManifestError, match="unsupported schema_version"):
            parse_app_manifest(_minimal_manifest(schema_version=2))

    def test_schema_version_zero(self):
        with pytest.raises(ManifestError, match="unsupported schema_version"):
            parse_app_manifest(_minimal_manifest(schema_version=0))

    def test_schema_version_true_rejected(self):
        with pytest.raises(ManifestError, match="must be an integer"):
            parse_app_manifest(_minimal_manifest(schema_version=True))

    def test_tool_timeout_seconds_true_rejected(self):
        with pytest.raises(ManifestError, match="must be an integer"):
            parse_app_manifest(
                _minimal_manifest(
                    tools={"run": {"argv": ["echo"], "timeout_seconds": True}}
                )
            )

    def test_tool_timeout_seconds_zero_rejected(self):
        with pytest.raises(ManifestError, match="timeout_seconds must be positive"):
            parse_app_manifest(
                _minimal_manifest(
                    tools={"run": {"argv": ["echo"], "timeout_seconds": 0}}
                )
            )

    def test_absolute_path_in_template(self):
        with pytest.raises(AppPathError, match="absolute"):
            parse_app_manifest(
                _minimal_manifest(templates={"boot": {"path": "/etc/passwd"}})
            )

    def test_dot_dot_path_in_template(self):
        with pytest.raises(AppPathError, match=r"\.\."):
            parse_app_manifest(
                _minimal_manifest(templates={"boot": {"path": "../escape.md"}})
            )

    def test_empty_tool_argv(self):
        with pytest.raises(ManifestError, match="non-empty"):
            parse_app_manifest(_minimal_manifest(tools={"run": {"argv": []}}))

    def test_tool_argv_non_string(self):
        with pytest.raises(ManifestError, match="must be a string"):
            parse_app_manifest(
                _minimal_manifest(tools={"run": {"argv": ["python3", 42]}})
            )

    def test_unknown_hook_point(self):
        with pytest.raises(ManifestError, match="unknown hook point"):
            parse_app_manifest(
                _minimal_manifest(
                    hooks={"on_startup": [{"tool": "t"}]},
                    tools={"t": {"argv": ["echo"]}},
                )
            )

    def test_hook_references_unknown_tool(self):
        with pytest.raises(ManifestError, match="unknown tool"):
            parse_app_manifest(
                _minimal_manifest(
                    hooks={"after_ticket_done": [{"tool": "nonexistent"}]},
                )
            )

    def test_absolute_path_in_output(self):
        with pytest.raises(AppPathError, match="absolute"):
            parse_app_manifest(
                _minimal_manifest(
                    tools={
                        "render": {
                            "argv": ["echo"],
                            "outputs": [{"kind": "image", "path": "/tmp/out.png"}],
                        }
                    }
                )
            )

    def test_dot_dot_in_output_path(self):
        with pytest.raises(AppPathError, match=r"\.\."):
            parse_app_manifest(
                _minimal_manifest(
                    tools={
                        "render": {
                            "argv": ["echo"],
                            "outputs": [{"kind": "image", "path": "../escape.png"}],
                        }
                    }
                )
            )

    def test_invalid_output_kind(self):
        with pytest.raises(ManifestError, match="must be one of"):
            parse_app_manifest(
                _minimal_manifest(
                    tools={
                        "render": {
                            "argv": ["echo"],
                            "outputs": [{"kind": "video", "path": "out.mp4"}],
                        }
                    }
                )
            )

    def test_invalid_hook_failure_mode(self):
        with pytest.raises(ManifestError, match="must be one of"):
            parse_app_manifest(
                _minimal_manifest(
                    tools={"t": {"argv": ["echo"]}},
                    hooks={"after_ticket_done": [{"tool": "t", "failure": "explode"}]},
                )
            )

    def test_dot_dot_in_cleanup_path(self):
        with pytest.raises(AppPathError, match=r"\.\."):
            parse_app_manifest(
                _minimal_manifest(
                    hooks={
                        "after_flow_archive": [{"cleanup_paths": ["state/../outside"]}]
                    },
                )
            )


class TestLoadAppManifest:
    def test_loads_from_file(self, tmp_path):
        manifest_file = tmp_path / "car-app.yaml"
        manifest_file.write_text(
            textwrap.dedent(
                """\
                schema_version: 1
                id: test.file
                name: File Test
                version: "1.0"
            """
            )
        )
        m = load_app_manifest(manifest_file)
        assert m.id == "test.file"

    def test_rejects_non_mapping(self, tmp_path):
        manifest_file = tmp_path / "car-app.yaml"
        manifest_file.write_text("- just\n- a list\n")
        with pytest.raises(ManifestError, match="YAML mapping"):
            load_app_manifest(manifest_file)
