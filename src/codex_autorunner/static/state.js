import { api, confirmModal, flash, streamEvents } from "./utils.js";
import { publish } from "./bus.js";
import { CONSTANTS } from "./constants.js";

let stopStateStream = null;

export async function loadState({ notify = true } = {}) {
  try {
    const data = await api(CONSTANTS.API.STATE_ENDPOINT);
    publish("state:update", data);
    return data;
  } catch (err) {
    if (notify) flash(err.message);
    publish("state:error", err);
    throw err;
  }
}

export function startStatePolling() {
  if (stopStateStream) return stopStateStream;

  let active = true;
  let cancelStream = null;

  const connect = () => {
    if (!active) return;
    // Initial fetch to ensure immediate state
    loadState({ notify: false }).catch(() => {});
    
    cancelStream = streamEvents("/api/state/stream", {
      onMessage: (data) => {
        try {
          const state = JSON.parse(data);
          publish("state:update", state);
        } catch (e) {
          console.error("Bad state payload", e);
        }
      },
      onFinish: () => {
        if (active) {
          // Reconnect after delay
          setTimeout(connect, 2000);
        }
      },
    });
  };

  connect();

  stopStateStream = () => {
    active = false;
    if (cancelStream) cancelStream();
    stopStateStream = null;
  };
  return stopStateStream;
}

async function runAction(path, body, successMessage) {
  await api(path, { method: "POST", body });
  if (successMessage) flash(successMessage);
  await loadState({ notify: false });
}

export function startRun(once = false, overrides = {}) {
  const body = { once };
  if (overrides && Object.prototype.hasOwnProperty.call(overrides, "agent")) {
    body.agent = overrides.agent;
  }
  if (overrides && Object.prototype.hasOwnProperty.call(overrides, "model")) {
    body.model = overrides.model;
  }
  if (overrides && Object.prototype.hasOwnProperty.call(overrides, "reasoning")) {
    body.reasoning = overrides.reasoning;
  }
  return runAction(
    "/api/run/start",
    body,
    once ? "Started one-off run" : "Runner starting"
  );
}

export function stopRun() {
  return runAction("/api/run/stop", null, "Stop signal sent");
}

export function resumeRun() {
  return runAction("/api/run/resume", null, "Resume requested");
}

export async function killRun() {
  const confirmed = await confirmModal(
    "Kill the runner process? This stops it immediately and may leave partial state.",
    { confirmText: "Kill runner", cancelText: "Cancel", danger: true }
  );
  if (!confirmed) return null;
  return runAction("/api/run/kill", null, "Kill signal sent");
}

export function resetRunner() {
  return runAction("/api/run/reset", null, "Runner reset complete");
}
