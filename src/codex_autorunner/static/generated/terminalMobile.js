// GENERATED FILE - do not edit directly. Source: static_src/
import { isMobileViewport } from "./utils.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { getBufferSnapshot, snapshotBufferLines, isAltBufferActive, clearAltScrollbackState, updateAltScrollback, } from "./terminalTranscript.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
const textEncoder = new TextEncoder();
export function createMobileState() {
    return {
        mobileControlsEl: null,
        ctrlActive: false,
        altActive: false,
        baseViewportHeight: window.innerHeight,
        suppressNextSendClick: false,
        lastSendTapAt: 0,
        textInputWasFocused: false,
        deferScrollRestore: false,
        savedViewportY: null,
        savedAtBottom: null,
        mobileViewEl: null,
        mobileViewActive: false,
        mobileViewScrollTop: null,
        mobileViewAtBottom: true,
        mobileViewRaf: null,
        mobileViewDirty: false,
        mobileViewSuppressAtBottomRecalc: false,
        wheelScrollInstalled: false,
        wheelScrollRemainder: 0,
        touchScrollInstalled: false,
        touchScrollLastY: null,
        touchScrollRemainder: 0,
    };
}
export function updateViewportInsets(state, terminalSectionEl) {
    const viewportHeight = window.innerHeight;
    if (viewportHeight > state.baseViewportHeight) {
        state.baseViewportHeight = viewportHeight;
    }
    let bottom = 0;
    let top = 0;
    const vv = window.visualViewport;
    if (vv) {
        const layoutHeight = document.documentElement?.clientHeight || viewportHeight;
        const vvOffset = Math.max(0, vv.offsetTop);
        top = vvOffset;
        bottom = Math.max(0, layoutHeight - (vv.height + vvOffset));
    }
    const keyboardFallback = vv ? 0 : Math.max(0, state.baseViewportHeight - viewportHeight);
    const inset = bottom || keyboardFallback;
    document.documentElement.style.setProperty("--vv-bottom", `${inset}px`);
    document.documentElement.style.setProperty("--vv-top", `${top}px`);
    terminalSectionEl?.style.setProperty("--vv-bottom", `${inset}px`);
    terminalSectionEl?.style.setProperty("--vv-top", `${top}px`);
}
export function captureTerminalScrollState(state, term) {
    if (!term)
        return;
    const buffer = term.buffer?.active;
    if (!buffer)
        return;
    state.savedViewportY = buffer.viewportY;
    state.savedAtBottom = buffer.viewportY >= buffer.baseY;
}
export function restoreTerminalScrollState(state, term, updateJumpBottomVisibility) {
    if (!term)
        return;
    const buffer = term.buffer?.active;
    if (!buffer)
        return;
    if (state.savedAtBottom) {
        term.scrollToBottom();
    }
    else if (Number.isInteger(state.savedViewportY)) {
        const delta = state.savedViewportY - buffer.viewportY;
        if (delta !== 0) {
            term.scrollLines(delta);
        }
    }
    updateJumpBottomVisibility();
    state.savedViewportY = null;
    state.savedAtBottom = null;
}
export function scrollToBottomIfNearBottom(term, updateJumpBottomVisibility) {
    if (!term)
        return;
    const buffer = term.buffer?.active;
    if (!buffer)
        return;
    const atBottom = buffer.viewportY >= buffer.baseY - 1;
    if (atBottom) {
        term.scrollToBottom();
        updateJumpBottomVisibility();
    }
}
export function initMobileView(state) {
    if (state.mobileViewEl)
        return;
    const existing = document.getElementById("mobile-terminal-view");
    if (existing) {
        state.mobileViewEl = existing;
    }
    else {
        state.mobileViewEl = document.createElement("div");
        state.mobileViewEl.id = "mobile-terminal-view";
        state.mobileViewEl.className = "mobile-terminal-view hidden";
        document.body.appendChild(state.mobileViewEl);
    }
    state.mobileViewEl.addEventListener("scroll", () => {
        if (!state.mobileViewEl)
            return;
        state.mobileViewScrollTop = state.mobileViewEl.scrollTop;
        const threshold = 4;
        state.mobileViewAtBottom =
            state.mobileViewEl.scrollTop + state.mobileViewEl.clientHeight >=
                state.mobileViewEl.scrollHeight - threshold;
    });
}
export function setMobileViewActive(state, active, isTouchDevice, term, transcriptState) {
    if (!isTouchDevice || !isMobileViewport())
        return;
    initMobileView(state);
    if (!state.mobileViewEl)
        return;
    const wasActive = state.mobileViewActive;
    state.mobileViewActive = Boolean(active);
    if (!state.mobileViewActive) {
        state.mobileViewEl.classList.add("hidden");
        return;
    }
    if (!wasActive) {
        state.mobileViewAtBottom = true;
        state.mobileViewScrollTop = null;
    }
    else {
        const buffer = term?.buffer?.active;
        if (buffer) {
            const atBottom = buffer.viewportY >= buffer.baseY;
            state.mobileViewAtBottom = atBottom;
        }
    }
    const shouldScrollToBottom = state.mobileViewAtBottom;
    state.mobileViewSuppressAtBottomRecalc = true;
    state.mobileViewEl.classList.remove("hidden");
    renderMobileView(state, term, transcriptState);
    state.mobileViewSuppressAtBottomRecalc = false;
    if (shouldScrollToBottom) {
        requestAnimationFrame(() => {
            if (!state.mobileViewEl || !state.mobileViewActive)
                return;
            state.mobileViewEl.scrollTop = state.mobileViewEl.scrollHeight;
        });
    }
}
export function scheduleMobileViewRender(state, term, transcriptState, awaitingReplayEnd) {
    if (awaitingReplayEnd) {
        renderMobileView(state, term, transcriptState);
        return;
    }
    state.mobileViewDirty = true;
    if (state.mobileViewRaf)
        return;
    state.mobileViewRaf = requestAnimationFrame(() => {
        state.mobileViewRaf = null;
        if (!state.mobileViewDirty)
            return;
        state.mobileViewDirty = false;
        renderMobileView(state, term, transcriptState);
    });
}
export function renderMobileView(state, term, transcriptState) {
    if (!term)
        return;
    const shouldRender = state.mobileViewActive && state.mobileViewEl;
    const useAltBuffer = isAltBufferActive(term);
    if (!shouldRender && !useAltBuffer) {
        if (transcriptState.altScrollbackLines.length || transcriptState.altSnapshotPlain) {
            clearAltScrollbackState(transcriptState);
        }
        return;
    }
    const bufferSnapshot = getBufferSnapshot(term, transcriptState.maxLines);
    if (!Array.isArray(bufferSnapshot?.lines)) {
        if (shouldRender && state.mobileViewEl) {
            state.mobileViewEl.innerHTML = "";
        }
        clearAltScrollbackState(transcriptState);
        return;
    }
    const bufferSnapshotLines = snapshotBufferLines(transcriptState, term, bufferSnapshot);
    if (!bufferSnapshotLines?.html) {
        if (shouldRender && state.mobileViewEl) {
            state.mobileViewEl.innerHTML = "";
        }
        return;
    }
    if (useAltBuffer) {
        updateAltScrollback(transcriptState, bufferSnapshotLines.plain, bufferSnapshotLines.html);
    }
    else {
        clearAltScrollbackState(transcriptState);
    }
    if (!shouldRender)
        return;
    if (state.mobileViewEl &&
        !state.mobileViewEl.classList.contains("hidden") &&
        !state.mobileViewSuppressAtBottomRecalc) {
        const threshold = 4;
        state.mobileViewAtBottom =
            state.mobileViewEl.scrollTop + state.mobileViewEl.clientHeight >=
                state.mobileViewEl.scrollHeight - threshold;
    }
    let content = "";
    if (useAltBuffer) {
        for (const line of transcriptState.altScrollbackLines) {
            content += `${line}\n`;
        }
    }
    for (const line of bufferSnapshotLines.html) {
        content += `${line}\n`;
    }
    if (state.mobileViewEl) {
        state.mobileViewEl.innerHTML = content;
        if (state.mobileViewAtBottom) {
            state.mobileViewEl.scrollTop = state.mobileViewEl.scrollHeight;
        }
        else if (state.mobileViewScrollTop !== null) {
            const maxScroll = state.mobileViewEl.scrollHeight - state.mobileViewEl.clientHeight;
            state.mobileViewEl.scrollTop = Math.min(state.mobileViewScrollTop, maxScroll);
        }
    }
}
export function sendKey(state, seq, socket, markSessionActive, updateModifierButtons) {
    if (!socket || socket.readyState !== WebSocket.OPEN)
        return;
    if (state.ctrlActive && seq.length === 1) {
        const char = seq.toUpperCase();
        const code = char.charCodeAt(0) - 64;
        if (code >= 1 && code <= 26) {
            seq = String.fromCharCode(code);
        }
    }
    markSessionActive();
    socket.send(textEncoder.encode(seq));
    state.ctrlActive = false;
    state.altActive = false;
    updateModifierButtons();
}
export function sendCtrl(char, socket, markSessionActive) {
    if (!socket || socket.readyState !== WebSocket.OPEN)
        return;
    const code = char.toUpperCase().charCodeAt(0) - 64;
    markSessionActive();
    socket.send(textEncoder.encode(String.fromCharCode(code)));
}
export function updateModifierButtons(state) {
    const ctrlBtn = document.getElementById("tmb-ctrl");
    const altBtn = document.getElementById("tmb-alt");
    if (ctrlBtn)
        ctrlBtn.classList.toggle("active", state.ctrlActive);
    if (altBtn)
        altBtn.classList.toggle("active", state.altActive);
}
export function initMobileControls(state, deps) {
    state.mobileControlsEl = document.getElementById("terminal-mobile-controls");
    if (!state.mobileControlsEl)
        return;
    if (!deps.isTouchDevice()) {
        state.mobileControlsEl.style.display = "none";
        return;
    }
    state.mobileControlsEl.addEventListener("click", (e) => {
        const btn = e.target?.closest(".tmb-key");
        if (!btn)
            return;
        e.preventDefault();
        const modKey = btn.dataset.key;
        if (modKey && modKey === "ctrl") {
            state.ctrlActive = !state.ctrlActive;
            updateModifierButtons(state);
            return;
        }
        if (modKey && modKey === "alt") {
            state.altActive = !state.altActive;
            updateModifierButtons(state);
            return;
        }
        const ctrlChar = btn.dataset.ctrl;
        if (ctrlChar) {
            sendCtrl(ctrlChar, deps.getSocket(), deps.markSessionActive);
            if (deps.isTouchDevice() && deps.getTextInputEnabled() && state.textInputWasFocused) {
                setTimeout(() => deps.safeFocus(deps.getTextInputTextareaEl()));
            }
            return;
        }
        const seq = btn.dataset.seq;
        if (seq) {
            sendKey(state, seq, deps.getSocket(), deps.markSessionActive, () => updateModifierButtons(state));
            if (deps.isTouchDevice() && deps.getTextInputEnabled() && state.textInputWasFocused) {
                setTimeout(() => deps.safeFocus(deps.getTextInputTextareaEl()));
            }
            return;
        }
    });
    state.mobileControlsEl.addEventListener("touchstart", (e) => {
        if (e.target?.closest(".tmb-key") && navigator.vibrate) {
            navigator.vibrate(10);
        }
    }, { passive: true });
}
export function installWheelScroll(state, term, isTouchDevice) {
    if (state.wheelScrollInstalled || !term || !term.element)
        return;
    if (isTouchDevice)
        return;
    const wheelTarget = term.element;
    const wheelListener = (event) => {
        if (!term || !event)
            return;
        if (event.ctrlKey)
            return;
        const buffer = term.buffer?.active;
        const mouseTracking = term?.modes?.mouseTrackingMode;
        if (mouseTracking && mouseTracking !== "none") {
            return;
        }
        if (!buffer || buffer.baseY <= 0) {
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        let deltaLines;
        if (event.deltaMode === WheelEvent.DOM_DELTA_LINE) {
            deltaLines = event.deltaY;
        }
        else if (event.deltaMode === WheelEvent.DOM_DELTA_PAGE) {
            deltaLines = event.deltaY * term.rows;
        }
        else {
            deltaLines = event.deltaY / 40;
        }
        const options = term.options || {};
        if (Number.isFinite(options.scrollSensitivity)) {
            deltaLines *= options.scrollSensitivity;
        }
        const modifier = options.fastScrollModifier || "alt";
        const fastSensitivity = Number.isFinite(options.fastScrollSensitivity)
            ? options.fastScrollSensitivity
            : 5;
        const modifierActive = modifier !== "none" &&
            ((modifier === "alt" && event.altKey) ||
                (modifier === "ctrl" && event.ctrlKey) ||
                (modifier === "shift" && event.shiftKey) ||
                (modifier === "meta" && event.metaKey));
        if (modifierActive) {
            deltaLines *= fastSensitivity;
        }
        state.wheelScrollRemainder += deltaLines;
        const wholeLines = Math.trunc(state.wheelScrollRemainder);
        if (wholeLines !== 0) {
            term.scrollLines(wholeLines);
            state.wheelScrollRemainder -= wholeLines;
        }
    };
    wheelTarget.addEventListener("wheel", wheelListener, {
        passive: false,
        capture: true,
    });
    state.wheelScrollInstalled = true;
}
export function installTouchScroll(state, term, isTouchDevice) {
    if (state.touchScrollInstalled || !term || !term.element)
        return;
    if (!isTouchDevice)
        return;
    const viewport = term.element.querySelector(".xterm-viewport");
    if (!viewport)
        return;
    const getLineHeight = () => {
        const core = term?._core;
        const dims = core?._renderService?.dimensions;
        if (dims && Number.isFinite(dims.actualCellHeight) && dims.actualCellHeight > 0) {
            return dims.actualCellHeight;
        }
        const fontSize = typeof term.options?.fontSize === "number" ? term.options.fontSize : 14;
        return Math.max(10, Math.round(fontSize * 1.2));
    };
    const handleTouchStart = (event) => {
        if (!event.touches || event.touches.length !== 1)
            return;
        state.touchScrollLastY = event.touches[0].clientY;
        state.touchScrollRemainder = 0;
    };
    const handleTouchMove = (event) => {
        if (!event.touches || event.touches.length !== 1)
            return;
        if (!term || state.mobileViewActive)
            return;
        const mouseTracking = term?.modes?.mouseTrackingMode;
        if (mouseTracking && mouseTracking !== "none") {
            return;
        }
        const buffer = term.buffer?.active;
        if (!buffer || buffer.baseY <= 0)
            return;
        const currentY = event.touches[0].clientY;
        if (!Number.isFinite(state.touchScrollLastY)) {
            state.touchScrollLastY = currentY;
            return;
        }
        const delta = currentY - state.touchScrollLastY;
        state.touchScrollLastY = currentY;
        state.touchScrollRemainder += delta;
        const lineHeight = getLineHeight();
        const lines = Math.trunc(state.touchScrollRemainder / lineHeight);
        if (lines === 0)
            return;
        state.touchScrollRemainder -= lines * lineHeight;
        term.scrollLines(-lines);
        event.preventDefault();
        event.stopPropagation();
    };
    const handleTouchEnd = () => {
        state.touchScrollLastY = null;
        state.touchScrollRemainder = 0;
    };
    viewport.addEventListener("touchstart", handleTouchStart, { passive: true });
    viewport.addEventListener("touchmove", handleTouchMove, { passive: false });
    viewport.addEventListener("touchend", handleTouchEnd, { passive: true });
    viewport.addEventListener("touchcancel", handleTouchEnd, { passive: true });
    state.touchScrollInstalled = true;
}
