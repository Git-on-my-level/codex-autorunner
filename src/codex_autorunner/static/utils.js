import { CONSTANTS } from "./constants.js";
import { BASE_PATH } from "./env.js";

const toast = document.getElementById("toast");
const decoder = new TextDecoder();
const AUTH_TOKEN_KEY = "car_auth_token";

/**
 * @typedef {Object} ApiOptions
 * @property {string} [method]
 * @property {any} [body]
 * @property {Record<string, string>} [headers]
 */

/**
 * @typedef {Object} StreamOptions
 * @property {string} [method]
 * @property {any} [body]
 * @property {(data: string, event: string) => void} [onMessage]
 * @property {(err: Error) => void} [onError]
 * @property {() => void} [onFinish]
 */

export function getAuthToken() {
  let token = null;
  try {
    token = sessionStorage.getItem(AUTH_TOKEN_KEY);
  } catch (_err) {
    token = null;
  }
  if (token) {
    return token;
  }
  if (window?.__CAR_AUTH_TOKEN) {
    return window.__CAR_AUTH_TOKEN;
  }
  return null;
}

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

export function getUrlParams() {
  try {
    return new URLSearchParams(window.location.search || "");
  } catch (_err) {
    return new URLSearchParams();
  }
}

export function updateUrlParams(updates = {}) {
  if (!window?.location?.href) return;
  if (typeof history === "undefined" || !history.replaceState) return;
  const url = new URL(window.location.href);
  const params = url.searchParams;
  Object.entries(updates).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") {
      params.delete(key);
    } else {
      params.set(key, String(value));
    }
  });
  url.search = params.toString();
  history.replaceState(null, "", url.toString());
}

export function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function buildWsUrl(path, query = "") {
  const resolved = resolvePath(path);
  const normalized = resolved.startsWith("/") ? resolved : `/${resolved}`;
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams(query.startsWith("?") ? query.slice(1) : query);
  const suffix = params.toString();
  return `${proto}://${window.location.host}${normalized}${suffix ? `?${suffix}` : ""}`;
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
  const warnStates = [
    "locked",
    "missing",
    "uninitialized",
    "initializing",
    "interrupted",
  ];
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

function extractErrorDetail(payload) {
  if (!payload || typeof payload !== "object") return "";
  const detail = payload.detail ?? payload.message ?? payload.error;
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (!item) return "";
        if (typeof item === "string") return item;
        if (typeof item === "object") {
          const msg = item.msg || item.message || "";
          const loc = Array.isArray(item.loc) ? item.loc.join(".") : item.loc;
          if (msg && loc) return `${loc}: ${msg}`;
          if (msg) return msg;
        }
        try {
          return JSON.stringify(item);
        } catch (_err) {
          return String(item);
        }
      })
      .filter(Boolean);
    return parts.join(" | ");
  }
  try {
    return JSON.stringify(detail);
  } catch (_err) {
    return String(detail);
  }
}

async function buildErrorMessage(res) {
  if (res.status === 401) {
    return "Unauthorized. Provide a valid token to access this server.";
  }
  let text = "";
  try {
    text = await res.text();
  } catch (_err) {
    text = "";
  }
  let payload = null;
  const contentType = res.headers.get("content-type") || "";
  const trimmed = text.trim();
  if (
    contentType.includes("application/json") ||
    trimmed.startsWith("{") ||
    trimmed.startsWith("[")
  ) {
    try {
      payload = JSON.parse(text);
    } catch (_err) {
      payload = null;
    }
  }
  const detail = extractErrorDetail(payload);
  if (detail) return detail;
  if (text) return text;
  return `Request failed (${res.status})`;
}

/**
 * @param {string} path
 * @param {ApiOptions} [options]
 */
export async function api(path, options = {}) {
  /** @type {Record<string, string>} */
  const headers = options.headers ? { ...options.headers } : {};
  const opts = { ...options, headers };
  const target = resolvePath(path);
  const token = getAuthToken();
  if (token && !headers.Authorization) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(target, opts);
  if (!res.ok) {
    const message = await buildErrorMessage(res);
    throw new Error(message);
  }
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return res.json();
  }
  return res.text();
}

/**
 * @param {string} path
 * @param {StreamOptions} [options]
 */
