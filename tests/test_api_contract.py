from fastapi.testclient import TestClient

from codex_autorunner.server import create_app


def test_repo_openapi_contract_has_core_paths(repo) -> None:
    app = create_app(repo)
    client = TestClient(app)

    schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    expected = {
        "/api/state": {"get"},
        "/api/version": {"get"},
        "/api/logs": {"get"},
        "/api/docs": {"get"},
        "/api/docs/{kind}": {"put"},
        "/api/docs/{kind}/chat": {"post"},
        "/api/ingest-spec": {"post"},
        "/api/docs/clear": {"post"},
        "/api/snapshot": {"get", "post"},
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
