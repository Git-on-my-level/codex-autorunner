from fastapi.testclient import TestClient

from codex_autorunner.server import create_hub_app


def test_repo_openapi_contract_has_core_paths(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    schema = client.get(f"/repos/{hub_env.repo_id}/openapi.json").json()
    paths = schema["paths"]

    expected = {
        "/api/version": {"get"},
        "/api/archive/snapshots": {"get"},
        "/api/archive/snapshots/{snapshot_id}": {"get"},
        "/api/archive/tree": {"get"},
        "/api/archive/file": {"get"},
        "/api/archive/download": {"get"},
        "/api/contextspace": {"get"},
        "/api/contextspace/{kind}": {"put"},
        "/api/contextspace/file": {"get", "put", "delete"},
        "/api/contextspace/files": {"get"},
        "/api/contextspace/tree": {"get"},
        "/api/contextspace/upload": {"post"},
        "/api/contextspace/download": {"get"},
        "/api/contextspace/download-zip": {"get"},
        "/api/contextspace/folder": {"post", "delete"},
        "/api/contextspace/spec/ingest": {"post"},
        "/api/file-chat": {"post"},
        "/api/file-chat/pending": {"get"},
        "/api/file-chat/apply": {"post"},
        "/api/file-chat/discard": {"post"},
        "/api/file-chat/interrupt": {"post"},
        "/api/run/start": {"post"},
        "/api/run/stop": {"post"},
        "/api/sessions": {"get"},
        "/api/usage": {"get"},
        "/api/usage/series": {"get"},
        "/api/terminal/image": {"post"},
        "/api/voice/config": {"get"},
        "/api/voice/transcribe": {"post"},
        "/api/review/status": {"get"},
        "/api/review/start": {"post"},
        "/api/review/stop": {"post"},
        "/api/review/reset": {"post"},
        "/api/review/artifact": {"get"},
    }

    for path, methods in expected.items():
        assert path in paths
        assert methods.issubset(set(paths[path].keys()))
