import {
  isMobileViewport,
  setMobileChromeHidden,
  setMobileComposeFixed,
} from "./utils.js";
import { subscribe } from "./bus.js";
import { getTerminalManager } from "./terminal.js";

const COMPOSE_INPUT_SELECTOR = "#doc-chat-input, #terminal-textarea";
const SEND_BUTTON_SELECTOR = "#doc-chat-send, #terminal-text-send";
let baseViewportHeight = window.innerHeight;

function isVisible(el) {
  if (!el) return false;
  return Boolean(el.offsetParent || el.getClientRects().length);
}

function isComposeFocused() {
  const el = document.activeElement;
  if (!el || !(el instanceof HTMLElement)) return false;
  return el.matches(COMPOSE_INPUT_SELECTOR);
}

function hasComposeDraft() {
  const inputs = Array.from(document.querySelectorAll(COMPOSE_INPUT_SELECTOR));
  return inputs.some((input) => {
    if (!(input instanceof HTMLTextAreaElement)) return false;
    if (!isVisible(input)) return false;
    return Boolean(input.value && input.value.trim());
  });
}

function updateViewportInset() {
  const viewportHeight = window.innerHeight;
  if (viewportHeight > baseViewportHeight) {
    baseViewportHeight = viewportHeight;
  }
  let bottom = 0;
  if (window.visualViewport) {
    const vv = window.visualViewport;
    const referenceHeight = Math.max(baseViewportHeight, viewportHeight);
    bottom = Math.max(0, referenceHeight - (vv.height + vv.offsetTop));
  }
  const keyboardFallback = window.visualViewport
    ? 0
    : Math.max(0, baseViewportHeight - viewportHeight);
  const inset = bottom || keyboardFallback;
  document.documentElement.style.setProperty("--vv-bottom", `${inset}px`);
}

function isTerminalComposeOpen() {
  const panel = document.getElementById("terminal");
  const input = document.getElementById("terminal-text-input");
  if (!panel || !input) return false;
  if (!panel.classList.contains("active")) return false;
  if (input.classList.contains("hidden")) return false;
  return isVisible(input);
}

function updateComposeFixed() {
  if (!isMobileViewport()) {
    setMobileComposeFixed(false);
    return;
  }
  const enabled = isComposeFocused() || hasComposeDraft() || isTerminalComposeOpen();
  setMobileComposeFixed(enabled);
}

export function initMobileCompact() {
  setMobileChromeHidden(false);

  const maybeHide = () => {
    if (!isMobileViewport()) return;
    if (!(isComposeFocused() || hasComposeDraft())) return;
    setMobileChromeHidden(true);
  };

  const show = () => {
    if (!isMobileViewport()) return;
    setMobileChromeHidden(false);
    updateComposeFixed();
    // Force a visual update
    document.documentElement.style.display = 'none';
    document.documentElement.offsetHeight; // trigger reflow
    document.documentElement.style.display = '';
  };

  window.addEventListener("scroll", maybeHide, { passive: true });
  document.addEventListener("scroll", maybeHide, { passive: true, capture: true });
  document.addEventListener("touchmove", maybeHide, { passive: true });
  document.addEventListener("wheel", maybeHide, { passive: true });

  document.addEventListener(
    "focusin",
    (e) => {
      if (!isMobileViewport()) return;
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.matches(COMPOSE_INPUT_SELECTOR)) return;
      updateComposeFixed();
      setMobileChromeHidden(false); // Ensure chrome is shown (or hidden? logic seems to be "hide chrome when focused" in maybeHide?)
      
      // If we are focusing the terminal input, switch to mobile view
      if (target.id === "terminal-textarea") {
         getTerminalManager()?.enterMobileInputMode();
      }
    },
    true
  );

  document.addEventListener(
    "focusout",
    (e) => {
      if (!isMobileViewport()) return;
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.matches(COMPOSE_INPUT_SELECTOR)) return;
      setTimeout(() => {
        if (isComposeFocused()) return;
        show();
        getTerminalManager()?.exitMobileInputMode();
      }, 50); // Slight increase to ensure reliable restore
    },
    true
  );

  document.addEventListener(
    "click",
    (e) => {
      if (!isMobileViewport()) return;
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.closest(SEND_BUTTON_SELECTOR)) return;
      show();
    },
    true
  );

  document.addEventListener(
    "input",
    (e) => {
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.matches(COMPOSE_INPUT_SELECTOR)) return;
      updateComposeFixed();
    },
    true
  );

  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", updateViewportInset);
    window.visualViewport.addEventListener("scroll", updateViewportInset);
    updateViewportInset();
  }

  window.addEventListener(
    "resize",
    () => {
      if (!isMobileViewport()) {
        setMobileChromeHidden(false);
      }
      updateComposeFixed();
    },
    { passive: true }
  );

  subscribe("tab:change", () => {
    show();
  });

  updateComposeFixed();
}
