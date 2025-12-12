# Voice Input Manual Checklist

- **Config sanity**: toggle `voice.enabled` on/off and hit `/api/voice/config`; verify provider/model surface and `warn_on_remote_api` flag match env overrides (`CODEX_AUTORUNNER_VOICE_*`), and chunk/push-to-talk thresholds reflect latency mode (realtime/balanced/quality).
- **Provider selection**: set `voice.provider` to `openai_whisper` with a valid key env; confirm `/api/voice/transcribe` accepts audio and redacts session metadata unless `redact_request` is false; ensure unknown providers are rejected.
- **Web UI flow**: open docs chat, tap/hold mic; first run should prompt opt-in when warnings are enabled, permission denial shows inline error, success returns a transcript inserted into the input; simulate upload failure by disabling network and confirm retry badge + Shift+tap re-record works.
- **TUI/terminal flow**: connect terminal panel, verify mic button + Alt+V hold-to-talk only show after connection; opt-in prompt matches web copy, transcript injects into PTY stream, and retry/permission errors surface near the toolbar.
- **Latency/quality knobs**: flip `CODEX_AUTORUNNER_VOICE_LATENCY` between `realtime|balanced|quality` (or adjust `chunk_ms`) and confirm recorder chunk cadence updates (shorter chunks for realtime, longer for quality) and auto-stop still triggers on silence/max duration.
- **Privacy + cleanup**: confirm warning logs mention remote API without raw audio; abort mid-capture leaves no pending blobs; tracks stop when retrying or navigating away.
