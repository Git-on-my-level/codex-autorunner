from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.browser.models import DEFAULT_VIEWPORT
from codex_autorunner.browser.orchestration import (
    DemoWorkflowConfig,
    DemoWorkflowConfigError,
    DemoWorkflowExecutionError,
    WorkflowDemoCaptureConfig,
    WorkflowExportConfig,
    WorkflowPublishConfig,
    WorkflowServiceConfig,
    load_demo_workflow_config,
    run_demo_workflow,
)
from codex_autorunner.browser.runtime import BrowserRunResult
from codex_autorunner.browser.server import BrowserServeSession


class _FakeRuntime:
    def __init__(self, result: BrowserRunResult) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    def capture_demo(self, **kwargs: object) -> BrowserRunResult:
        self.calls.append(kwargs)
        return self._result


def _workflow_config(
    *,
    tmp_path: Path,
    services: tuple[WorkflowServiceConfig, ...],
    export: WorkflowExportConfig,
    publish: WorkflowPublishConfig,
) -> DemoWorkflowConfig:
    script = tmp_path / "workflow-demo.yaml"
    script.write_text("version: 1\nsteps: []\n", encoding="utf-8")
    out_dir = tmp_path / "workflow-out"
    return DemoWorkflowConfig(
        workflow_path=tmp_path / "workflow.yaml",
        services=services,
        demo=WorkflowDemoCaptureConfig(
            script_path=script,
            path="/",
            viewport=DEFAULT_VIEWPORT,
            record_video=True,
            trace_mode="off",
            out_dir=out_dir,
            output_name=None,
        ),
        export=export,
        publish=publish,
    )


def test_run_demo_workflow_starts_in_order_and_stops_in_reverse(tmp_path: Path) -> None:
    events: list[str] = []
    service_sessions = {
        "svc-a-cmd": BrowserServeSession(
            pid=11,
            pgid=None,
            ready_source="ready_url",
            target_url="http://127.0.0.1:3101",
            ready_url="http://127.0.0.1:3101/health",
        ),
        "svc-b-cmd": BrowserServeSession(
            pid=12,
            pgid=None,
            ready_source="ready_url",
            target_url="http://127.0.0.1:3202",
            ready_url="http://127.0.0.1:3202/health",
        ),
    }

    class _FakeSupervisor:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            self._cmd = config.serve_cmd

        def start(self) -> None:
            events.append(f"start:{self._cmd}")

        def wait_until_ready(self) -> BrowserServeSession:
            events.append(f"ready:{self._cmd}")
            return service_sessions[self._cmd]

        def stop(self) -> None:
            events.append(f"stop:{self._cmd}")

    out_dir = tmp_path / "workflow-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path = out_dir / "demo-video.webm"
    summary_path = out_dir / "demo-summary.json"
    video_path.write_bytes(b"video")
    summary_path.write_text("{}", encoding="utf-8")
    runtime = _FakeRuntime(
        BrowserRunResult(
            ok=True,
            mode="demo",
            target_url="http://127.0.0.1:3202",
            artifacts={"video": video_path, "summary": summary_path},
        )
    )
    config = _workflow_config(
        tmp_path=tmp_path,
        services=(
            WorkflowServiceConfig(
                name="svc-a",
                serve_cmd="svc-a-cmd",
                ready_url="http://127.0.0.1:3101/health",
                ready_log_pattern=None,
                cwd=None,
                env={},
                ready_timeout_seconds=3.0,
            ),
            WorkflowServiceConfig(
                name="svc-b",
                serve_cmd="svc-b-cmd",
                ready_url="http://127.0.0.1:3202/health",
                ready_log_pattern=None,
                cwd=None,
                env={},
                ready_timeout_seconds=3.0,
            ),
        ),
        export=WorkflowExportConfig(
            manifest_name="demo-export.json",
            primary_artifact=None,
        ),
        publish=WorkflowPublishConfig(
            enabled=False,
            outbox_dir=tmp_path / "outbox",
            include=(),
        ),
    )

    result = run_demo_workflow(
        config,
        runtime=runtime,  # type: ignore[arg-type]
        supervisor_factory=_FakeSupervisor,
    )

    assert [event for event in events if event.startswith("start:")] == [
        "start:svc-a-cmd",
        "start:svc-b-cmd",
    ]
    assert [event for event in events if event.startswith("ready:")] == [
        "ready:svc-a-cmd",
        "ready:svc-b-cmd",
    ]
    assert [event for event in events if event.startswith("stop:")] == [
        "stop:svc-b-cmd",
        "stop:svc-a-cmd",
    ]
    assert runtime.calls and runtime.calls[0]["base_url"] == "http://127.0.0.1:3202"
    assert result.primary_artifact_key == "video"
    assert result.primary_artifact_path == video_path
    assert result.export_manifest_path == out_dir / "demo-export.json"
    manifest = json.loads(result.export_manifest_path.read_text(encoding="utf-8"))
    assert manifest["export"]["primary_artifact_key"] == "video"
    assert manifest["publish"]["enabled"] is False


