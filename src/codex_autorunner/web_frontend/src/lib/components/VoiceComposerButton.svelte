<script lang="ts" module>
  export type VoiceConfigPayload = {
    enabled: boolean;
    has_api_key?: boolean;
    api_key_env?: string;
    chunk_ms?: number;
    provider?: string | null;
    missing_extra?: string;
  };
</script>

<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { pmaApi } from '$lib/api/client';

  let {
    disabled = false,
    onTranscript,
    onError
  }: {
    disabled?: boolean;
    onTranscript?: (text: string) => void;
    onError?: (message: string) => void;
  } = $props();

  let config = $state<VoiceConfigPayload | null>(null);
  let configError = $state<string | null>(null);
  let supported = $state(true);
  let recording = $state(false);
  let sending = $state(false);
  let toggleMode = $state(false);

  let pointerDownAt = 0;
  let pointerIsDown = false;
  let stream: MediaStream | null = null;
  let recorder: MediaRecorder | null = null;
  let chunks: Blob[] = [];
  let pendingClickToggle = false;

  const HOLD_THRESHOLD_MS = 280;

  const ready = $derived(Boolean(config?.enabled && supported && !configError));
  const blockedReason = $derived(blockedReasonFor(config, supported, configError));
  const ariaLabel = $derived(
    recording
      ? 'Stop recording'
      : sending
        ? 'Transcribing'
        : ready
          ? 'Hold or tap to record voice'
          : 'Voice unavailable'
  );

  onMount(() => {
    if (typeof navigator === 'undefined' || !navigator.mediaDevices || typeof window === 'undefined' || !('MediaRecorder' in window)) {
      supported = false;
      return;
    }
    void loadConfig();
  });

  onDestroy(() => {
    cleanupRecorder();
    cleanupStream();
  });

  async function loadConfig(): Promise<void> {
    const result = await pmaApi.voice.getConfig();
    if (result.ok) {
      config = result.data as VoiceConfigPayload;
    } else {
      configError = result.error.message;
    }
  }

  function blockedReasonFor(
    cfg: VoiceConfigPayload | null,
    supportedFlag: boolean,
    err: string | null
  ): string | null {
    if (!supportedFlag) return 'Browser does not support voice capture';
    if (err) return err;
    if (!cfg) return null;
    if (cfg.enabled) return null;
    if (cfg.missing_extra) return cfg.missing_extra;
    if (cfg.has_api_key === false && cfg.api_key_env) {
      return `Voice disabled — set ${cfg.api_key_env} to enable Whisper`;
    }
    return 'Voice disabled — configure a Whisper provider in settings';
  }

  function pickMimeType(): string | null {
    const candidates = [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/ogg;codecs=opus',
      'audio/ogg',
      'audio/mp4',
      'audio/mp4;codecs=mp4a.40.2'
    ];
    for (const mime of candidates) {
      if (MediaRecorder.isTypeSupported(mime)) return mime;
    }
    return null;
  }

  function extensionFor(mime: string): string {
    if (!mime) return 'webm';
    if (mime.includes('ogg')) return 'ogg';
    if (mime.includes('mp4') || mime.includes('m4a')) return 'm4a';
    if (mime.includes('wav')) return 'wav';
    return 'webm';
  }

  async function startRecording(): Promise<void> {
    if (recording || sending || !ready) return;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      onError?.('Microphone permission denied');
      return;
    }
    const mimeType = pickMimeType();
    try {
      recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    } catch {
      cleanupStream();
      onError?.('Microphone unavailable');
      return;
    }
    chunks = [];
    recorder.addEventListener('dataavailable', (event) => {
      const data = (event as BlobEvent).data;
      if (data && data.size > 0) chunks.push(data);
    });
    recorder.addEventListener('stop', () => {
      void onRecorderStop();
    });
    try {
      const chunkMs = typeof config?.chunk_ms === 'number' && config.chunk_ms > 0 ? config.chunk_ms : 600;
      const recorderMime = recorder.mimeType || mimeType || '';
      const shouldChunk = !/mp4|m4a/i.test(recorderMime);
      if (shouldChunk) {
        recorder.start(chunkMs);
      } else {
        recorder.start();
      }
      recording = true;
    } catch {
      cleanupRecorder();
      cleanupStream();
      onError?.('Unable to start recorder');
    }
  }

  function stopRecording(): void {
    if (!recorder || !recording) return;
    recording = false;
    toggleMode = false;
    sending = true;
    try {
      recorder.stop();
    } catch {
      sending = false;
      cleanupRecorder();
      cleanupStream();
      onError?.('Unable to stop recorder');
    }
  }

  async function onRecorderStop(): Promise<void> {
    const recorderMime = (recorder && recorder.mimeType) || 'audio/webm';
    const blob = new Blob(chunks, { type: recorderMime });
    cleanupRecorder();
    cleanupStream();
    if (!blob.size) {
      sending = false;
      onError?.('No audio captured');
      return;
    }
    const filename = `voice.${extensionFor(recorderMime)}`;
    const result = await pmaApi.voice.transcribe(blob, filename);
    sending = false;
    if (!result.ok) {
      onError?.(result.error.message || 'Voice transcription failed');
      return;
    }
    const text = typeof result.data.text === 'string' ? result.data.text.trim() : '';
    if (!text) {
      onError?.('No speech detected');
      return;
    }
    onTranscript?.(text);
  }

  function cleanupRecorder(): void {
    recorder = null;
    chunks = [];
  }

  function cleanupStream(): void {
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
    }
  }

  async function handlePointerDown(event: PointerEvent): Promise<void> {
    if (disabled || !ready) return;
    event.preventDefault();
    pointerDownAt = Date.now();
    pointerIsDown = true;
    pendingClickToggle = false;

    if (recording && toggleMode) {
      stopRecording();
      return;
    }
    if (recording || sending) return;
    await startRecording();
  }

  function handlePointerUp(): void {
    if (!pointerIsDown) return;
    pointerIsDown = false;
    const heldFor = Date.now() - pointerDownAt;
    if (heldFor < HOLD_THRESHOLD_MS) {
      // Treat as a tap → toggle mode: keep recording, stop on next click.
      if (recording && !toggleMode) {
        toggleMode = true;
      } else {
        pendingClickToggle = true;
      }
      return;
    }
    if (recording && !toggleMode) {
      stopRecording();
    }
  }

  function handlePointerLeave(): void {
    if (!pointerIsDown) return;
    pointerIsDown = false;
    if (recording && !toggleMode) {
      stopRecording();
    }
  }

  function handlePointerCancel(): void {
    pointerIsDown = false;
    if (recording && !toggleMode) stopRecording();
  }
