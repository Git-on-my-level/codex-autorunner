from pathlib import Path

from codex_autorunner.core.app_server_threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from codex_autorunner.integrations.app_server.event_buffer import AppServerEventBuffer
from codex_autorunner.server import create_hub_app


def test_hub_app_state_includes_pma_context(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)

    assert hasattr(app.state, "app_server_threads")
    assert isinstance(app.state.app_server_threads, AppServerThreadRegistry)
    assert app.state.app_server_threads.path == default_app_server_threads_path(
        Path(hub_env.hub_root)
    )

    assert hasattr(app.state, "app_server_events")
    assert isinstance(app.state.app_server_events, AppServerEventBuffer)

    assert hasattr(app.state, "opencode_supervisor")
    assert hasattr(app.state, "opencode_prune_interval")
