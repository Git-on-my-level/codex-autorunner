import { confirmModal, flash, resolvePath } from "./utils.js";

// SVG mic icon (more polished than emoji)
const MIC_ICON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" x2="12" y1="19" y2="22"></line></svg>`;
const RETRY_ICON = "↻";

// Audio level visualization
const NUM_BARS = 5;

function createLevelMeter() {
  const container = document.createElement("div");
  container.className = "voice-level-meter";
  for (let i = 0; i < NUM_BARS; i++) {
    const bar = document.createElement("div");
    bar.className = "voice-level-bar";
    container.appendChild(bar);
  }
  return container;
}

function updateLevelMeter(meter, level) {
  if (!meter) return;
  const bars = meter.querySelectorAll(".voice-level-bar");
  // level is 0-1, map to bar heights
  bars.forEach((bar, i) => {
    // Each bar has a threshold; add some randomness for natural look
    const threshold = (i + 1) / NUM_BARS;
    const variance = Math.random() * 0.15;
    const active = level + variance >= threshold * 0.7;
    const height = active
      ? Math.min(100, level * 100 + Math.random() * 30)
      : 15;
    bar.style.height = `${height}%`;
    bar.classList.toggle("active", active);
  });
}

function supportsVoice() {
  return !!(navigator.mediaDevices && window.MediaRecorder);
}

async function fetchVoiceConfig() {
  const res = await fetch(resolvePath("/api/voice/config"));
  if (!res.ok) throw new Error("Voice config unavailable");
  return res.json();
}

function pickMimeType() {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/ogg",
  ];
  for (const mime of candidates) {
    if (MediaRecorder.isTypeSupported(mime)) return mime;
  }
  return null;
}

function formatErrorMessage(err, fallback) {
  if (!err) return fallback;
  if (typeof err === "string") return err;
  if (err.detail) return err.detail;
  if (err.message) return err.message;
  return fallback;
}