</script>

<button
  class="icon-button attachment-button mic"
  class:is-recording={recording}
  class:is-sending={sending}
  type="button"
  disabled={disabled || !ready}
  title={blockedReason ?? (recording ? 'Click to stop' : 'Hold to talk · tap to toggle')}
  aria-label={ariaLabel}
  aria-pressed={recording}
  onpointerdown={handlePointerDown}
  onpointerup={handlePointerUp}
  onpointerleave={handlePointerLeave}
  onpointercancel={handlePointerCancel}
  onclick={(event) => event.preventDefault()}
>
  {#if sending}
    <svg class="voice-spinner" viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2" opacity="0.25" />
      <path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
    </svg>
  {:else}
    <svg class="attachment-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" x2="12" y1="19" y2="22" />
    </svg>
    {#if recording}
      <span class="voice-recording-dot" aria-hidden="true"></span>
    {/if}
  {/if}
</button>

<style>
  .icon-button.mic {
    position: relative;
  }

  .icon-button.mic.is-recording {
    color: var(--color-danger);
    background: var(--color-danger-soft);
  }

  .icon-button.mic.is-sending {
    color: var(--color-accent);
  }

  .voice-recording-dot {
    position: absolute;
    top: 4px;
    right: 4px;
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: var(--color-danger);
    box-shadow: 0 0 0 2px var(--color-surface);
    animation: voice-pulse 1.1s ease-in-out infinite;
  }

  .voice-spinner {
    width: 16px;
    height: 16px;
    animation: voice-spin 0.9s linear infinite;
  }

  @keyframes voice-pulse {
    0%, 100% { opacity: 0.55; transform: scale(1); }
    50% { opacity: 1; transform: scale(1.15); }
  }

  @keyframes voice-spin {
    to { transform: rotate(360deg); }
  }

  @media (prefers-reduced-motion: reduce) {
    .voice-recording-dot,
    .voice-spinner {
      animation: none;
    }
  }
</style>
