from fastapi.testclient import TestClient

from codex_autorunner.server import create_hub_app


def test_repo_openapi_contract_has_core_paths(hub_env) -> None:
    app = create_hub_app(hub_env.hub_root)
    client = TestClient(app)

    schema = client.get(f"/repos/{hub_env.repo_id}/openapi.json").json()
    paths = schema["paths"]

    expected = {
        "/api/version": {"get"},
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
