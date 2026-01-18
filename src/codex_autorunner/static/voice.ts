import { flash, resolvePath, getAuthToken } from "./utils.js";

const MIC_ICON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" x2="12" y1="19" y2="22"></line></svg>`;
const SENDING_ICON_SVG = `<svg class="voice-spinner" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9" opacity="0.25"></circle><path d="M21 12a9 9 0 0 0-9-9"></path></svg>`;
const RETRY_ICON = "↻";

const NUM_BARS = 5;

function createLevelMeter(): HTMLDivElement {
  const container = document.createElement("div");
  container.className = "voice-level-meter";
  for (let i = 0; i < NUM_BARS; i++) {
    const bar = document.createElement("div");
    bar.className = "voice-level-bar";
    container.appendChild(bar);
  }
  return container;
}

function updateLevelMeter(meter: HTMLDivElement | null, level: number): void {
  if (!meter) return;
  const bars = meter.querySelectorAll(".voice-level-bar");
  bars.forEach((bar, i) => {
    const threshold = (i + 1) / NUM_BARS;
    const variance = Math.random() * 0.15;
    const active = level + variance >= threshold * 0.7;
    const height = active
      ? Math.min(100, level * 100 + Math.random() * 30)
      : 15;
    (bar as HTMLElement).style.height = `${height}%`;
    bar.classList.toggle("active", active);
  });
}

function supportsVoice(): boolean {
  return !!(navigator.mediaDevices && window.MediaRecorder);
}

interface VoiceConfig {
  enabled: boolean;
  has_api_key: boolean;
  api_key_env?: string;
  chunk_ms?: number;
}

async function fetchVoiceConfig(): Promise<VoiceConfig> {
  const headers: Record<string, string> = {};
  const token = getAuthToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const res = await fetch(resolvePath("/api/voice/config"), { headers });
  if (!res.ok) throw new Error("Voice config unavailable");
  return res.json() as Promise<VoiceConfig>;
}

function pickMimeType(): string | null {
  const candidates: string[] = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/ogg",
    "audio/mp4",
    "audio/mp4;codecs=mp4a.40.2",
  ];
  for (const mime of candidates) {
    if (MediaRecorder.isTypeSupported(mime)) return mime;
  }
  return null;
}

interface ErrorLike {
  detail?: string;
  message?: string;
}

function formatErrorMessage(err: unknown, fallback: string): string {
  if (!err) return fallback;
  if (typeof err === "string") return err;
  if (typeof err === "object" && err !== null) {
    if ("detail" in err) return (err as ErrorLike).detail || fallback;
    if ("message" in err) return (err as ErrorLike).message || fallback;
  }
  return fallback;
}

interface VoiceInputOptions {
  button: HTMLButtonElement;
  input?: HTMLInputElement | HTMLTextAreaElement;
  statusEl?: HTMLElement;
  onTranscript?: (text: string) => void;
  onError?: (error: string) => void;
}

interface VoiceInputAPI {
  config: VoiceConfig;
  start: () => Promise<void>;
  stop: () => void;
  isRecording: () => boolean;
  hasPending: () => boolean;
}

interface VoiceState {
  recording: boolean;
  sending: boolean;
  pendingBlob: Blob | null;
  chunks: Blob[];
  recorder: MediaRecorder | null;
  recorderDataHandler: ((e: Event) => void) | null;
  recorderStopHandler: (() => void) | null;
  stream: MediaStream | null;
  lastError: string;
  pointerDownTime: number;
  pointerIsDown: boolean;
  isClickToggleMode: boolean;
  pendingClickToggle: boolean;
  audioContext: AudioContext | null;
  analyser: AnalyserNode | null;
  levelMeter: HTMLDivElement | null;
  levelMeterStopHandler: ((e: Event) => void) | null;
  animationFrame: number | null;
  stopTimeout: ReturnType<typeof setTimeout> | null;
  stopTimedOut: boolean;
}

