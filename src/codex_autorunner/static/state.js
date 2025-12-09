import { api, flash, createPoller } from "./utils.js";
import { publish } from "./bus.js";

let stopStatePoll = null;

export async function loadState({ notify = true } = {}) {
  try {
    const data = await api("/api/state");
    publish("state:update", data);
    return data;
  } catch (err) {
    if (notify) flash(err.message);
    publish("state:error", err);
    throw err;
  }
}

export function startStatePolling(intervalMs = 15000) {
  if (stopStatePoll) return stopStatePoll;
  stopStatePoll = createPoller(() => loadState({ notify: false }), intervalMs, {
    immediate: false,
  });
  return stopStatePoll;
}

async function runAction(path, body, successMessage) {
  await api(path, { method: "POST", body });
  if (successMessage) flash(successMessage);
  await loadState({ notify: false });
}

export function startRun(once = false) {
  return runAction("/api/run/start", { once }, once ? "Started one-off run" : "Runner starting");
}

export function stopRun() {
  return runAction("/api/run/stop", null, "Stop signal sent");
}

export function resumeRun() {
  return runAction("/api/run/resume", null, "Resume requested");
}

export function killRun() {
  return runAction("/api/run/kill", null, "Kill signal sent");
}