export async function initVoiceInput({
  button,
  input,
  statusEl,
  onTranscript,
  onError,
}) {
  if (!button) return null;
  button.type = "button";

  if (!supportsVoice()) {
    disableButton(button, statusEl, "Voice capture not supported");
    return null;
  }

  let config;
  try {
    config = await fetchVoiceConfig();
  } catch (err) {
    disableButton(button, statusEl, "Voice unavailable");
    return null;
  }

  if (!config.enabled) {
    // Show more helpful message based on API key status
    const reason =
      config.has_api_key === false
        ? `Voice disabled (${config.api_key_env || "API key"} not set)`
        : "Voice disabled";
    disableButton(button, statusEl, reason);
    return null;
  }

  // #region agent log
  fetch("http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      location: "voice.js:initVoiceInput:config",
      message: "voice config loaded",
      data: {
        enabled: Boolean(config.enabled),
        provider: config.provider || null,
        has_api_key: Boolean(config.has_api_key),
        api_key_env: config.api_key_env || null,
        chunk_ms: config.chunk_ms || null,
        latency_mode: config.latency_mode || null,
        supportsVoice: supportsVoice(),
      },
      timestamp: Date.now(),
      sessionId: "debug-session",
      runId: "pre-fix",
      hypothesisId: "H4",
    }),
  }).catch(() => {});
  // #endregion

  const state = {
    recording: false,
    sending: false,
    pendingBlob: null,
    optInAccepted: true, // No opt-in gating
    chunks: [],
    recorder: null,
    stream: null,
    lastError: "",
    // Click-to-toggle support
    pointerDownTime: 0,
    pointerIsDown: false,
    isClickToggleMode: false,
    pendingClickToggle: false,
    // Audio visualization
    audioContext: null,
    analyser: null,
    levelMeter: null,
    animationFrame: null,
    // Debug instrumentation helpers
    _debugMaxLevel: 0,
    _debugLevelSamples: 0,
  };

  // Threshold for distinguishing click vs hold (ms)
  const CLICK_THRESHOLD_MS = 300;

  // Show whisper integration status
  const statusMsg = config.has_api_key
    ? "Hold to talk"
    : `Hold to talk (${config.api_key_env || "API key"} not configured)`;
  setStatus(statusEl, statusMsg);
  resetButton(button);

  const triggerStart = async ({ forceRetry = false } = {}) => {
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

  const startHandler = async (event) => {
    event.preventDefault();
    state.pointerDownTime = Date.now();
    state.pointerIsDown = true;
    state.pendingClickToggle = false;
    // #region agent log
    fetch("http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        location: "voice.js:startHandler",
        message: "pointerdown",
        data: {
          recording: Boolean(state.recording),
          sending: Boolean(state.sending),
          hasPending: Boolean(state.pendingBlob),
          isClickToggleMode: Boolean(state.isClickToggleMode),
          shiftKey: Boolean(event && event.shiftKey),
        },
        timestamp: Date.now(),
        sessionId: "debug-session",
        runId: "pre-fix",
        hypothesisId: "H2",
      }),
    }).catch(() => {});
    // #endregion

    // If already recording in click-toggle mode, stop on next click
    if (state.recording && state.isClickToggleMode) {
      // #region agent log
      fetch(
        "http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            location: "voice.js:startHandler:toggleStop",
            message: "click-toggle stopping",
            data: {
              recorder_state: (state.recorder && state.recorder.state) || null,
              chunks: state.chunks.length,
            },
            timestamp: Date.now(),
            sessionId: "debug-session",
            runId: "pre-fix",
            hypothesisId: "H2",
          }),
        }
      ).catch(() => {});
      // #endregion
      stopRecording();
      state.isClickToggleMode = false;
      return;
    }

    await triggerStart({ forceRetry: Boolean(event.shiftKey) });
  };

  const endHandler = (event) => {
    const holdDuration = Date.now() - state.pointerDownTime;
    state.pointerIsDown = false;
    // #region agent log
    fetch("http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        location: "voice.js:endHandler",
        message: "pointerup",
        data: {
          holdDurationMs: holdDuration,
          recording: Boolean(state.recording),
          isClickToggleMode: Boolean(state.isClickToggleMode),
        },
        timestamp: Date.now(),
        sessionId: "debug-session",
        runId: "pre-fix",
        hypothesisId: "H2",
      }),
    }).catch(() => {});
    // #endregion

    // If it was a quick click (< threshold), switch to click-toggle mode
    if (holdDuration < CLICK_THRESHOLD_MS && state.recording) {
      state.isClickToggleMode = true;
      // Don't stop recording - user will click again to stop
      return;
    }
    // If recording hasn't started yet (e.g., waiting on getUserMedia),
    // remember that this was a click-to-toggle gesture.
    if (holdDuration < CLICK_THRESHOLD_MS && !state.recording) {
      state.pendingClickToggle = true;
      return;
    }

    // Normal hold-to-talk: stop recording on release
    if (state.recording && !state.isClickToggleMode) {
      stopRecording();
    }
  };

  button.addEventListener("pointerdown", startHandler);
  button.addEventListener("pointerup", endHandler);
  button.addEventListener("pointerleave", (e) => {
    // Only stop on leave if in hold mode (not click-toggle)
    if (state.recording && !state.isClickToggleMode) {
      stopRecording();
    }
  });
  button.addEventListener("pointercancel", (e) => {
    if (state.recording && !state.isClickToggleMode) {
      stopRecording();
    }
  });
  button.addEventListener("click", (e) => e.preventDefault());

  async function startRecording() {
    let stream;
    // #region agent log
    fetch("http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        location: "voice.js:startRecording:getUserMedia",
        message: "requesting microphone",
        data: { constraints: { audio: true } },
        timestamp: Date.now(),
        sessionId: "debug-session",
        runId: "pre-fix",
        hypothesisId: "H1",
      }),
    }).catch(() => {});
    // #endregion
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      // #region agent log
      fetch(
        "http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            location: "voice.js:startRecording:getUserMedia:err",
            message: "microphone request failed",
            data: {
              name: err && err.name ? String(err.name) : null,
              message: err && err.message ? String(err.message) : null,
            },
            timestamp: Date.now(),
            sessionId: "debug-session",
            runId: "pre-fix",
            hypothesisId: "H1",
          }),
        }
      ).catch(() => {});
      // #endregion
      state.lastError = "Microphone permission denied";
      setStatus(statusEl, state.lastError);
      setButtonError(button, state.pendingBlob);
      if (onError) onError(state.lastError);
      return;
    }
    state.stream = stream;
    // #region agent log
    fetch("http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        location: "voice.js:startRecording:getUserMedia:ok",
        message: "microphone stream acquired",
        data: {
          tracks:
            stream && stream.getAudioTracks
              ? stream.getAudioTracks().length
              : null,
          track_ready:
            stream && stream.getAudioTracks && stream.getAudioTracks()[0]
              ? stream.getAudioTracks()[0].readyState
              : null,
          settings: (() => {
            try {
              const t = stream.getAudioTracks && stream.getAudioTracks()[0];
              if (!t || !t.getSettings) return null;
              const s = t.getSettings();
              const safe = {};
              for (const k of [
                "sampleRate",
                "channelCount",
                "echoCancellation",
                "noiseSuppression",
                "autoGainControl",
                "sampleSize",
              ])
                if (k in s) safe[k] = s[k];
              return safe;
            } catch (e) {
              return null;
            }
          })(),
        },
        timestamp: Date.now(),
        sessionId: "debug-session",
        runId: "pre-fix",
        hypothesisId: "H1",
      }),
    }).catch(() => {});
    // #endregion
    try {
      const t = stream.getAudioTracks && stream.getAudioTracks()[0];
      if (t) {
        // #region agent log
        fetch(
          "http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              location: "voice.js:startRecording:track",
              message: "audio track state",
              data: {
                enabled: Boolean(t.enabled),
                muted: Boolean(t.muted),
                readyState: t.readyState || null,
              },
              timestamp: Date.now(),
              sessionId: "debug-session",
              runId: "pre-fix",
              hypothesisId: "H1",
            }),
          }
        ).catch(() => {});
        // #endregion
      }
    } catch (e) {}
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

    // #region agent log
    fetch("http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        location: "voice.js:startRecording:MediaRecorder",
        message: "media recorder created",
        data: {
          picked_mime: mimeType,
          recorder_mime: (state.recorder && state.recorder.mimeType) || null,
          recorder_state: (state.recorder && state.recorder.state) || null,
        },
        timestamp: Date.now(),
        sessionId: "debug-session",
        runId: "pre-fix",
        hypothesisId: "H2",
      }),
    }).catch(() => {});
    // #endregion

    state.chunks = [];
    state.recorder.addEventListener("dataavailable", (e) => {
      if (e.data && e.data.size > 0) {
        state.chunks.push(e.data);
        if (state.chunks.length === 1) {
          // #region agent log
          fetch(
            "http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad",
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                location: "voice.js:dataavailable:first",
                message: "first audio chunk received",
                data: {
                  chunk_size: e.data.size,
                  chunk_type: e.data.type || null,
                  recorder_state:
                    (state.recorder && state.recorder.state) || null,
                },
                timestamp: Date.now(),
                sessionId: "debug-session",
                runId: "pre-fix",
                hypothesisId: "H2",
              }),
            }
          ).catch(() => {});
          // #endregion
        }
      }
    });
    state.recorder.addEventListener("stop", onRecorderStop);
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

    // Set up audio level visualization
    try {
      state._debugMaxLevel = 0;
      state._debugLevelSamples = 0;
      state.audioContext = new (window.AudioContext ||
        window.webkitAudioContext)();
      const source = state.audioContext.createMediaStreamSource(stream);
      state.analyser = state.audioContext.createAnalyser();
      state.analyser.fftSize = 256;
      state.analyser.smoothingTimeConstant = 0.5;
      source.connect(state.analyser);

      // Create and show level meter
      state.levelMeter = createLevelMeter();
      button.parentElement.insertBefore(state.levelMeter, button.nextSibling);

      // Start animation loop
      const dataArray = new Uint8Array(state.analyser.frequencyBinCount);
      const animateLevel = () => {
        if (!state.recording) return;
        state.analyser.getByteFrequencyData(dataArray);
        // Calculate average level (0-255) and normalize to 0-1
        const avg = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
        const level = Math.min(1, avg / 128);
        if (level > state._debugMaxLevel) state._debugMaxLevel = level;
        updateLevelMeter(state.levelMeter, level);
        if (state._debugLevelSamples < 3) {
          state._debugLevelSamples += 1;
          // #region agent log
          fetch(
            "http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad",
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                location: "voice.js:visualization:level",
                message: "analyser level sample",
                data: { avg, level, maxLevel: state._debugMaxLevel },
                timestamp: Date.now(),
                sessionId: "debug-session",
                runId: "pre-fix",
                hypothesisId: "H3",
              }),
            }
          ).catch(() => {});
          // #endregion
        }
        state.animationFrame = requestAnimationFrame(animateLevel);
      };
      animateLevel();
    } catch (err) {
      // #region agent log
      fetch(
        "http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            location: "voice.js:startRecording:visualization:err",
            message: "audio visualization setup failed",
            data: {
              name: err && err.name ? String(err.name) : null,
              message: err && err.message ? String(err.message) : null,
            },
            timestamp: Date.now(),
            sessionId: "debug-session",
            runId: "pre-fix",
            hypothesisId: "H3",
          }),
        }
      ).catch(() => {});
      // #endregion
      // Continue without visualization - not critical
    }

    try {
      state.recorder.start(config.chunk_ms || 600);
    } catch (err) {
      state.recording = false;
      state.lastError = "Unable to start recorder";
      setStatus(statusEl, state.lastError);
      setButtonError(button, state.pendingBlob);
      if (onError) onError(state.lastError);
    }
  }

  function stopRecording() {
    if (!state.recorder) return;
    state.recording = false;
    state.isClickToggleMode = false; // Reset click-toggle mode
    state.sending = true;
    setStatus(statusEl, "Transcribing…");
    setButtonSending(button);
    // #region agent log
    fetch("http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        location: "voice.js:stopRecording",
        message: "stop requested",
        data: { maxLevel: state._debugMaxLevel, chunks: state.chunks.length },
        timestamp: Date.now(),
        sessionId: "debug-session",
        runId: "pre-fix",
        hypothesisId: "H2",
      }),
    }).catch(() => {});
    // #endregion
    try {
      state.recorder.stop();
    } catch (err) {
      state.sending = false;
      state.lastError = "Unable to stop recorder";
      setButtonError(button, state.pendingBlob);
      if (onError) onError(state.lastError);
    }
  }

  async function onRecorderStop() {
    const blob = new Blob(state.chunks, {
      type: (state.recorder && state.recorder.mimeType) || "audio/webm",
    });
    // #region agent log
    fetch("http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        location: "voice.js:onRecorderStop:blob",
        message: "recorder stopped; built blob",
        data: {
          chunks: state.chunks.length,
          blob_size: blob.size,
          blob_type: blob.type || null,
        },
        timestamp: Date.now(),
        sessionId: "debug-session",
        runId: "pre-fix",
        hypothesisId: "H2",
      }),
    }).catch(() => {});
    // #endregion
    cleanupRecorder(state);
    if (!blob.size) {
      state.sending = false;
      state.lastError = "No audio captured";
      setStatus(statusEl, state.lastError);
      setButtonError(button, state.pendingBlob);
      if (onError) onError(state.lastError);
      return;
    }
    await sendForTranscription(blob);
  }

  async function retryTranscription() {
    if (!state.pendingBlob) return;
    setStatus(statusEl, "Retrying…");
    setButtonSending(button);
    await sendForTranscription(state.pendingBlob, { retry: true });
  }

  async function sendForTranscription(blob, { retry = false } = {}) {
    state.sending = true;
    state.pendingBlob = blob;
    try {
      const text = await transcribeBlob(blob, state.optInAccepted);
      state.sending = false;
      state.pendingBlob = null;
      setStatus(statusEl, text ? "Transcript ready" : "No speech detected");
      resetButton(button);
      if (text && onTranscript) onTranscript(text);
      if (!text) flash("No speech detected in recording", "error");
    } catch (err) {
      state.sending = false;
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
      cleanupStream(state);
    }
  }

  async function transcribeBlob(blob, optIn) {
    const formData = new FormData();
    formData.append("file", blob, "voice.webm");
    formData.append("opt_in", optIn ? "1" : "0");
    const url = resolvePath("/api/voice/transcribe");
    // #region agent log
    fetch("http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        location: "voice.js:transcribeBlob:fetch",
        message: "sending audio for transcription",
        data: {
          url,
          blob_size: blob.size,
          blob_type: blob.type || null,
          opt_in: Boolean(optIn),
        },
        timestamp: Date.now(),
        sessionId: "debug-session",
        runId: "pre-fix",
        hypothesisId: "H4",
      }),
    }).catch(() => {});
    // #endregion
    const res = await fetch(url, {
      method: "POST",
      body: formData,
    });
    let payload = {};
    try {
      payload = await res.json();
    } catch (err) {
      // Ignore JSON errors; will fall back to generic message
    }
    if (!res.ok) {
      const detail =
        payload.detail ||
        payload.error ||
        (typeof payload === "string" ? payload : "") ||
        `Voice failed (${res.status})`;
      throw new Error(detail);
    }
    // #region agent log
    fetch("http://127.0.0.1:7242/ingest/0edefa02-53f9-4997-b974-f16ebabdecad", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        location: "voice.js:transcribeBlob:response",
        message: "transcribe response received",
        data: {
          status: res.status,
          ok: Boolean(res.ok),
          keys:
            payload && typeof payload === "object"
              ? Object.keys(payload).slice(0, 10)
              : null,
          text_len: payload && payload.text ? String(payload.text).length : 0,
        },
        timestamp: Date.now(),
        sessionId: "debug-session",
        runId: "pre-fix",
        hypothesisId: "H4",
      }),
    }).catch(() => {});
    // #endregion
    return payload.text || "";
  }

  return {
    config,
    start: () => triggerStart(),
    stop: () => endHandler(),
    isRecording: () => state.recording,
    hasPending: () => Boolean(state.pendingBlob),
  };
}

