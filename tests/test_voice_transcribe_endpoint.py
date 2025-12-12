from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.server import create_app


def _client(repo_root: Path) -> TestClient:
    app = create_app(repo_root)
    return TestClient(app)


def test_voice_transcribe_reads_uploaded_file_bytes(repo: Path) -> None:
    """
    The web UI uploads audio as multipart/form-data (FormData).
    The server must read the uploaded file bytes, not the raw multipart body.

    If it incorrectly reads the raw body, even an empty uploaded file would look non-empty
    (multipart boundaries) and we'd get a provider error instead of empty_audio.
    """

    client = _client(repo)
    res = client.post(
        "/api/voice/transcribe",
        files={"file": ("voice.webm", b"", "audio/webm")},
    )
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == "No audio received"
