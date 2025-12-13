import { CONSTANTS } from "./constants.js";
import { BASE_PATH } from "./env.js";

const toast = document.getElementById("toast");
const decoder = new TextDecoder();

export function resolvePath(path) {
  if (!path) return path;
  const absolutePrefixes = ["http://", "https://", "ws://", "wss://"];
  if (absolutePrefixes.some((prefix) => path.startsWith(prefix))) {
    return path;
  }
  if (!BASE_PATH) {
    return path;
  }
  if (path.startsWith(BASE_PATH)) {
    return path;
  }
  if (path.startsWith("/")) {
    return `${BASE_PATH}${path}`;
  }
  return `${BASE_PATH}/${path}`;
}

export function buildWsUrl(path, query = "") {
  const resolved = resolvePath(path);
  const normalized = resolved.startsWith("/") ? resolved : `/${resolved}`;
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${normalized}${query}`;
}

export function flash(message, type = "info") {
  toast.textContent = message;
  toast.classList.remove("error");
  if (type === "error") {
    toast.classList.add("error");
  }
  toast.classList.add("show");
  setTimeout(() => {
    toast.classList.remove("show", "error");
  }, CONSTANTS.UI.TOAST_DURATION);
}

export function statusPill(el, status) {
  const normalized = status || "idle";
  el.textContent = normalized;
  el.classList.remove("pill-idle", "pill-running", "pill-error", "pill-warn");
  const errorStates = ["error", "init_error"];
  const warnStates = ["locked", "missing", "uninitialized", "initializing"];
  if (normalized === "running") {
    el.classList.add("pill-running");
  } else if (errorStates.includes(normalized)) {
    el.classList.add("pill-error");
  } else if (warnStates.includes(normalized)) {
    el.classList.add("pill-warn");
  } else {
    el.classList.add("pill-idle");
  }
}

export async function api(path, options = {}) {
  const headers = options.headers ? { ...options.headers } : {};
  const opts = { ...options, headers };
  const target = resolvePath(path);
  if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(target, opts);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Request failed (${res.status})`);
  }
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return res.json();
  }
  return res.text();
}

export function streamEvents(
  path,
  { method = "GET", body = null, onMessage, onError, onFinish } = {}
) {
  const controller = new AbortController();
  let fetchBody = body;
  const target = resolvePath(path);
  const headers = {};
  if (fetchBody && typeof fetchBody === "object" && !(fetchBody instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    fetchBody = JSON.stringify(fetchBody);
  }
  fetch(target, { method, body: fetchBody, headers, signal: controller.signal })
    .then(async (res) => {
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Request failed (${res.status})`);
      }
      if (!res.body) {
        throw new Error("Streaming not supported in this browser");
      }
      const reader = res.body.getReader();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop();
        for (const chunk of chunks) {
          if (!chunk.trim()) continue;
          const lines = chunk.split("\n");
          let event = "message";
          const dataLines = [];
          for (const line of lines) {
            if (line.startsWith("event:")) {
              event = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              dataLines.push(line.slice(5).trimStart());
            }
          }
          const data = dataLines.join("\n");
          if (onMessage) onMessage(data, event || "message");
        }
      }
      if (!controller.signal.aborted && onFinish) {
        onFinish();
      }
    })
    .catch((err) => {
      if (controller.signal.aborted) {
        if (onFinish) onFinish();
        return;
      }
      if (onError) onError(err);
      if (onFinish) onFinish();
    });

  return () => controller.abort();
}

export function createPoller(fn, intervalMs, { immediate = true } = {}) {
  let timer = null;
  const tick = async () => {
    try {
      await fn();
    } finally {
      timer = setTimeout(tick, intervalMs);
    }
  };
  if (immediate) {
    tick();
  } else {
    timer = setTimeout(tick, intervalMs);
  }
  return () => {
    if (timer) clearTimeout(timer);
  };
}

/**
 * Show a custom confirmation modal dialog.
 * Works consistently across desktop and mobile.
 * @param {string} message - The confirmation message to display
 * @param {Object} [options] - Optional configuration
 * @param {string} [options.confirmText="Confirm"] - Text for the confirm button
 * @param {string} [options.cancelText="Cancel"] - Text for the cancel button
 * @param {boolean} [options.danger=true] - Whether to style confirm as danger
 * @returns {Promise<boolean>} - Resolves to true if confirmed, false if cancelled
 */
export function confirmModal(message, options = {}) {
  const { confirmText = "Confirm", cancelText = "Cancel", danger = true } = options;
  return new Promise((resolve) => {
    const overlay = document.getElementById("confirm-modal");
    const messageEl = document.getElementById("confirm-modal-message");
    const okBtn = document.getElementById("confirm-modal-ok");
    const cancelBtn = document.getElementById("confirm-modal-cancel");

    messageEl.textContent = message;
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;
    okBtn.className = danger ? "danger" : "primary";
    overlay.hidden = false;

    const cleanup = () => {
      overlay.hidden = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onOverlayClick);
      document.removeEventListener("keydown", onKeydown);
    };

    const onOk = () => {
      cleanup();
      resolve(true);
    };

    const onCancel = () => {
      cleanup();
      resolve(false);
    };

    const onOverlayClick = (e) => {
      if (e.target === overlay) {
        cleanup();
        resolve(false);
      }
    };

    const onKeydown = (e) => {
      if (e.key === "Escape") {
        cleanup();
        resolve(false);
      } else if (e.key === "Enter") {
        cleanup();
        resolve(true);
      }
    };

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("click", onOverlayClick);
    document.addEventListener("keydown", onKeydown);

    // Focus the cancel button for safety (less destructive default)
    cancelBtn.focus();
  });
}

/**
 * Show a custom input modal dialog.
 * Works consistently across desktop and mobile.
 * @param {string} message - The prompt message to display
 * @param {Object} [options] - Optional configuration
 * @param {string} [options.placeholder=""] - Placeholder text for input
 * @param {string} [options.defaultValue=""] - Default value for input
 * @param {string} [options.confirmText="OK"] - Text for the confirm button
 * @param {string} [options.cancelText="Cancel"] - Text for the cancel button
 * @returns {Promise<string|null>} - Resolves to the input value, or null if cancelled
 */
export function inputModal(message, options = {}) {
  const { placeholder = "", defaultValue = "", confirmText = "OK", cancelText = "Cancel" } = options;
  return new Promise((resolve) => {
    const overlay = document.getElementById("input-modal");
    const messageEl = document.getElementById("input-modal-message");
    const inputEl = document.getElementById("input-modal-input");
    const okBtn = document.getElementById("input-modal-ok");
    const cancelBtn = document.getElementById("input-modal-cancel");

    messageEl.textContent = message;
    inputEl.placeholder = placeholder;
    inputEl.value = defaultValue;
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;
    overlay.hidden = false;

    const cleanup = () => {
      overlay.hidden = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onOverlayClick);
      document.removeEventListener("keydown", onKeydown);
    };

    const onOk = () => {
      const value = inputEl.value.trim();
      cleanup();
      resolve(value || null);
    };

    const onCancel = () => {
      cleanup();
      resolve(null);
    };

    const onOverlayClick = (e) => {
      if (e.target === overlay) {
        cleanup();
        resolve(null);
      }
    };

    const onKeydown = (e) => {
      if (e.key === "Escape") {
        cleanup();
        resolve(null);
      } else if (e.key === "Enter" && document.activeElement === inputEl) {
        e.preventDefault();
        onOk();
      }
    };

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("click", onOverlayClick);
    document.addEventListener("keydown", onKeydown);

    // Focus the input field
    inputEl.focus();
    inputEl.select();
  });
}