def test_run_demo_workflow_stops_started_services_on_readiness_failure(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class _FakeSupervisor:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            self._cmd = config.serve_cmd

        def start(self) -> None:
            events.append(f"start:{self._cmd}")

        def wait_until_ready(self) -> BrowserServeSession:
            events.append(f"ready:{self._cmd}")
            if self._cmd == "svc-b-cmd":
                raise RuntimeError("simulated readiness failure")
            return BrowserServeSession(
                pid=21,
                pgid=None,
                ready_source="ready_url",
                target_url="http://127.0.0.1:3303",
                ready_url="http://127.0.0.1:3303/health",
            )

        def stop(self) -> None:
            events.append(f"stop:{self._cmd}")

    config = _workflow_config(
        tmp_path=tmp_path,
        services=(
            WorkflowServiceConfig(
                name="svc-a",
                serve_cmd="svc-a-cmd",
                ready_url="http://127.0.0.1:3303/health",
                ready_log_pattern=None,
                cwd=None,
                env={},
                ready_timeout_seconds=3.0,
            ),
            WorkflowServiceConfig(
                name="svc-b",
                serve_cmd="svc-b-cmd",
                ready_url="http://127.0.0.1:3404/health",
                ready_log_pattern=None,
                cwd=None,
                env={},
                ready_timeout_seconds=3.0,
            ),
        ),
        export=WorkflowExportConfig(
            manifest_name="demo-export.json",
            primary_artifact=None,
        ),
        publish=WorkflowPublishConfig(
            enabled=False,
            outbox_dir=tmp_path / "outbox",
            include=(),
        ),
    )

    with pytest.raises(DemoWorkflowExecutionError) as exc_info:
        run_demo_workflow(
            config,
            runtime=_FakeRuntime(
                BrowserRunResult(ok=True, mode="demo", target_url=None)
            ),  # type: ignore[arg-type]
            supervisor_factory=_FakeSupervisor,
        )

    assert exc_info.value.category == "service_readiness"
    assert [event for event in events if event.startswith("stop:")] == [
        "stop:svc-b-cmd",
        "stop:svc-a-cmd",
    ]


def test_run_demo_workflow_rewrites_published_manifest_with_final_content(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    service_sessions = {
        "svc-cmd": BrowserServeSession(
            pid=15,
            pgid=None,
            ready_source="ready_url",
            target_url="http://127.0.0.1:3505",
            ready_url="http://127.0.0.1:3505/health",
        )
    }

    class _FakeSupervisor:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            self._cmd = config.serve_cmd

        def start(self) -> None:
            events.append(f"start:{self._cmd}")

        def wait_until_ready(self) -> BrowserServeSession:
            events.append(f"ready:{self._cmd}")
            return service_sessions[self._cmd]

        def stop(self) -> None:
            events.append(f"stop:{self._cmd}")

    out_dir = tmp_path / "workflow-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path = out_dir / "demo-video.webm"
    summary_path = out_dir / "demo-summary.json"
    video_path.write_bytes(b"video")
    summary_path.write_text("{}", encoding="utf-8")
    runtime = _FakeRuntime(
        BrowserRunResult(
            ok=True,
            mode="demo",
            target_url="http://127.0.0.1:3505",
            artifacts={"video": video_path, "summary": summary_path},
        )
    )
    config = _workflow_config(
        tmp_path=tmp_path,
        services=(
            WorkflowServiceConfig(
                name="svc",
                serve_cmd="svc-cmd",
                ready_url="http://127.0.0.1:3505/health",
                ready_log_pattern=None,
                cwd=None,
                env={},
                ready_timeout_seconds=3.0,
            ),
        ),
        export=WorkflowExportConfig(
            manifest_name="demo-export.json",
            primary_artifact=None,
        ),
        publish=WorkflowPublishConfig(
            enabled=True,
            outbox_dir=tmp_path / "outbox",
            include=(),
        ),
    )

    result = run_demo_workflow(
        config,
        runtime=runtime,  # type: ignore[arg-type]
        supervisor_factory=_FakeSupervisor,
    )

    published_manifest_path = tmp_path / "outbox" / "demo-export.json"
    assert published_manifest_path in result.published_artifacts

    manifest = json.loads(result.export_manifest_path.read_text(encoding="utf-8"))
    published_manifest = json.loads(published_manifest_path.read_text(encoding="utf-8"))
    expected_published = {str(path) for path in result.published_artifacts}

    assert set(manifest["publish"]["published"]) == expected_published
    assert set(published_manifest["publish"]["published"]) == expected_published


def test_run_demo_workflow_rewrites_all_published_manifest_copies(
    tmp_path: Path,
) -> None:
    class _FakeSupervisor:
        def __init__(self, _config) -> None:  # type: ignore[no-untyped-def]
            pass

        def start(self) -> None:
            return

        def wait_until_ready(self) -> BrowserServeSession:
            return BrowserServeSession(
                pid=17,
                pgid=None,
                ready_source="ready_url",
                target_url="http://127.0.0.1:3606",
                ready_url="http://127.0.0.1:3606/health",
            )

        def stop(self) -> None:
            return

    out_dir = tmp_path / "workflow-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path = out_dir / "demo-video.webm"
    video_path.write_bytes(b"video")
    runtime = _FakeRuntime(
        BrowserRunResult(
            ok=True,
            mode="demo",
            target_url="http://127.0.0.1:3606",
            artifacts={"video": video_path},
        )
    )
    config = _workflow_config(
        tmp_path=tmp_path,
        services=(
            WorkflowServiceConfig(
                name="svc",
                serve_cmd="svc-cmd",
                ready_url="http://127.0.0.1:3606/health",
                ready_log_pattern=None,
                cwd=None,
                env={},
                ready_timeout_seconds=3.0,
            ),
        ),
        export=WorkflowExportConfig(
            manifest_name="demo-export.json",
            primary_artifact=None,
        ),
        publish=WorkflowPublishConfig(
            enabled=True,
            outbox_dir=tmp_path / "outbox",
            include=("manifest", "manifest"),
        ),
    )

    result = run_demo_workflow(
        config,
        runtime=runtime,  # type: ignore[arg-type]
        supervisor_factory=_FakeSupervisor,
    )

    expected_published = {str(path) for path in result.published_artifacts}
    for published_path in result.published_artifacts:
        payload = json.loads(published_path.read_text(encoding="utf-8"))
        assert set(payload["publish"]["published"]) == expected_published


def test_load_demo_workflow_config_expands_home_before_repo_root_rebase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    workflow_dir = repo_root / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    script_path = workflow_dir / "demo-script.yaml"
    script_path.write_text("version: 1\nsteps: []\n", encoding="utf-8")

    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home_dir))

    workflow_path = workflow_dir / "workflow.yaml"
    workflow_path.write_text(
        """
services:
  - name: app
    serve_cmd: npm run dev
    ready_url: http://127.0.0.1:3000/health
demo:
  script: demo-script.yaml
  out_dir: ~/renders
publish:
  enabled: true
  outbox_dir: ~/outbox
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_demo_workflow_config(workflow_path=workflow_path, repo_root=repo_root)

    assert config.demo.out_dir == (home_dir / "renders").resolve()
    assert config.publish.outbox_dir == (home_dir / "outbox").resolve()


def test_load_demo_workflow_config_wraps_expanduser_errors(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    workflow_dir = repo_root / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    script_path = workflow_dir / "demo-script.yaml"
    script_path.write_text("version: 1\nsteps: []\n", encoding="utf-8")

    workflow_path = workflow_dir / "workflow.yaml"
    workflow_path.write_text(
        """
services:
  - name: app
    serve_cmd: npm run dev
    ready_url: http://127.0.0.1:3000/health
demo:
  script: demo-script.yaml
  out_dir: ~missing-user/renders
publish:
  enabled: true
  outbox_dir: ./outbox
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DemoWorkflowConfigError) as exc_info:
        load_demo_workflow_config(workflow_path=workflow_path, repo_root=repo_root)

    assert "Unable to expand path '~missing-user/renders'" in str(exc_info.value)


def test_load_demo_workflow_config_wraps_expanduser_errors_for_demo_script(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    workflow_dir = repo_root / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)

    workflow_path = workflow_dir / "workflow.yaml"
    workflow_path.write_text(
        """
services:
  - name: app
    serve_cmd: npm run dev
    ready_url: http://127.0.0.1:3000/health
demo:
  script: ~missing-user/demo-script.yaml
publish:
  enabled: false
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DemoWorkflowConfigError) as exc_info:
        load_demo_workflow_config(workflow_path=workflow_path, repo_root=repo_root)

    assert "Unable to expand path '~missing-user/demo-script.yaml'" in str(
        exc_info.value
    )
