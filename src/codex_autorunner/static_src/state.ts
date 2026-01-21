import { api, confirmModal, flash, streamEvents } from "./utils.js";
import { publish } from "./bus.js";
import { CONSTANTS } from "./constants.js";

let stopStateStream: (() => void) | null = null;

interface LoadStateOptions {
  notify?: boolean;
}

export async function loadState({ notify = true }: LoadStateOptions = {}): Promise<unknown> {
  try {
    const data = await api(CONSTANTS.API.STATE_ENDPOINT);
    publish("state:update", data);
    return data;
  } catch (err) {
    if (notify) flash((err as Error).message);
    publish("state:error", err);
    throw err;
  }
}

export function startStatePolling(): () => void {
  if (stopStateStream) return stopStateStream;

  let active = true;
  let cancelStream: (() => void) | null = null;

  const connect = () => {
    if (!active) return;
    loadState({ notify: false }).catch(() => {});

    cancelStream = streamEvents("/api/state/stream", {
      onMessage: (data: string, event: string) => {
        if (event && event !== "message") return;
        try {
          const state = JSON.parse(data);
          publish("state:update", state);
        } catch (e) {
          console.error("Bad state payload", e);
        }
      },
      onFinish: () => {
        if (active) {
          setTimeout(connect, 2000);
        }
      },
    }) as (() => void);
  };

  connect();

  stopStateStream = () => {
    active = false;
    if (cancelStream) cancelStream();
    stopStateStream = null;
  };
  return stopStateStream;
}

async function runAction(path: string, body: unknown | null, successMessage: string): Promise<void> {
  await api(path, { method: "POST", body });
  if (successMessage) flash(successMessage);
  await loadState({ notify: false });
}

export function startRun(
  once = false,
  overrides: { agent?: string; model?: string; reasoning?: string } = {}
): Promise<void> {
  const body: { once: boolean; agent?: string; model?: string; reasoning?: string } = { once };
  if (Object.prototype.hasOwnProperty.call(overrides, "agent")) {
    body.agent = overrides.agent;
  }
  if (Object.prototype.hasOwnProperty.call(overrides, "model")) {
    body.model = overrides.model;
  }
  if (Object.prototype.hasOwnProperty.call(overrides, "reasoning")) {
    body.reasoning = overrides.reasoning;
  }
  return runAction(
    "/api/run/start",
    body,
    once ? "Started one-off run" : "Runner starting"
  );
}

export function stopRun(): Promise<void> {
  return runAction("/api/run/stop", null, "Stop signal sent");
}

export function resumeRun(): Promise<void> {
  return runAction("/api/run/resume", null, "Resume requested");
}

export async function killRun(): Promise<void | null> {
  const confirmed = await confirmModal(
    "Kill the runner process? This stops it immediately and may leave partial state.",
    { confirmText: "Kill runner", cancelText: "Cancel", danger: true }
  );
  if (!confirmed) return null;
  return runAction("/api/run/kill", null, "Kill signal sent");
}

export function resetRun(): Promise<void> {
  return runAction("/api/run/reset", null, "Runner reset complete");
}

export async function clearLock(): Promise<void | null> {
  const confirmed = await confirmModal(
    "Clear a stale autorunner lock? This will only succeed if the lock looks safe to remove.",
    { confirmText: "Clear lock", cancelText: "Cancel", danger: true }
  );
  if (!confirmed) return null;
  return runAction("/api/run/clear-lock", null, "Cleared stale lock");
}
