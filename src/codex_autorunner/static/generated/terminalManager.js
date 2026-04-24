// GENERATED FILE - do not edit directly. Source: static_src/
import { flash, isMobileViewport } from "./utils.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { getSelectedAgent, getSelectedProfile, getSelectedModel, getSelectedReasoning, initAgentControls, } from "./agentControls.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { getSavedSessionId as getSessionId, setSavedSessionId as setSessionId, clearSavedSessionId as clearSessionId, markSessionActive as sessionMarkActive, buildConnectQuery, createTerminalSocket, teardownSocket as sessionTeardownSocket, SocketHeartbeat, ReconnectScheduler, } from "./terminalSession.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { createReplayState, resetReplayState, initReplayForConnect, bufferReplayChunk, handleReplayEnd, consumeLiveReset, } from "./terminalReplay.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { createTranscriptState, resetTranscript, restoreTranscript, hydrateTerminalFromTranscript, appendTranscriptChunk, isAltBufferActive, } from "./terminalTranscript.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { TEXT_INPUT_STORAGE_KEYS, createTextInputState, readBoolFromStorage, safeFocus, captureTextInputSelection, updateTextInputSendUi, persistTextInputDraft, restoreTextInputDraft, loadPendingTextInput, sendPendingTextInputChunk, handleTextInputAck, sendFromTextarea, setTextInputEnabled, updateComposerSticky, registerTextInputHook, hasTextInputHookFired, markTextInputHookFired, migrateTextInputHookSession, handleImageFiles, } from "./terminalTextInput.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { createMobileState, updateViewportInsets, captureTerminalScrollState, restoreTerminalScrollState, scrollToBottomIfNearBottom, setMobileViewActive, scheduleMobileViewRender, initMobileControls, installWheelScroll, installTouchScroll, } from "./terminalMobile.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { createVoiceState, initTerminalVoice, handleVoiceHotkeyDown, handleVoiceHotkeyUp, } from "./terminalVoice.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
const textEncoder = new TextEncoder();
import { CONSTANTS } from "./constants.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { subscribe } from "./bus.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { REPO_ID, BASE_PATH } from "./env.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
const TOUCH_OVERRIDE = (() => {
    try {
        const params = new URLSearchParams(window.location.search);
        const truthy = new Set(["1", "true", "yes", "on"]);
        const falsy = new Set(["0", "false", "no", "off"]);
        const touchParam = params.get("force_touch") ?? params.get("touch");
        if (touchParam !== null) {
            const value = String(touchParam).toLowerCase();
            if (truthy.has(value))
                return true;
            if (falsy.has(value))
                return false;
        }
        const desktopParam = params.get("force_desktop") ?? params.get("desktop");
        if (desktopParam !== null) {
            const value = String(desktopParam).toLowerCase();
            if (truthy.has(value))
                return false;
            if (falsy.has(value))
                return true;
        }
        return null;
    }
    catch (_err) {
        return null;
    }
})();
const TERMINAL_DEBUG = (() => {
    try {
        const params = new URLSearchParams(window.location.search);
        const truthy = new Set(["1", "true", "yes", "on"]);
        const falsy = new Set(["0", "false", "no", "off"]);
        const param = params.get("terminal_debug") ?? params.get("debug_terminal");
        if (param !== null) {
            const value = String(param).toLowerCase();
            if (truthy.has(value))
                return true;
            if (falsy.has(value))
                return false;
        }
        try {
            const stored = localStorage.getItem("codex_terminal_debug");
            if (stored !== null) {
                const value = String(stored).toLowerCase();
                if (truthy.has(value))
                    return true;
                if (falsy.has(value))
                    return false;
            }
        }
        catch (_err) {
            // ignore storage errors
        }
        return false;
    }
    catch (_err) {
        return false;
    }
})();
const CAR_CONTEXT_HOOK_ID = "car_context";
const CAR_CONTEXT_HINT = wrapInjectedContext(CONSTANTS.PROMPTS.CAR_CONTEXT_HINT);
const INJECTED_CONTEXT_TAG_RE = /<injected context>/i;
const CAR_CONTEXT_COMMAND_RE = [
    /^\/\S/,
    /^\.\/\S/,
    /^git(\s|$)/,
    /^cd(\s|$)/,
    /^ls(\s|$)/,
    /^make(\s|$)/,
    /^pnpm(\s|$)/,
    /^npm(\s|$)/,
    /^python3?(\s|$)/,
];
function wrapInjectedContext(text) {
    return `<injected context>\n${text}\n</injected context>`;
}
function wrapInjectedContextIfNeeded(text) {
    if (!text)
        return text;
    return INJECTED_CONTEXT_TAG_RE.test(text) ? text : wrapInjectedContext(text);
}
function looksLikeCommand(text) {
    const trimmed = text.trim();
    if (!trimmed)
        return false;
    const lowered = trimmed.toLowerCase();
    return CAR_CONTEXT_COMMAND_RE.some((pattern) => pattern.test(lowered));
}
export class TerminalManager {
    constructor() {
        this.term = null;
        this.fitAddon = null;
        this.socket = null;
        this.inputDisposable = null;
        this.intentionalDisconnect = false;
        this.reconnect = new ReconnectScheduler();
        this.heartbeat = new SocketHeartbeat(TERMINAL_DEBUG, (msg, details) => this._logTerminalDebug(msg, details));
        this.lastConnectMode = null;
        this.suppressNextNotFoundFlash = false;
        this.currentSessionId = null;
        this.statusBase = "Disconnected";
        this.terminalIdleTimeoutSeconds = null;
        this.sessionNotFound = false;
        this.terminalDebug = TERMINAL_DEBUG;
        this.replayChunkCount = 0;
        this.replayByteCount = 0;
        this.liveChunkCount = 0;
        this.liveByteCount = 0;
        this.lastAltBufferActive = null;
        this.lastAltScrollbackSize = 0;
        this.statusEl = null;
        this.overlayEl = null;
        this.connectBtn = null;
        this.disconnectBtn = null;
        this.resumeBtn = null;
        this.jumpBottomBtn = null;
        this.agentSelect = null;
        this.profileSelect = null;
        this.modelSelect = null;
        this.modelInput = null;
        this.reasoningSelect = null;
        this.resizeRaf = null;
        this.transcript = createTranscriptState();
        this.replayState = createReplayState();
        this.textInput = createTextInputState();
        this.mobile = createMobileState();
        this.voice = createVoiceState();
        this.term = null;
        this.fitAddon = null;
        this.socket = null;
        this.inputDisposable = null;
        this.intentionalDisconnect = false;
        this.lastConnectMode = null;
        this.suppressNextNotFoundFlash = false;
        this.currentSessionId = null;
        this.statusBase = "Disconnected";
        this.terminalIdleTimeoutSeconds = null;
        this.sessionNotFound = false;
        this.terminalDebug = TERMINAL_DEBUG;
        this.replayChunkCount = 0;
        this.replayByteCount = 0;
        this.liveChunkCount = 0;
        this.liveByteCount = 0;
        this.lastAltBufferActive = null;
        this.lastAltScrollbackSize = 0;
        this.resizeRaf = null;
        this._resetTerminalDebugCounters();
        registerTextInputHook(this.textInput, this._buildCarContextHook());
        this._handleResize = this._handleResize.bind(this);
        this._handleVoiceHotkeyDown = this._handleVoiceHotkeyDown.bind(this);
        this._handleVoiceHotkeyUp = this._handleVoiceHotkeyUp.bind(this);
        this._scheduleResizeAfterLayout = this._scheduleResizeAfterLayout.bind(this);
    }
    isTouchDevice() {
        if (TOUCH_OVERRIDE !== null)
            return TOUCH_OVERRIDE;
        return "ontouchstart" in window || navigator.maxTouchPoints > 0;
    }
    init() {
        this.statusEl = document.getElementById("terminal-status");
        this.overlayEl = document.getElementById("terminal-overlay");
        this.connectBtn = document.getElementById("terminal-connect");
        this.disconnectBtn = document.getElementById("terminal-disconnect");
        this.resumeBtn = document.getElementById("terminal-resume");
        this.jumpBottomBtn = document.getElementById("terminal-jump-bottom");
        this.agentSelect = document.getElementById("terminal-agent-select");
        this.profileSelect = document.getElementById("terminal-profile-select");
        this.modelSelect = document.getElementById("terminal-model-select");
        this.modelInput = document.getElementById("terminal-model-input");
        this.reasoningSelect = document.getElementById("terminal-reasoning-select");
        if (!this.statusEl || !this.connectBtn || !this.disconnectBtn || !this.resumeBtn) {
            return;
        }
        this.connectBtn.addEventListener("click", () => this.connect({ mode: "new" }));
        this.resumeBtn.addEventListener("click", () => {
            const selectedAgent = getSelectedAgent();
            if (selectedAgent && selectedAgent.toLowerCase() === "opencode") {
                this.connect({ mode: "new" });
            }
            else {
                this.connect({ mode: "resume" });
            }
        });
        this.disconnectBtn.addEventListener("click", () => this.disconnect());
        this.jumpBottomBtn?.addEventListener("click", () => {
            this.term?.scrollToBottom();
            this._updateJumpBottomVisibility();
            if (!this.isTouchDevice()) {
                this.term?.focus();
            }
        });
        this._updateButtons(false);
        this._setStatus("Disconnected");
        restoreTranscript(this.transcript);
        window.addEventListener("resize", this._handleResize);
        if (window.visualViewport) {
            window.visualViewport.addEventListener("resize", this._scheduleResizeAfterLayout);
            window.visualViewport.addEventListener("scroll", this._scheduleResizeAfterLayout);
        }
        this._initMobileControls();
        this._initTerminalVoice();
        this._initTextInputPanel();
        initAgentControls({
            agentSelect: this.agentSelect,
            profileSelect: this.profileSelect,
            modelSelect: this.modelSelect,
            modelInput: this.modelInput,
            reasoningSelect: this.reasoningSelect,
        });
        if (this.agentSelect) {
            this.agentSelect.addEventListener("change", () => this._updateButtons(false));
        }
        subscribe("state:update", (state) => {
            if (state &&
                typeof state === "object" && state !== null && "terminal_idle_timeout_seconds" in state) {
                this.terminalIdleTimeoutSeconds = state.terminal_idle_timeout_seconds;
            }
        });
        if (this.terminalIdleTimeoutSeconds === null) {
            this._loadTerminalIdleTimeout().catch(() => { });
        }
        if (this._getSavedSessionId()) {
            this.connect({ mode: "attach" });
        }
    }
    fit() {
        if (this.fitAddon && this.term) {
            try {
                this.fitAddon.fit();
                this._handleResize();
            }
            catch (e) {
                // ignore
            }
        }
    }
    _setStatus(message) {
        this.statusBase = message;
        this._renderStatus();
    }
    _logTerminalDebug(message, details = null) {
        if (!this.terminalDebug)
            return;
        const prefix = "[terminal-debug]";
        if (details) {
            console.info(prefix, message, details);
        }
        else {
            console.info(prefix, message);
        }
    }
    _logBufferSnapshot(reason) {
        if (!this.terminalDebug || !this.term)
            return;
        const buffer = this.term.buffer?.active;
        this._logTerminalDebug("buffer snapshot", {
            reason,
            alt: isAltBufferActive(this.term),
            type: buffer && typeof buffer.type === "string" ? buffer.type : null,
            length: buffer ? buffer.length : null,
            baseY: buffer ? buffer.baseY : null,
            viewportY: buffer ? buffer.viewportY : null,
            cursorY: buffer ? buffer.cursorY : null,
            rows: this.term.rows,
            cols: this.term.cols,
            scrollback: typeof this.term.options?.scrollback === "number"
                ? this.term.options.scrollback
                : null,
        });
    }
    _resetTerminalDebugCounters() {
        this.replayChunkCount = 0;
        this.replayByteCount = 0;
        this.liveChunkCount = 0;
        this.liveByteCount = 0;
    }
    _renderStatus() {
        if (!this.statusEl)
            return;
        const sessionId = this.currentSessionId;
        const isConnected = this.statusBase === "Connected";
        this.statusEl.classList.toggle("connected", isConnected);
        if (!sessionId) {
            this.statusEl.textContent = this.statusBase;
            this.statusEl.title = "";
            return;
        }
        const shortId = sessionId.substring(0, 8);
        const repoLabel = this._getRepoLabel();
        const suffix = repoLabel ? ` ${shortId} · ${repoLabel}` : ` ${shortId}`;
        this.statusEl.textContent = `${this.statusBase}${suffix}`;
        this.statusEl.title = `Session: ${sessionId}`;
    }
    _getRepoLabel() {
        if (REPO_ID)
            return REPO_ID;
        if (BASE_PATH)
            return BASE_PATH;
        return "repo";
    }
    _getRepoStorageKey() {
        return REPO_ID || BASE_PATH || window.location.pathname || "default";
    }
    _getSavedSessionId() {
        return getSessionId(this._getRepoStorageKey(), this.terminalIdleTimeoutSeconds);
    }
    _setSavedSessionId(sessionId) {
        setSessionId(this._getRepoStorageKey(), sessionId);
    }
    _clearSavedSessionId() {
        clearSessionId(this._getRepoStorageKey());
    }
    _markSessionActive() {
        sessionMarkActive(this._getRepoStorageKey());
    }
    _setCurrentSessionId(sessionId) {
        this.currentSessionId = sessionId || null;
        if (this.currentSessionId) {
            migrateTextInputHookSession(CAR_CONTEXT_HOOK_ID, this.currentSessionId, this._getRepoStorageKey());
        }
        this._renderStatus();
    }
    async _loadTerminalIdleTimeout() {
        // State endpoint removed - terminal idle timeout no longer loaded from /api/state
    }
    _getFontSize() {
        return window.innerWidth < 640 ? 10 : 13;
    }
    _updateJumpBottomVisibility() {
        if (!this.jumpBottomBtn || !this.term)
            return;
        const buffer = this.term.buffer?.active;
        if (!buffer) {
            this.jumpBottomBtn.classList.add("hidden");
            return;
        }
        const atBottom = buffer.viewportY >= buffer.baseY;
        this.jumpBottomBtn.classList.toggle("hidden", atBottom);
        if (this.mobile.mobileViewActive) {
            this.mobile.mobileViewAtBottom = atBottom;
        }
    }
    _captureTerminalScrollState() {
        captureTerminalScrollState(this.mobile, this.term);
    }
    _restoreTerminalScrollState() {
        restoreTerminalScrollState(this.mobile, this.term, () => this._updateJumpBottomVisibility());
    }
    _scrollToBottomIfNearBottom() {
        scrollToBottomIfNearBottom(this.term, () => this._updateJumpBottomVisibility());
    }
    _resetTerminalDisplay() {
        if (!this.term)
            return;
        try {
            this.term.reset();
        }
        catch (_err) {
            try {
                this.term.clear();
            }
            catch (__err) {
                // ignore
            }
        }
    }
    _buildCarContextHook() {
        return {
            id: CAR_CONTEXT_HOOK_ID,
            apply: ({ text }) => {
                if (!text || !text.trim())
                    return null;
                if (hasTextInputHookFired(CAR_CONTEXT_HOOK_ID, this.currentSessionId, this._getRepoStorageKey()))
                    return null;
                if (looksLikeCommand(text))
                    return null;
                const lowered = text.toLowerCase();
                if (lowered.includes("about_car.md"))
                    return null;
                if (lowered.includes(".codex-autorunner"))
                    return null;
                if (text.includes(CONSTANTS.PROMPTS.CAR_CONTEXT_HINT) ||
                    text.includes(CAR_CONTEXT_HINT)) {
                    return null;
                }
                markTextInputHookFired(CAR_CONTEXT_HOOK_ID, this.currentSessionId, this._getRepoStorageKey());
                const injection = wrapInjectedContextIfNeeded(CAR_CONTEXT_HINT);
                const separator = text.endsWith("\n") ? "\n" : "\n\n";
                return { text: `${text}${separator}${injection}` };
            },
        };
    }
    _getTextInputDeps() {
        return {
            getSocket: () => this.socket,
            getTerm: () => this.term,
            isTouchDevice: () => this.isTouchDevice(),
            markSessionActive: () => this._markSessionActive(),
            getSavedSessionId: () => this._getSavedSessionId(),
            connect: (opts) => this.connect(opts),
            setMobileViewActive: (active) => this._setMobileViewActive(active),
            scheduleResizeAfterLayout: () => this._scheduleResizeAfterLayout(),
            captureTerminalScrollState: () => this._captureTerminalScrollState(),
            restoreTerminalScrollState: () => this._restoreTerminalScrollState(),
            scrollToBottomIfNearBottom: () => this._scrollToBottomIfNearBottom(),
            updateComposerSticky: () => this._updateComposerSticky(),
            updateJumpBottomVisibility: () => this._updateJumpBottomVisibility(),
            getRepoStorageKey: () => this._getRepoStorageKey(),
            getCurrentSessionId: () => this.currentSessionId,
            getRepoLabel: () => this._getRepoLabel(),
            logTerminalDebug: (msg, details) => this._logTerminalDebug(msg, details),
        };
    }
    _getMobileDeps() {
        return {
            getSocket: () => this.socket,
            getTerm: () => this.term,
            getTranscriptState: () => this.transcript,
            isTouchDevice: () => this.isTouchDevice(),
            markSessionActive: () => this._markSessionActive(),
            getTextInputEnabled: () => this.textInput.textInputEnabled,
            safeFocus: (el) => safeFocus(el),
            getTextInputTextareaEl: () => this.textInput.textInputTextareaEl,
            logTerminalDebug: (msg, details) => this._logTerminalDebug(msg, details),
        };
    }
    _setMobileViewActive(active) {
        setMobileViewActive(this.mobile, active, this.isTouchDevice(), this.term, this.transcript);
    }
    _scheduleMobileViewRender() {
        scheduleMobileViewRender(this.mobile, this.term, this.transcript, this.replayState.awaitingReplayEnd);
    }
    _updateComposerSticky() {
        updateComposerSticky(this.textInput, this.isTouchDevice());
    }
    _ensureTerminal() {
        const win = window;
        if (!win.Terminal || !win.FitAddon) {
            this._setStatus("xterm assets missing; reload or check /static/vendor");
            flash("xterm assets missing; reload the page", "error");
            return false;
        }
        if (this.term) {
            if (!this.inputDisposable) {
                this.inputDisposable = this.term.onData((data) => {
                    if (!this.socket || this.socket.readyState !== WebSocket.OPEN)
                        return;
                    this._markSessionActive();
                    this.socket.send(textEncoder.encode(data));
                });
            }
            return true;
        }
        const container = document.getElementById("terminal-container");
        if (!container)
            return false;
        this.term = new win.Terminal({
            convertEol: true,
            fontFamily: '"JetBrains Mono", "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace',
            fontSize: this._getFontSize(),
            scrollSensitivity: 1,
            fastScrollSensitivity: 5,
            cursorBlink: true,
            rows: 24,
            cols: 100,
            scrollback: this.transcript.maxLines,
            theme: CONSTANTS.THEME.XTERM,
        });
        this.fitAddon = new win.FitAddon.FitAddon();
        this.term.loadAddon(this.fitAddon);
        this.term.open(container);
        this.term.write('Press "New" or "Resume" to launch Codex TUI...\r\n');
        installWheelScroll(this.mobile, this.term, this.isTouchDevice());
        installTouchScroll(this.mobile, this.term, this.isTouchDevice());
        this.term.onScroll(() => this._updateJumpBottomVisibility());
        this.term.onRender(() => this._scheduleMobileViewRender());
        this._updateJumpBottomVisibility();
        try {
            this.fitAddon.fit();
        }
        catch (e) {
            // ignore fit errors when not visible
        }
        if (!this.inputDisposable) {
            this.inputDisposable = this.term.onData((data) => {
                if (!this.socket || this.socket.readyState !== WebSocket.OPEN)
                    return;
                this._markSessionActive();
                this.socket.send(textEncoder.encode(data));
            });
        }
        return true;
    }
    _teardownSocket() {
        this.reconnect.cancel();
        sessionTeardownSocket(this.socket, this.heartbeat);
        this.socket = null;
        resetReplayState(this.replayState);
        this.transcript.resetForConnect = false;
    }
    _noteSocketActivity() {
        this.heartbeat.noteActivity();
    }
    _startSocketHeartbeat() {
        if (this.socket) {
            this.heartbeat.start(this.socket);
        }
    }
    _stopSocketHeartbeat() {
        this.heartbeat.stop();
    }
    _updateButtons(connected) {
        if (this.connectBtn)
            this.connectBtn.disabled = connected;
        if (this.disconnectBtn)
            this.disconnectBtn.disabled = !connected;
        if (this.resumeBtn) {
            const selectedAgent = getSelectedAgent();
            const isOpencode = selectedAgent && selectedAgent.toLowerCase() === "opencode";
            if (isOpencode) {
                this.resumeBtn.classList.add("hidden");
            }
            else {
                this.resumeBtn.classList.remove("hidden");
                this.resumeBtn.disabled = connected;
            }
        }
        updateTextInputSendUi(this.textInput, this.socket, this.sessionNotFound);
        const voiceUnavailable = this.voice.voiceBtn?.classList.contains("disabled");
        if (this.voice.voiceBtn && !voiceUnavailable) {
            this.voice.voiceBtn.disabled = !connected;
            this.voice.voiceBtn.classList.toggle("voice-disconnected", !connected);
        }
        const mobileVoiceUnavailable = this.voice.mobileVoiceBtn?.classList.contains("disabled");
        if (this.voice.mobileVoiceBtn && !mobileVoiceUnavailable) {
            this.voice.mobileVoiceBtn.disabled = !connected;
            this.voice.mobileVoiceBtn.classList.toggle("voice-disconnected", !connected);
        }
        if (this.voice.voiceStatus && !voiceUnavailable && !connected) {
            this.voice.voiceStatus.textContent = "Connect to use voice";
            this.voice.voiceStatus.classList.remove("hidden");
        }
        else if (this.voice.voiceStatus &&
            !voiceUnavailable &&
            connected &&
            this.voice.voiceController &&
            this.voice.voiceStatus.textContent === "Connect to use voice") {
            this.voice.voiceStatus.textContent = "Hold to talk (Alt+V)";
            this.voice.voiceStatus.classList.remove("hidden");
        }
    }
    _handleResize() {
        if (!this.fitAddon || !this.term)
            return;
        const newFontSize = this._getFontSize();
        if (this.term.options.fontSize !== newFontSize) {
            this.term.options.fontSize = newFontSize;
        }
        if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
            try {
                this.fitAddon.fit();
            }
            catch (e) {
                // ignore fit errors when not visible
            }
            return;
        }
        this.fitAddon.fit();
        this.socket.send(JSON.stringify({
            type: "resize",
            cols: this.term.cols,
            rows: this.term.rows,
        }));
    }
    _scheduleResizeAfterLayout() {
        if (this.resizeRaf) {
            cancelAnimationFrame(this.resizeRaf);
            this.resizeRaf = null;
        }
        this.resizeRaf = requestAnimationFrame(() => {
            this.resizeRaf = requestAnimationFrame(() => {
                this.resizeRaf = null;
                updateViewportInsets(this.mobile, this.textInput.terminalSectionEl);
                this._handleResize();
                if (this.mobile.deferScrollRestore) {
                    this.mobile.deferScrollRestore = false;
                    this._restoreTerminalScrollState();
                }
            });
        });
    }
    scheduleResizeAfterLayout() {
        this._scheduleResizeAfterLayout();
    }
    _updateTextInputConnected(_connected) {
        if (this.textInput.textInputTextareaEl)
            this.textInput.textInputTextareaEl.disabled = false;
        updateTextInputSendUi(this.textInput, this.socket, this.sessionNotFound);
    }
    connect(options = {}) {
        const mode = (options.mode || options.resume ? "resume" : "new").toLowerCase();
        const isAttach = mode === "attach";
        const isResume = mode === "resume";
        const shouldAwaitReplay = isAttach || isResume;
        const quiet = Boolean(options.quiet);
        this.sessionNotFound = false;
        if (!this._ensureTerminal())
            return;
        if (this.socket && this.socket.readyState === WebSocket.OPEN)
            return;
        if (!quiet) {
            this.reconnect.attempts = 0;
        }
        this.reconnect.cancel();
        this._teardownSocket();
        this.intentionalDisconnect = false;
        this.lastConnectMode = mode;
        this.transcript.resetForConnect = false;
        initReplayForConnect(this.replayState, shouldAwaitReplay, false);
        this.transcript.resetForConnect = false;
        this._resetTerminalDebugCounters();
        this.lastAltBufferActive = null;
        this.lastAltScrollbackSize = 0;
        if (!isAttach && !isResume) {
            resetTranscript(this.transcript);
            this.transcript.resetForConnect = true;
            initReplayForConnect(this.replayState, shouldAwaitReplay, true);
        }
        const savedSessionId = this._getSavedSessionId();
        const selectedAgent = getSelectedAgent();
        const selectedProfile = getSelectedProfile(selectedAgent);
        const selectedModel = getSelectedModel(selectedAgent);
        const selectedReasoning = getSelectedReasoning(selectedAgent);
        this._logTerminalDebug("connect", {
            mode,
            shouldAwaitReplay,
            savedSessionId,
        });
        if (isAttach) {
            if (savedSessionId) {
                this._setCurrentSessionId(savedSessionId);
            }
            else {
                if (!quiet)
                    flash("No saved terminal session to attach to", "error");
                return;
            }
        }
        else {
            this._clearSavedSessionId();
            this._setCurrentSessionId(null);
        }
        const queryParams = buildConnectQuery({
            mode,
            terminalDebug: this.terminalDebug,
            isAttach,
            savedSessionId: isAttach ? savedSessionId : (savedSessionId || null),
            agent: !isAttach ? selectedAgent : null,
            profile: !isAttach ? selectedProfile : null,
            model: !isAttach ? selectedModel : null,
            reasoning: !isAttach ? selectedReasoning : null,
        });
        this.socket = createTerminalSocket(queryParams);
        this.socket.onopen = () => {
            this.reconnect.openedAt = Date.now();
            this._noteSocketActivity();
            this._startSocketHeartbeat();
            this.overlayEl?.classList.add("hidden");
            this._markSessionActive();
            this._logTerminalDebug("socket open", {
                mode,
                sessionId: this.currentSessionId,
            });
            if ((isAttach || isResume) && this.term) {
                this._resetTerminalDisplay();
                this.transcript.hydrated = false;
                hydrateTerminalFromTranscript(this.transcript, this.term);
                this._updateJumpBottomVisibility();
            }
            if (isAttach)
                this._setStatus("Connected (reattached)");
            else if (isResume)
                this._setStatus("Connected (codex resume)");
            else
                this._setStatus("Connected");
            this._updateButtons(true);
            updateTextInputSendUi(this.textInput, this.socket, this.sessionNotFound);
            this.fitAddon.fit();
            this._handleResize();
            if (isResume)
                this.term?.write("\r\nLaunching codex resume...\r\n");
            if (this.textInput.textInputPending) {
                sendPendingTextInputChunk(this.textInput, this.socket, () => this._markSessionActive());
            }
        };
        this.socket.onmessage = (event) => {
            this._noteSocketActivity();
            this._markSessionActive();
            if (typeof event.data === "string") {
                try {
                    const payload = JSON.parse(event.data);
                    if (payload.type === "hello") {
                        if (payload.session_id) {
                            this._setSavedSessionId(payload.session_id);
                            this._setCurrentSessionId(payload.session_id);
                        }
                        this._markSessionActive();
                        this._logTerminalDebug("hello", {
                            sessionId: payload.session_id || null,
                        });
                    }
                    else if (payload.type === "replay_end") {
                        this._handleReplayEnd();
                    }
                    else if (payload.type === "ack") {
                        handleTextInputAck(this.textInput, this.socket, () => this._markSessionActive(), payload);
                        updateTextInputSendUi(this.textInput, this.socket, this.sessionNotFound);
                    }
                    else if (payload.type === "exit") {
                        this.term?.write(`\r\n[session ended${payload.code !== null ? ` (code ${payload.code})` : ""}] \r\n`);
                        this._clearSavedSessionId();
                        this._setCurrentSessionId(null);
                        this.intentionalDisconnect = true;
                        this.disconnect();
                    }
                    else if (payload.type === "error") {
                        if (payload.message && payload.message.includes("Session not found")) {
                            this.sessionNotFound = true;
                            this._clearSavedSessionId();
                            this._setCurrentSessionId(null);
                            if (this.lastConnectMode === "attach") {
                                if (!this.suppressNextNotFoundFlash) {
                                    flash(payload.message || "Terminal error", "error");
                                }
                                this.suppressNextNotFoundFlash = false;
                                this.disconnect();
                                return;
                            }
                            updateTextInputSendUi(this.textInput, this.socket, this.sessionNotFound);
                            return;
                        }
                        flash(payload.message || "Terminal error", "error");
                    }
                }
                catch (err) {
                    // ignore bad payloads
                }
                return;
            }
            if (this.term) {
                const chunk = new Uint8Array(event.data);
                if (this.replayState.awaitingReplayEnd) {
                    this.replayChunkCount += 1;
                    this.replayByteCount += chunk.length;
                    bufferReplayChunk(this.replayState, chunk);
                    return;
                }
                const liveReset = consumeLiveReset(this.replayState);
                if (liveReset.shouldReset) {
                    resetTranscript(this.transcript);
                    this._resetTerminalDisplay();
                    if (liveReset.prelude) {
                        appendTranscriptChunk(this.transcript, liveReset.prelude);
                        this._scheduleMobileViewRender();
                        this.term.write(liveReset.prelude);
                    }
                    this._logTerminalDebug("first_live_reset", {
                        pendingPrelude: liveReset.hadPrelude,
                    });
                }
                this.liveChunkCount += 1;
                this.liveByteCount += chunk.length;
                appendTranscriptChunk(this.transcript, chunk);
                this._scheduleMobileViewRender();
                this.term.write(chunk);
            }
        };
        this.socket.onerror = () => {
            this._setStatus("Connection error");
        };
        this.socket.onclose = () => {
            this._stopSocketHeartbeat();
            this.reconnect.resetAttemptsIfStable();
            this._updateButtons(false);
            updateTextInputSendUi(this.textInput, this.socket, this.sessionNotFound);
            if (this.intentionalDisconnect) {
                this._setStatus("Disconnected");
                this.overlayEl?.classList.remove("hidden");
                return;
            }
            if (this.textInput.textInputPending) {
                flash("Send not confirmed; your text is preserved and will retry on reconnect", "info");
            }
            const savedId = this._getSavedSessionId();
            if (!savedId) {
                this._setStatus("Disconnected");
                this.overlayEl?.classList.remove("hidden");
                return;
            }
            const scheduled = this.reconnect.schedule(() => {
                this.suppressNextNotFoundFlash = true;
                this.connect({ mode: "attach", quiet: true });
            }, (status) => this._setStatus(status));
            if (!scheduled) {
                this.overlayEl?.classList.remove("hidden");
                flash("Terminal connection lost", "error");
            }
        };
    }
    _handleReplayEnd() {
        const flush = handleReplayEnd(this.replayState, this.transcript.resetForConnect, this.transcript.altScrollbackLines.length);
        if (!flush)
            return;
        this._logTerminalDebug("replay_end", {
            chunks: flush.chunks.length,
            bytes: this.replayByteCount,
            prelude: Boolean(flush.prelude),
            hasAltScreenEnter: flush.hasAltScreenEnter,
            shouldApplyPrelude: flush.shouldApplyPrelude,
            clearOnLive: !this.transcript.resetForConnect,
            altScrollback: this.transcript.altScrollbackLines.length,
        });
        if (flush.hasReplay && this.term) {
            resetTranscript(this.transcript);
            this._resetTerminalDisplay();
            if (flush.shouldApplyPrelude && flush.prelude) {
                appendTranscriptChunk(this.transcript, flush.prelude);
                this._scheduleMobileViewRender();
                this.term.write(flush.prelude);
            }
            for (const chunk of flush.chunks) {
                appendTranscriptChunk(this.transcript, chunk);
                this._scheduleMobileViewRender();
                this.term.write(chunk);
            }
            if (this.terminalDebug) {
                this.term.write("", () => {
                    this._logBufferSnapshot("replay_end_post");
                });
            }
        }
        else {
            this._logBufferSnapshot("replay_end_empty");
        }
    }
    disconnect() {
        this.intentionalDisconnect = true;
        this.reconnect.cancel();
        this._teardownSocket();
        this._setStatus("Disconnected");
        this.overlayEl?.classList.remove("hidden");
        this._updateButtons(false);
        if (this.voice.voiceKeyActive) {
            this.voice.voiceKeyActive = false;
            this.voice.voiceController?.stop();
        }
        this.voice.voiceController?.cleanup?.();
        this.voice.mobileVoiceController?.cleanup?.();
        this.voice.textVoiceController?.cleanup?.();
        if (this.inputDisposable) {
            this.inputDisposable.dispose();
            this.inputDisposable = null;
        }
    }
    _initTextInputPanel() {
        this.textInput.terminalSectionEl = document.getElementById("terminal");
        this.textInput.textInputToggleBtn = document.getElementById("terminal-text-input-toggle");
        this.textInput.textInputPanelEl = document.getElementById("terminal-text-input");
        this.textInput.textInputTextareaEl = document.getElementById("terminal-textarea");
        this.textInput.textInputSendBtn = document.getElementById("terminal-text-send");
        this.textInput.textInputImageBtn = document.getElementById("terminal-text-image");
        this.textInput.textInputImageInputEl = document.getElementById("terminal-text-image-input");
        if (!this.textInput.terminalSectionEl ||
            !this.textInput.textInputToggleBtn ||
            !this.textInput.textInputPanelEl ||
            !this.textInput.textInputTextareaEl ||
            !this.textInput.textInputSendBtn) {
            return;
        }
        this.textInput.textInputEnabled = readBoolFromStorage(TEXT_INPUT_STORAGE_KEYS.enabled, this.isTouchDevice());
        const deps = this._getTextInputDeps();
        this.textInput.textInputToggleBtn.addEventListener("click", () => {
            setTextInputEnabled(this.textInput, !this.textInput.textInputEnabled, this.isTouchDevice(), deps, { focus: true, focusTextarea: true });
        });
        const triggerSend = async () => {
            if (this.textInput.textInputSendBtn?.disabled) {
                flash("Connect the terminal first", "error");
                return;
            }
            const now = Date.now();
            if (now - this.mobile.lastSendTapAt < 300)
                return;
            this.mobile.lastSendTapAt = now;
            await sendFromTextarea(this.textInput, this.socket, deps, this);
        };
        this.textInput.textInputSendBtn.addEventListener("pointerup", (e) => {
            if (e.pointerType !== "touch")
                return;
            if (e.cancelable)
                e.preventDefault();
            this.mobile.suppressNextSendClick = true;
            triggerSend();
        });
        this.textInput.textInputSendBtn.addEventListener("touchend", (e) => {
            if (e.cancelable)
                e.preventDefault();
            this.mobile.suppressNextSendClick = true;
            triggerSend();
        });
        this.textInput.textInputSendBtn.addEventListener("click", () => {
            if (this.mobile.suppressNextSendClick) {
                this.mobile.suppressNextSendClick = false;
                return;
            }
            triggerSend();
        });
        this.textInput.textInputTextareaEl.addEventListener("input", () => {
            persistTextInputDraft(this.textInput);
            this._updateComposerSticky();
            captureTextInputSelection(this.textInput);
        });
        this.textInput.textInputTextareaEl.addEventListener("keydown", (e) => {
            if (e.key !== "Enter" || e.isComposing)
                return;
            const shouldSend = e.metaKey || e.ctrlKey;
            if (shouldSend) {
                e.preventDefault();
                triggerSend();
            }
            e.stopPropagation();
        });
        const captureSel = () => captureTextInputSelection(this.textInput);
        this.textInput.textInputTextareaEl.addEventListener("select", captureSel);
        this.textInput.textInputTextareaEl.addEventListener("keyup", captureSel);
        this.textInput.textInputTextareaEl.addEventListener("mouseup", captureSel);
        this.textInput.textInputTextareaEl.addEventListener("touchend", captureSel);
        if (this.textInput.textInputImageBtn && this.textInput.textInputImageInputEl) {
            this.textInput.textInputTextareaEl.addEventListener("paste", (e) => {
                const items = e.clipboardData?.items;
                if (!items || !items.length)
                    return;
                const files = [];
                for (const item of Array.from(items)) {
                    if (item.type && item.type.startsWith("image/")) {
                        const file = item.getAsFile();
                        if (file)
                            files.push(file);
                    }
                }
                if (!files.length)
                    return;
                e.preventDefault();
                handleImageFiles(this.textInput, files, deps);
            });
            this.textInput.textInputImageBtn.addEventListener("click", () => {
                captureTextInputSelection(this.textInput);
                this.textInput.textInputImageInputEl?.click();
            });
            this.textInput.textInputImageInputEl.addEventListener("change", () => {
                const files = Array.from(this.textInput.textInputImageInputEl?.files || []);
                if (!files.length)
                    return;
                handleImageFiles(this.textInput, files, deps);
                this.textInput.textInputImageInputEl.value = "";
            });
        }
        this.textInput.textInputTextareaEl.addEventListener("focus", () => {
            this.mobile.textInputWasFocused = true;
            this._updateComposerSticky();
            updateViewportInsets(this.mobile, this.textInput.terminalSectionEl);
            captureTextInputSelection(this.textInput);
            this._captureTerminalScrollState();
            this.mobile.deferScrollRestore = true;
            if (this.isTouchDevice() && isMobileViewport()) {
                this._scheduleResizeAfterLayout();
                this._setMobileViewActive(true);
                if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
                    const savedSessionId = this._getSavedSessionId();
                    if (savedSessionId) {
                        this.connect({ mode: "attach", quiet: true });
                    }
                    else {
                        this.connect({ mode: "new", quiet: true });
                    }
                }
            }
        });
        this.textInput.textInputTextareaEl.addEventListener("blur", () => {
            setTimeout(() => {
                if (document.activeElement !== this.textInput.textInputTextareaEl) {
                    this.mobile.textInputWasFocused = false;
                }
                this._updateComposerSticky();
                this._captureTerminalScrollState();
                this.mobile.deferScrollRestore = true;
                if (this.isTouchDevice() && isMobileViewport()) {
                    this._scheduleResizeAfterLayout();
                    this._setMobileViewActive(false);
                }
            }, 0);
        });
        if (this.textInput.textInputImageBtn && this.textInput.textInputImageInputEl) {
            this.textInput.terminalSectionEl.addEventListener("paste", (e) => {
                if (document.activeElement === this.textInput.textInputTextareaEl)
                    return;
                const items = e.clipboardData?.items;
                if (!items || !items.length)
                    return;
                const files = [];
                for (const item of Array.from(items)) {
                    if (item.type && item.type.startsWith("image/")) {
                        const file = item.getAsFile();
                        if (file)
                            files.push(file);
                    }
                }
                if (!files.length)
                    return;
                e.preventDefault();
                handleImageFiles(this.textInput, files, deps);
            });
        }
        this.textInput.textInputPending = loadPendingTextInput();
        restoreTextInputDraft(this.textInput);
        if (this.textInput.textInputPending && this.textInput.textInputTextareaEl && !this.textInput.textInputTextareaEl.value) {
            this.textInput.textInputTextareaEl.value = this.textInput.textInputPending.originalText || "";
        }
        setTextInputEnabled(this.textInput, this.textInput.textInputEnabled, this.isTouchDevice(), deps, { focus: false });
        updateViewportInsets(this.mobile, this.textInput.terminalSectionEl);
        this._updateComposerSticky();
        this._updateTextInputConnected(Boolean(this.socket && this.socket.readyState === WebSocket.OPEN));
        if (this.textInput.textInputPending) {
            const savedSessionId = this._getSavedSessionId();
            if (savedSessionId && (!this.socket || this.socket.readyState !== WebSocket.OPEN)) {
                this.connect({ mode: "attach", quiet: true });
            }
        }
    }
    _initMobileControls() {
        initMobileControls(this.mobile, this._getMobileDeps());
    }
    _initTerminalVoice() {
        initTerminalVoice(this.voice, this.socket, {
            getSocket: () => this.socket,
            getTerm: () => this.term,
            isTouchDevice: () => this.isTouchDevice(),
            markSessionActive: () => this._markSessionActive(),
            getTextInputState: () => this.textInput,
            getTextInputDeps: () => this._getTextInputDeps(),
            setTextInputEnabled: (st, en, td, dp, opts) => setTextInputEnabled(st, en, td, dp, opts),
            updateComposerSticky: () => this._updateComposerSticky(),
        });
    }
    _handleVoiceHotkeyDown(event) {
        handleVoiceHotkeyDown(this.voice, event, this.socket);
    }
    _handleVoiceHotkeyUp(event) {
        handleVoiceHotkeyUp(this.voice, event);
    }
}