export async function initVoiceInput({
  button,
  input: _input,
  statusEl,
  onTranscript,
  onError,
}: VoiceInputOptions): Promise<VoiceInputAPI | null> {
  if (!button) return null;
  button.type = "button";
  const replaceWithWaveform = button.dataset?.voiceMode === "waveform";

  if (!supportsVoice()) {
    disableButton(button, statusEl, "Voice capture not supported");
    return null;
  }

  let config: VoiceConfig;
  try {
    config = await fetchVoiceConfig();
  } catch (err) {
    disableButton(button, statusEl, "Voice unavailable");
    return null;
  }

  if (!config.enabled) {
    const reason =
      config.has_api_key === false
        ? `Voice disabled (${config.api_key_env || "API key"} not set)`
        : "Voice disabled";
    disableButton(button, statusEl, reason);
    return null;
  }

  const state: VoiceState = {
    recording: false,
    sending: false,
    pendingBlob: null,
    chunks: [],
    recorder: null,
    recorderDataHandler: null,
    recorderStopHandler: null,
    stream: null,
    lastError: "",
    pointerDownTime: 0,
    pointerIsDown: false,
    isClickToggleMode: false,
    pendingClickToggle: false,
    audioContext: null,
    analyser: null,
    levelMeter: null,
    levelMeterStopHandler: null,
    animationFrame: null,
    stopTimeout: null,
    stopTimedOut: false,
  };

  const CLICK_THRESHOLD_MS = 300;

  const statusMsg = config.has_api_key
    ? "Hold to talk"
    : `Hold to talk (${config.api_key_env || "API key"} not configured)`;
  setStatus(statusEl, statusMsg);
  resetButton(button);

  const triggerStart = async ({ forceRetry = false } = {}): Promise<void> => {
    if (state.recording || state.sending) {
      return;
    }
    if (state.pendingBlob && !forceRetry) {
      await retryTranscription();
      return;
    }
    state.pendingBlob = null;
    state.lastError = "";
    await startRecording();
  };

  const startHandler = async (event: Event): Promise<void> => {
    event.preventDefault();
    state.pointerDownTime = Date.now();
    state.pointerIsDown = true;
    state.pendingClickToggle = false;

    if (state.recording && state.isClickToggleMode) {
      stopRecording();
      state.isClickToggleMode = false;
      return;
    }

    await triggerStart({ forceRetry: (event as MouseEvent).shiftKey });
  };

  const endHandler = (): void => {
    const holdDuration = Date.now() - state.pointerDownTime;
    state.pointerIsDown = false;

    if (holdDuration < CLICK_THRESHOLD_MS && !state.recording) {
      state.pendingClickToggle = true;
      return;
    }

    if (state.recording && !state.isClickToggleMode) {
      stopRecording();
    }
  };

  button.addEventListener("pointerdown", startHandler);
  button.addEventListener("pointerup", endHandler);
  button.addEventListener("pointerleave", () => {
    if (state.recording && !state.isClickToggleMode) {
      stopRecording();
    }
  });
  button.addEventListener("pointercancel", () => {
    if (state.recording && !state.isClickToggleMode) {
      stopRecording();
    }
  });
  button.addEventListener("click", (e) => e.preventDefault());

  async function startRecording(): Promise<void> {
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      state.lastError = "Microphone permission denied";
      setStatus(statusEl, state.lastError);
      setButtonError(button, state.pendingBlob);
      if (onError) onError(state.lastError);
      return;
    }
    state.stream = stream;
    const mimeType = pickMimeType();
    try {
      state.recorder = new MediaRecorder(
        stream,
        mimeType ? { mimeType } : undefined
      );
    } catch (err) {
      state.lastError = "Microphone unavailable";
      cleanupStream(state);
      setStatus(statusEl, state.lastError);
      setButtonError(button, state.pendingBlob);
      if (onError) onError(state.lastError);
      return;
    }

    state.chunks = [];
    state.stopTimedOut = false;
    if (state.stopTimeout) {
      clearTimeout(state.stopTimeout);
      state.stopTimeout = null;
    }
    state.recorderDataHandler = (e: Event) => {
      const dataEvent = e as BlobEvent;
      if (dataEvent.data && dataEvent.data.size > 0) {
        state.chunks.push(dataEvent.data);
      }
    };
    state.recorderStopHandler = onRecorderStop;
    state.recorder.addEventListener("dataavailable", state.recorderDataHandler);
    state.recorder.addEventListener("stop", state.recorderStopHandler);
    state.recording = true;
    if (state.pendingClickToggle && !state.pointerIsDown) {
      state.isClickToggleMode = true;
      state.pendingClickToggle = false;
    }
    setStatus(
      statusEl,
      state.isClickToggleMode
        ? "Listening… click to stop"
        : "Listening… click or release to stop"
    );
    setButtonRecording(button);

    try {
      const AudioContextCtor =
        window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      state.audioContext = new AudioContextCtor();
      const source = state.audioContext.createMediaStreamSource(stream);
      state.analyser = state.audioContext.createAnalyser();
      state.analyser.fftSize = 256;
      state.analyser.smoothingTimeConstant = 0.5;
      source.connect(state.analyser);

      state.levelMeter = createLevelMeter();
      button.parentElement!.insertBefore(state.levelMeter, button.nextSibling);
      if (replaceWithWaveform) {
        button.classList.add("hidden");
        state.levelMeter.classList.add("voice-level-stop");
        state.levelMeterStopHandler = (e: Event) => {
          e.preventDefault();
          stopRecording();
        };
        state.levelMeter.addEventListener("click", state.levelMeterStopHandler);
      }

      const dataArray = new Uint8Array(state.analyser.frequencyBinCount);
      const animateLevel = () => {
        if (!state.recording) return;
        state.analyser.getByteFrequencyData(dataArray);
        const avg = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
        const level = Math.min(1, avg / 128);
        updateLevelMeter(state.levelMeter, level);
        state.animationFrame = requestAnimationFrame(animateLevel);
      };
      animateLevel();
    } catch (err) {
      // Continue without visualization - not critical
    }

    try {
      const chunkMs =
        typeof config.chunk_ms === "number" && config.chunk_ms > 0
          ? config.chunk_ms
          : 600;
      const mime = (state.recorder && state.recorder.mimeType) || mimeType || "";
      const shouldChunk = !/mp4|m4a/i.test(mime);
      if (shouldChunk) {
        state.recorder!.start(chunkMs);
      } else {
        state.recorder!.start();
      }
    } catch (err) {
      state.recording = false;
      state.lastError = "Unable to start recorder";
      setStatus(statusEl, state.lastError);
      setButtonError(button, state.pendingBlob);
      cleanupStream(state);
      if (onError) onError(state.lastError);
    }
  }

  function stopRecording(): void {
    if (!state.recorder) return;
    state.recording = false;
    state.isClickToggleMode = false;
    state.sending = true;
    setStatus(statusEl, "Transcribing…");
    setButtonSending(button);
    try {
      if (state.stopTimeout) {
        clearTimeout(state.stopTimeout);
      }
      state.stopTimeout = setTimeout(() => {
        if (!state.sending) return;
        state.stopTimedOut = true;
        state.sending = false;
        state.lastError = "Recording timed out";
        setStatus(statusEl, state.lastError);
        setButtonError(button, state.pendingBlob);
        cleanupRecorder(state);
        cleanupStream(state);
        if (onError) onError(state.lastError);
      }, 4000);
      state.recorder!.stop();
    } catch (err) {
      state.sending = false;
      state.lastError = "Unable to stop recorder";
      setButtonError(button, state.pendingBlob);
      cleanupStream(state);
      if (onError) onError(state.lastError);
    }
  }

  async function onRecorderStop(): Promise<void> {
    try {
      if (state.stopTimeout) {
        clearTimeout(state.stopTimeout);
        state.stopTimeout = null;
      }
      if (state.stopTimedOut) {
        state.stopTimedOut = false;
        return;
      }
      const blob = new Blob(state.chunks, {
        type: (state.recorder && state.recorder.mimeType) || "audio/webm",
      });
      cleanupRecorder(state);
      if (!blob.size) {
        state.sending = false;
        state.lastError = "No audio captured";
        setStatus(statusEl, state.lastError);
        setButtonError(button, state.pendingBlob);
        if (onError) onError(state.lastError);
        cleanupStream(state);
        return;
      }
      await sendForTranscription(blob);
    } catch (err) {
      state.sending = false;
      state.lastError = formatErrorMessage(err, "Voice transcription failed");
      setStatus(statusEl, state.lastError);
      setButtonError(button, state.pendingBlob);
      if (onError) onError(state.lastError);
      cleanupStream(state);
    }
  }

  async function retryTranscription(): Promise<void> {
    if (!state.pendingBlob) return;
    setStatus(statusEl, "Retrying…");
    setButtonSending(button);
    await sendForTranscription(state.pendingBlob, { retry: true });
  }

  async function sendForTranscription(blob: Blob, { retry = false } = {}): Promise<void> {
    state.sending = true;
    state.pendingBlob = blob;
    try {
      const text = await transcribeBlob(blob);
      state.pendingBlob = null;
      setStatus(statusEl, text ? "Transcript ready" : "No speech detected");
      resetButton(button);
      if (text && onTranscript) onTranscript(text);
      if (!text) flash("No speech detected in recording", "error");
    } catch (err) {
      state.lastError = formatErrorMessage(err, "Voice transcription failed");
      setStatus(statusEl, state.lastError);
      setButtonError(button, state.pendingBlob);
      flash(
        retry
          ? "Voice retry failed; try again."
          : "Voice upload failed, tap to retry or Shift+tap to re-record.",
        "error"
      );
      if (onError) onError(state.lastError);
    } finally {
      state.sending = false;
      cleanupStream(state);
    }
  }

  async function transcribeBlob(blob: Blob): Promise<string> {
    const formData = new FormData();
    const ext = getExtensionForMime(blob.type);
    formData.append("file", blob, `voice.${ext}`);
    const url = resolvePath("/api/voice/transcribe");
    const headers: Record<string, string> = {};
    const token = getAuthToken();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
    const res = await fetch(url, {
      method: "POST",
      body: formData,
      headers,
    });
    let payload: unknown = {};
    try {
      payload = await res.json();
    } catch (err) {
      // Ignore JSON errors; will fall back to generic message
    }
    if (!res.ok) {
      const detail =
        (payload as { detail?: string }).detail ||
        (payload as { error?: string }).error ||
        (typeof payload === "string" ? payload : "") ||
        `Voice failed (${res.status})`;
      throw new Error(detail);
    }
    return (payload as { text?: string }).text || "";
  }

  function cleanupRecorder(state: VoiceState): void {
    if (state.recorder) {
      if (state.recorderStopHandler) {
        if (typeof state.recorder.removeEventListener === "function") {
          state.recorder.removeEventListener("stop", state.recorderStopHandler);
        }
        state.recorderStopHandler = null;
      }
      if (state.recorderDataHandler) {
        if (typeof state.recorder.removeEventListener === "function") {
          state.recorder.removeEventListener(
            "dataavailable",
            state.recorderDataHandler
          );
        }
        state.recorderDataHandler = null;
      }
    }
    state.recorder = null;
    if (state.stopTimeout) {
      clearTimeout(state.stopTimeout);
      state.stopTimeout = null;
    }

    if (state.animationFrame) {
      cancelAnimationFrame(state.animationFrame);
      state.animationFrame = null;
    }
    if (state.levelMeter) {
      if (state.levelMeterStopHandler) {
        state.levelMeter.removeEventListener(
          "click",
          state.levelMeterStopHandler
        );
        state.levelMeterStopHandler = null;
      }
      if (state.levelMeter.parentElement) {
        state.levelMeter.parentElement.removeChild(state.levelMeter);
      }
    }
    state.levelMeter = null;
    if (replaceWithWaveform) {
      button.classList.remove("hidden");
    }
    if (state.audioContext) {
      state.audioContext.close().catch(() => {});
      state.audioContext = null;
    }
    state.analyser = null;
  }

  function getExtensionForMime(mime: string): string {
    if (!mime) return "webm";
    if (mime.includes("ogg")) return "ogg";
    if (mime.includes("mp4") || mime.includes("m4a")) return "m4a";
    if (mime.includes("wav")) return "wav";
    return "webm";
  }

  return {
    config,
    start: () => triggerStart(),
    stop: () => endHandler(),
    isRecording: () => state.recording,
    hasPending: () => Boolean(state.pendingBlob),
  };
}

