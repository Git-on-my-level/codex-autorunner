# Voice Input Architecture

This document outlines the shared speech input architecture for Codex Autorunner. It emphasizes reusable contracts across the web UI and TUI, opt-in voice usage, and provider pluggability so we can start with OpenAI Whisper while keeping room for other engines.

## Goals and Constraints
- Single shared module for capture + transcription that both web and TUI call into (no bespoke per-surface logic).
- Push-to-talk first: explicit activation, clear start/stop cues, auto-stop on silence or max duration.
- Opt-in and privacy-safe: remote API warning, no raw audio persistence, redact logs by default.
- Provider pluggable via config/env with latency/quality controls tuned for mobile networks.
- Resilient UX: actionable errors (permissions, network), retry paths, and reconnect after transient failures.

## Config Surface (YAML + env overrides)
- `voice.enabled` (bool, default `false`; env `CODEX_AUTORUNNER_VOICE_ENABLED`).
- `voice.provider` (string, default `openai_whisper`; env `CODEX_AUTORUNNER_VOICE_PROVIDER`).
- `voice.latency_mode` (enum `realtime|balanced|quality`; env `CODEX_AUTORUNNER_VOICE_LATENCY`).
- `voice.push_to_talk`: `{ max_ms: 15000, silence_auto_stop_ms: 1200, min_hold_ms: 150 }`.
- `voice.chunk_ms` (default `600`) and `voice.sample_rate` (default `16000`) guide capture chunking.
- `voice.warn_on_remote_api` (bool, default `true`) toggles user-facing warnings when sending audio.
- Provider-specific block:
  - `voice.providers.openai_whisper`: `{ api_key_env: "OPENAI_API_KEY", model: "whisper-1", base_url: null, temperature: 0, language: null, redact_request: true }`.
  - Future providers live under `voice.providers.<name>` with matching keys.
- Defaults live in config; env vars override for runtime toggles/keys without editing files.

## Shared Modules and Interfaces
- **VoiceConfig**: normalized config built from YAML + env overrides. Holds provider choice, chunking, push-to-talk thresholds, and privacy flags.
- **SpeechProvider** (protocol):
  - `start_stream(session: SpeechSessionMetadata) -> TranscriptionStream`.
  - `supports_streaming` flag allows fallback to buffered mode when a provider cannot stream.
  - Provider receives pre-encoded audio chunks (no heavy DSP; we lean on provider-side handling).
- **TranscriptionStream** (protocol):
  - `send_chunk(chunk: AudioChunk) -> Iterable[TranscriptionEvent]` for incremental results.
  - `flush_final()` for end-of-input + final transcript; `abort(reason)` for cancellations/errors.
  - Emits `TranscriptionEvent` containing `text`, `is_final`, `latency_ms`, optional `error`.
- **AudioChunk**: bytes + metadata `{ sample_rate, start_ms, end_ms, seq }` to help order/latency tracking.
- **VoiceCaptureSession**: orchestrates push-to-talk lifecycle:
  - `request_permission()` (no-op if already granted), `begin_capture()`, `push_chunk(bytes)`, `end_capture(reason)` (user stop, silence timeout, error).
  - Emits state events: `idle → awaiting_permission → recording → streaming → finalizing → idle/error`.
  - Hooks for UI: callbacks for `on_state`, `on_partial(text)`, `on_final(text)`, `on_error(display_message)`, `on_warning`.
- **PushToTalkCapture**: shared concrete controller that enforces opt-in for remote APIs, replays buffered chunks on retry, auto-stops on silence/max duration, and exposes a `tick()` hook so hosts can poll for timeouts without their own timers.
- **Adapters**:
  - Web: wraps `MediaRecorder`/`getUserMedia`; uses `silence_auto_stop_ms` with client-side silence detection and backpressure-aware chunk emission; fetches `/api/voice/config` for opt-in/provider messaging and sends buffered blobs to `/api/voice/transcribe` with retry/resend on upload failures.
  - TUI: wraps a thin recorder (e.g., `pyaudio`/`sounddevice` later) with the same chunk + state contract; degraded mode: upload pre-recorded file if live capture unavailable.
  - Terminal tab: reuses the browser adapter to inject transcripts into the Codex TUI websocket; exposes an Alt+V hold-to-talk shortcut next to the mic button, mirrors `/api/voice/config` opt-in copy, and reuses the same retry/error affordances as docs chat.

## Push-to-Talk UX Contract
- Explicit opt-in gate shown before first use when `warn_on_remote_api` is true.
- Clear start/stop affordance:
  - Web: mic button toggles; long-press starts recording; badge shows `recording` / `sending…` / `retry`.
  - TUI: keybinding (e.g., `v`) to hold-to-talk; text prompts for permission/errors.
- Auto behaviors:
  - Auto-stop on silence threshold or `max_ms`.
  - Auto-retry for transient provider/network errors with capped backoff; surface a retry button/prompt.
- Error messaging maps to states: `permission-denied`, `mic-unavailable`, `network-failed`, `provider-error`, `timeout`.

## Privacy, Logging, and Persistence
- Raw audio stays in-memory; no filesystem persistence unless an explicit debug flag is set (future).
- Logs redact audio and headers; only record event timings, chunk counts, and provider choice.
- UI shows a warning before sending audio to remote APIs and surfaces the selected provider and model.

## Provider Selection and Latency/Quality Knobs
- Provider resolver reads `voice.provider`; if missing, voice features stay disabled.
- Latency modes map to chunk sizing + when we flush partials:
  - `realtime`: 300–500 ms chunks, eager partials.
  - `balanced`: 600 ms chunks (default), partials every 1–2 chunks.
  - `quality`: 800–1200 ms chunks, prefer final results to reduce request count.
- OpenAI Whisper provider runs behind `SpeechProvider` and enforces redaction + remote warnings; mock provider available for offline/manual tests.