export function streamEvents(path, options = {}) {
  const { method = "GET", body = null, onMessage, onError, onFinish } = options;
  const controller = new AbortController();
  let fetchBody = body;
  const target = resolvePath(path);
  /** @type {Record<string, string>} */
  const headers = {};
  const token = getAuthToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (fetchBody && typeof fetchBody === "object" && !(fetchBody instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    fetchBody = JSON.stringify(fetchBody);
  }
  fetch(target, { method, body: fetchBody, headers, signal: controller.signal })
    .then(async (res) => {
      if (!res.ok) {
        const message = await buildErrorMessage(res);
        throw new Error(message);
      }
      if (!res.body) {
        throw new Error("Streaming not supported in this browser");
      }
      const reader = res.body.getReader();
      let buffer = "";
      for (;;) {
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
          if (!dataLines.length) continue;
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

export function isMobileViewport() {
  try {
    return Boolean(window.matchMedia && window.matchMedia("(max-width: 640px)").matches);
  } catch (_err) {
    return window.innerWidth <= 640;
  }
}

export function setMobileChromeHidden(hidden) {
  document.documentElement.classList.toggle("mobile-chrome-hidden", Boolean(hidden));
}

export function setMobileComposeFixed(enabled) {
  document.documentElement.classList.toggle("mobile-compose-fixed", Boolean(enabled));
}

const MODAL_BACKGROUND_IDS = ["hub-shell", "repo-shell"];
const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type=\"hidden\"])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex=\"-1\"])",
].join(",");
let modalOpenCount = 0;

function getFocusableElements(container) {
  if (!container || !container.querySelectorAll) return [];
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter(
    (el) => el && el.tabIndex !== -1 && !el.hidden && !el.disabled
  );
}

function setModalBackgroundHidden(hidden) {
  if (hidden) {
    modalOpenCount += 1;
  } else {
    modalOpenCount = Math.max(0, modalOpenCount - 1);
  }
  const shouldHide = modalOpenCount > 0;
  MODAL_BACKGROUND_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (shouldHide) {
      el.setAttribute("aria-hidden", "true");
      try {
        el.inert = true;
      } catch (_err) {
        el.setAttribute("inert", "");
      }
    } else {
      el.removeAttribute("aria-hidden");
      try {
        el.inert = false;
      } catch (_err) {
        el.removeAttribute("inert");
      }
    }
  });
}

function handleTabKey(event, container) {
  const focusable = getFocusableElements(container);
  if (!focusable.length) {
    event.preventDefault();
    container?.focus?.();
    return;
  }
  const currentIndex = focusable.indexOf(document.activeElement);
  const lastIndex = focusable.length - 1;
  if (event.shiftKey) {
    if (currentIndex <= 0) {
      event.preventDefault();
      focusable[lastIndex].focus();
    }
  } else if (currentIndex === -1 || currentIndex === lastIndex) {
    event.preventDefault();
    focusable[0].focus();
  }
}

export function openModal(overlay, options = {}) {
  if (!overlay) return () => {};
  const {
    closeOnEscape = true,
    closeOnOverlay = true,
    initialFocus,
    returnFocusTo,
    onKeydown,
    onRequestClose,
  } = options;
  const dialog = overlay.querySelector(".modal-dialog") || overlay;
  const previousActive = returnFocusTo || document.activeElement;
  let isClosed = false;

  const close = () => {
    if (isClosed) return;
    isClosed = true;
    overlay.hidden = true;
    overlay.removeEventListener("click", handleOverlayClick);
    document.removeEventListener("keydown", handleKeydown);
    setModalBackgroundHidden(false);
    if (previousActive && previousActive.focus) {
      previousActive.focus();
    }
  };

  const requestClose = onRequestClose || close;

  const handleOverlayClick = (event) => {
    if (closeOnOverlay && event.target === overlay) {
      requestClose("overlay");
    }
  };

  const handleKeydown = (event) => {
    if (event.key === "Escape" && closeOnEscape) {
      event.preventDefault();
      requestClose("escape");
      return;
    }
    if (event.key === "Tab") {
      handleTabKey(event, dialog);
      return;
    }
    if (onKeydown) {
      onKeydown(event);
    }
  };

  overlay.hidden = false;
  setModalBackgroundHidden(true);

  overlay.addEventListener("click", handleOverlayClick);
  document.addEventListener("keydown", handleKeydown);

  const focusTarget = initialFocus || getFocusableElements(dialog)[0] || dialog;
  if (focusTarget && focusTarget.focus) {
    focusTarget.focus();
  }

  return close;
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

    const triggerEl = document.activeElement;
    messageEl.textContent = message;
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;
    okBtn.className = danger ? "danger" : "primary";
    let closeModal = null;
    let settled = false;

    const finalize = (result) => {
      if (settled) return;
      settled = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      if (closeModal) {
        const close = closeModal;
        closeModal = null;
        close();
      }
      resolve(result);
    };

    const onOk = () => {
      finalize(true);
    };

    const onCancel = () => {
      finalize(false);
    };

    closeModal = openModal(overlay, {
      initialFocus: cancelBtn,
      returnFocusTo: triggerEl,
      onRequestClose: () => finalize(false),
      onKeydown: (event) => {
        if (event.key === "Enter" && document.activeElement === okBtn) {
          event.preventDefault();
          finalize(true);
        }
      },
    });

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
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
    const inputEl = /** @type {HTMLInputElement} */ (
      document.getElementById("input-modal-input")
    );
    const okBtn = /** @type {HTMLButtonElement} */ (
      document.getElementById("input-modal-ok")
    );
    const cancelBtn = /** @type {HTMLButtonElement} */ (
      document.getElementById("input-modal-cancel")
    );

    const triggerEl = document.activeElement;
    messageEl.textContent = message;
    inputEl.placeholder = placeholder;
    inputEl.value = defaultValue;
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;
    let closeModal = null;
    let settled = false;

    const finalize = (result) => {
      if (settled) return;
      settled = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      if (closeModal) {
        const close = closeModal;
        closeModal = null;
        close();
      }
      resolve(result);
    };

    const onOk = () => {
      const value = inputEl.value.trim();
      finalize(value || null);
    };

    const onCancel = () => {
      finalize(null);
    };

    closeModal = openModal(overlay, {
      initialFocus: inputEl,
      returnFocusTo: triggerEl,
      onRequestClose: () => finalize(null),
      onKeydown: (event) => {
        if (event.key === "Enter") {
          const active = document.activeElement;
          if (active === inputEl || active === okBtn) {
            event.preventDefault();
            onOk();
          }
        }
      },
    });

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);

    // Focus the input field
    inputEl.focus();
    inputEl.select();
  });
}
