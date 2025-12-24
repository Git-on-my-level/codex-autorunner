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
let viewportPoll = null;

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
  return true;
}

function updateComposeFixed() {
  if (!isMobileViewport()) {
    setMobileComposeFixed(false);
    return;
  }
  const enabled =
    isComposeFocused() || hasComposeDraft() || isTerminalComposeOpen();
  setMobileComposeFixed(enabled);
  // Always update viewport inset when compose state changes so the composer
  // is positioned correctly above the keyboard even when not focused.
  if (enabled) {
    updateViewportInset();
    updateMobileControlsOffset();
  }
  updateDocComposeOffset();
}

/**
 * Measure the actual height of the terminal text input panel and set a CSS
 * variable so the mobile controls can be positioned exactly above it.
 */
function updateMobileControlsOffset() {
  const textInput = document.getElementById("terminal-text-input");
  const mobileControls = document.getElementById("terminal-mobile-controls");
  if (!textInput || !mobileControls) return;

  // Get the actual rendered height of the text input panel
  const textInputHeight = textInput.offsetHeight || 0;
  // Add a small gap between controls and text input
  const offset = textInputHeight + 4;
  document.documentElement.style.setProperty(
    "--compose-input-height",
    `${offset}px`
  );

  // Also set the total height for padding-bottom calculation
  const controlsHeight = mobileControls.offsetHeight || 0;
  const totalHeight = textInputHeight + controlsHeight + 8;
  document.documentElement.style.setProperty(
    "--compose-total-height",
    `${totalHeight}px`
  );
}

function updateDocComposeOffset() {
  const compose = document.querySelector("#docs .doc-chat-compose");
  if (!compose || !isVisible(compose)) return;
  const composeHeight = compose.offsetHeight || 0;
  if (!composeHeight) return;
  const offset = composeHeight + 8;
  document.documentElement.style.setProperty(
    "--doc-compose-height",
    `${offset}px`
  );
}

function isTerminalTextarea(el) {
  return Boolean(
    el && el instanceof HTMLElement && el.id === "terminal-textarea"
  );
}

export function initMobileCompact() {
  setMobileChromeHidden(false);

  const maybeHide = () => {
    if (!isMobileViewport()) return;
    if (!(isComposeFocused() || hasComposeDraft())) return;
    setMobileChromeHidden(true);
    updateDocComposeOffset();
  };

  const show = () => {
    if (!isMobileViewport()) return;
    setMobileChromeHidden(false);
    updateComposeFixed();
    // Force a visual update
    document.documentElement.style.display = "none";
    document.documentElement.offsetHeight; // trigger reflow
    document.documentElement.style.display = "";
  };

  window.addEventListener("scroll", maybeHide, { passive: true });
  document.addEventListener("scroll", maybeHide, {
    passive: true,
    capture: true,
  });
  document.addEventListener("touchmove", maybeHide, { passive: true });
  document.addEventListener("wheel", maybeHide, { passive: true });

  document.addEventListener(
    "focusin",
    (e) => {
      if (!isMobileViewport()) return;
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.matches(COMPOSE_INPUT_SELECTOR)) return;
      updateViewportInset();
      updateComposeFixed();
      setMobileChromeHidden(false);
      updateDocComposeOffset();

      // Start polling for viewport changes (keyboard animation)
      if (viewportPoll) clearInterval(viewportPoll);
      viewportPoll = setInterval(updateViewportInset, 100);

      if (isTerminalTextarea(target)) {
        getTerminalManager()?.scheduleResizeAfterLayout?.();
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

      if (viewportPoll) {
        clearInterval(viewportPoll);
        viewportPoll = null;
      }

      setTimeout(() => {
        // Always update viewport inset - keyboard may still be visible or transitioning
        updateViewportInset();
        if (isComposeFocused()) return;
        show();
        getTerminalManager()?.scheduleResizeAfterLayout?.();
      }, 50);
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
      // Defer show() to allow the click event to reach the button listener (bubbling phase)
      // before potentially forcing a reflow that cancels the event.
      requestAnimationFrame(() => show());
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

  // Update viewport inset on any focus change when terminal compose is open.
  // This ensures the composer stays positioned correctly above the keyboard
  // even when focus moves to buttons (like mobile control keys).
  document.addEventListener(
    "focusin",
    () => {
      if (!isMobileViewport()) return;
      if (isTerminalComposeOpen()) {
        updateViewportInset();
      }
    },
    true
  );

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

  subscribe("terminal:compose", (data) => {
    updateViewportInset();
    updateComposeFixed();
    // Delay to ensure DOM has updated with new panel visibility
    requestAnimationFrame(() => {
      updateMobileControlsOffset();
      updateDocComposeOffset();
    });
  });

  updateComposeFixed();
  // Initial measurement after layout
  requestAnimationFrame(() => {
    updateMobileControlsOffset();
    updateDocComposeOffset();
  });
}