function cleanupRecorder(state) {
  if (state.recorder) {
    state.recorder.onstop = null;
    state.recorder.ondataavailable = null;
  }
  state.recorder = null;

  // Clean up audio visualization
  if (state.animationFrame) {
    cancelAnimationFrame(state.animationFrame);
    state.animationFrame = null;
  }
  if (state.levelMeter && state.levelMeter.parentElement) {
    state.levelMeter.parentElement.removeChild(state.levelMeter);
  }
  state.levelMeter = null;
  if (state.audioContext) {
    state.audioContext.close().catch(() => {});
    state.audioContext = null;
  }
  state.analyser = null;
}

function cleanupStream(state) {
  if (state.stream) {
    state.stream.getTracks().forEach((track) => track.stop());
  }
  state.stream = null;
}

function setStatus(el, text) {
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}

function resetButton(button) {
  button.disabled = false;
  button.classList.remove(
    "voice-recording",
    "voice-sending",
    "voice-error",
    "voice-retry"
  );
  button.innerHTML = MIC_ICON_SVG;
}

function setButtonRecording(button) {
  button.classList.add("voice-recording");
  button.classList.remove("voice-sending", "voice-error");
  button.innerHTML = MIC_ICON_SVG;
}

function setButtonSending(button) {
  button.classList.add("voice-sending");
  button.classList.remove("voice-recording", "voice-error");
  button.innerHTML = MIC_ICON_SVG;
}

function setButtonError(button, hasPending) {
  button.classList.remove("voice-recording", "voice-sending");
  button.classList.add("voice-error");
  if (hasPending) {
    button.classList.add("voice-retry");
    button.textContent = RETRY_ICON;
  } else {
    button.innerHTML = MIC_ICON_SVG;
  }
}

function disableButton(button, statusEl, reason) {
  button.disabled = true;
  button.classList.add("disabled");
  button.innerHTML = MIC_ICON_SVG;
  button.title = reason;
  setStatus(statusEl, reason);
}
