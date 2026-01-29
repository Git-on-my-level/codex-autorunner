from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.server import create_hub_app


def _client(hub_env) -> TestClient:
    app = create_hub_app(hub_env.hub_root)
    return TestClient(app)


def test_voice_transcribe_reads_uploaded_file_bytes(hub_env, repo: Path) -> None:
    """
    The web UI uploads audio as multipart/form-data (FormData).
    The server must read the uploaded file bytes, not the raw multipart body.

    If it incorrectly reads the raw body, even an empty uploaded file would look non-empty
    (multipart boundaries) and we'd get a provider error instead of empty_audio.
    """

    client = _client(hub_env)
    res = client.post(
        f"/repos/{hub_env.repo_id}/api/voice/transcribe",
        files={"file": ("voice.webm", b"", "audio/webm")},
    )
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == "No audio received"