function cleanupStream(state: VoiceState): void {
  if (state.stream) {
    state.stream.getTracks().forEach((track) => track.stop());
  }
  state.stream = null;
}

function setStatus(el: HTMLElement | undefined, text: string): void {
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}

function safeRemoveAttribute(el: HTMLElement, name: string): void {
  if (typeof el.removeAttribute !== "function") return;
  el.removeAttribute(name);
}

function safeSetAttribute(el: HTMLElement, name: string, value: string): void {
  if (typeof el.setAttribute !== "function") return;
  el.setAttribute(name, value);
}

function resetButton(button: HTMLButtonElement): void {
  button.disabled = false;
  safeRemoveAttribute(button, "aria-busy");
  button.classList.remove(
    "voice-recording",
    "voice-sending",
    "voice-error",
    "voice-retry"
  );
  button.innerHTML = MIC_ICON_SVG;
}

function setButtonRecording(button: HTMLButtonElement): void {
  safeRemoveAttribute(button, "aria-busy");
  button.classList.add("voice-recording");
  button.classList.remove("voice-sending", "voice-error");
  button.innerHTML = MIC_ICON_SVG;
}

function setButtonSending(button: HTMLButtonElement): void {
  safeSetAttribute(button, "aria-busy", "true");
  button.classList.add("voice-sending");
  button.classList.remove("voice-recording", "voice-error");
  button.innerHTML = SENDING_ICON_SVG;
}

function setButtonError(button: HTMLButtonElement, hasPending: Blob | null): void {
  safeRemoveAttribute(button, "aria-busy");
  button.classList.remove("voice-recording", "voice-sending");
  button.classList.add("voice-error");
  if (hasPending) {
    button.classList.add("voice-retry");
    button.textContent = RETRY_ICON;
  } else {
    button.innerHTML = MIC_ICON_SVG;
  }
}

function disableButton(button: HTMLButtonElement, statusEl: HTMLElement | undefined, reason: string): void {
  button.disabled = true;
  button.classList.add("disabled");
  button.innerHTML = MIC_ICON_SVG;
  button.title = reason;
  setStatus(statusEl, reason);
}
