import { api, flash, streamEvents, getUrlParams, updateUrlParams } from "./utils.js";
import { publish, subscribe } from "./bus.js";
import { saveToCache, loadFromCache } from "./cache.js";
import { CONSTANTS } from "./constants.js";

const logRunIdInput = document.getElementById("log-run-id") as HTMLInputElement | null;
const logTailInput = document.getElementById("log-tail") as HTMLInputElement | null;
const toggleLogStreamButton = document.getElementById("toggle-log-stream") as HTMLButtonElement | null;
const showTimestampToggle = document.getElementById("log-show-timestamp") as HTMLInputElement | null;
const showRunToggle = document.getElementById("log-show-run") as HTMLInputElement | null;
const showSummaryToggle = document.getElementById("log-show-summary") as HTMLInputElement | null;
const jumpBottomButton = document.getElementById("log-jump-bottom") as HTMLButtonElement | null;
const loadOlderButton = document.getElementById("log-load-older") as HTMLButtonElement | null;

let stopLogStream: (() => void) | null = null;
let lastKnownRunId: number | null = null;
let rawLogLines: string[] = [];
let autoScrollEnabled = true;
let renderedStartIndex = 0;
let renderedEndIndex = 0;
let isViewingTail = true;
let renderState: RenderState | null = null;
let logContexts: LogContextState[] = [];
let logContextState: LogContextState = { inPromptBlock: false, inDiffBlock: false };

const DOC_CHAT_META_RE = /doc-chat id=[a-f0-9]+ (result=|exit_code=)/i;

const LINE_PATTERNS = {
  runStart: /^=== run \d+ start/,
  runEnd: /^=== run \d+ end/,
  thinking: /^thinking$/i,
  thinkingContent: /^\*\*.+\*\*$/,
  thinkingMultiline:
    /^I'm (preparing|planning|considering|reviewing|analyzing|checking|looking|reading|searching)/i,
  execStart: /^exec$/i,
  execCommand: /^\/bin\/(zsh|bash|sh)\s+-[a-z]+\s+['"]?.+in\s+\//i,
  applyPatch: /^apply_patch\(/i,
  fileUpdate: /^file update:?$/i,
  fileModified: /^M\s+[\w./]/,
  diffGitHeader: /^diff --git /,
  diffFileHeader: /^(---|\+\+\+)\s+[ab]\//,
  diffIndex: /^index [a-f0-9]+\.\.[a-f0-9]+/,
  diffHunk: /^@@\s+-\d+,?\d*\s+\+\d+,?\d*\s+@@/,
  promptMarker:
    /^<(SPEC|WORK_DOCS|TODO|PROGRESS|OPINIONS|TARGET_DOC|RECENT_RUN|SYSTEM|USER|ASSISTANT)>$/,
  promptMarkerEnd:
    /^<\/(SPEC|WORK_DOCS|TODO|PROGRESS|OPINIONS|TARGET_DOC|RECENT_RUN|SYSTEM|USER|ASSISTANT)>$/,
  mcpStartup: /^mcp startup:/i,
  tokensUsed: /^tokens used/i,
  agentOutput: /^Agent:\s*/i,
  success: /succeeded in \d+ms/i,
  exitCode: /exited \d+ in \d+ms/i,
  testOutput:
    /^(={3,}\s*(test session|.*passed|.*failed)|PASSED|FAILED|ERROR)/i,
  pythonTraceback: /^(Traceback \(most recent|File ".*", line \d+|.*Error:)/i,
  markdownList: /^- (\[[ x]\]\s)?[A-Z]/,
};

interface LineClassification {
  type: string;
  priority: number;
  startDiff?: boolean;
  resetDiff?: boolean;
}

function classifyLine(line: string, context: LogContextState = { inPromptBlock: false, inDiffBlock: false }): LineClassification {
  const stripped = line
    .replace(/^\[[^\]]*]\s*/, "")
    .replace(/^(run=\d+\s*)?(stdout|stderr):\s*/, "")
    .replace(/^doc-chat id=[a-f0-9]+ stdout:\s*/i, "")
    .trim();

  if (LINE_PATTERNS.runStart.test(stripped))
    return { type: "run-start", priority: 1, resetDiff: true };
  if (LINE_PATTERNS.runEnd.test(stripped))
    return { type: "run-end", priority: 1, resetDiff: true };

  if (LINE_PATTERNS.agentOutput.test(stripped))
    return { type: "agent-output", priority: 1 };

  if (LINE_PATTERNS.thinking.test(stripped))
    return { type: "thinking-label", priority: 2 };
  if (LINE_PATTERNS.thinkingContent.test(stripped))
    return { type: "thinking", priority: 2 };
  if (LINE_PATTERNS.thinkingMultiline.test(stripped))
    return { type: "thinking", priority: 2 };

  if (LINE_PATTERNS.execStart.test(stripped))
    return { type: "exec-label", priority: 3 };
  if (LINE_PATTERNS.execCommand.test(stripped))
    return { type: "exec-command", priority: 3 };
  if (LINE_PATTERNS.applyPatch.test(stripped))
    return { type: "exec-command", priority: 3 };
  if (LINE_PATTERNS.fileUpdate.test(stripped))
    return { type: "file-update-label", priority: 3, startDiff: true };
  if (LINE_PATTERNS.fileModified.test(stripped))
    return { type: "file-modified", priority: 3 };

  if (LINE_PATTERNS.testOutput.test(stripped))
    return { type: "test-output", priority: 3 };

  if (LINE_PATTERNS.pythonTraceback.test(stripped))
    return { type: "error-output", priority: 2 };

  if (LINE_PATTERNS.diffGitHeader.test(stripped))
    return { type: "diff-header", priority: 4, startDiff: true };
  if (LINE_PATTERNS.diffFileHeader.test(stripped))
    return { type: "diff-header", priority: 4 };
  if (LINE_PATTERNS.diffIndex.test(stripped))
    return { type: "diff-header", priority: 4 };
  if (LINE_PATTERNS.diffHunk.test(stripped))
    return { type: "diff-hunk", priority: 4 };

  if (context.inDiffBlock) {
    if (/^\+[^+]/.test(stripped) && !LINE_PATTERNS.markdownList.test(stripped))
      return { type: "diff-add", priority: 4 };
    if (/^-[^-]/.test(stripped) && !LINE_PATTERNS.markdownList.test(stripped))
      return { type: "diff-del", priority: 4 };
  }

  if (LINE_PATTERNS.promptMarker.test(stripped))
    return { type: "prompt-marker", priority: 5 };
  if (LINE_PATTERNS.promptMarkerEnd.test(stripped))
    return { type: "prompt-marker-end", priority: 5 };

  if (LINE_PATTERNS.mcpStartup.test(stripped))
    return { type: "system", priority: 6 };
  if (LINE_PATTERNS.tokensUsed.test(stripped))
    return { type: "tokens", priority: 6 };

  if (LINE_PATTERNS.success.test(stripped))
    return { type: "success", priority: 3 };
  if (LINE_PATTERNS.exitCode.test(stripped))
    return { type: "exit-code", priority: 3 };

  if (context.inPromptBlock) return { type: "prompt-context", priority: 5 };

  return { type: "output", priority: 4 };
}

function isSummaryMode(): boolean {
  return !showSummaryToggle || showSummaryToggle.checked;
}

function processLine(line: string): string {
  let next = line;
  next = next.replace(/^=== run (\d+)\s+chat(\s|$)/, "=== run $1$2");

  if (showTimestampToggle && !showTimestampToggle.checked) {
    next = next.replace(/^\[[^\]]*]\s*/, "");
  }
  if (showRunToggle && !showRunToggle.checked) {
    if (next.startsWith("[")) {
      next = next.replace(/^(\[[^\]]+]\s*)run=\d+\s*/, "$1");
    } else {
      next = next.replace(/^run=\d+\s*/, "");
    }
  }
  next = next.replace(/^(\[[^\]]+]\s*)?(run=\d+\s*)?chat:\s*/, "$1$2");
  next = next.replace(
    /^(\[[^\]]+]\s*)?(run=\d+\s*)?(stdout|stderr):\s*/,
    "$1$2"
  );
  next = next.replace(
    /^(\[[^\]]+]\s*)?(run=\d+\s*)?doc-chat id=[a-f0-9]+ stdout:\s*/i,
    "$1$2"
  );
  return next.trimEnd();
}

function shouldOmitLine(line: string): boolean {
  if (showRunToggle && !showRunToggle.checked && DOC_CHAT_META_RE.test(line)) {
    return true;
  }
  return false;
}

interface RenderState {
  inPromptBlock: boolean;
  promptBlockDetails: HTMLDetailsElement | null;
  promptBlockContent: HTMLDivElement | null;
  promptBlockType: string | null;
  promptLineCount: number;
  inDiffBlock: boolean;
}

function resetRenderState(): void {
  renderState = {
    inPromptBlock: false,
    promptBlockDetails: null,
    promptBlockContent: null,
    promptBlockType: null,
    promptLineCount: 0,
    inDiffBlock: false,
  };
}

interface LogContextState {
  inPromptBlock: boolean;
  inDiffBlock: boolean;
}

function resetLogContexts(): void {
  logContexts = [];
  logContextState = { inPromptBlock: false, inDiffBlock: false };
}

function updateLogContextForLine(line: string): void {
  logContexts.push({ ...logContextState });
  const classification = classifyLine(line, logContextState);
  if (classification.startDiff) {
    logContextState.inDiffBlock = true;
  }
  if (classification.resetDiff) {
    logContextState.inDiffBlock = false;
  }
  if (classification.type === "prompt-marker") {
    logContextState.inPromptBlock = true;
    logContextState.inDiffBlock = false;
  } else if (classification.type === "prompt-marker-end") {
    logContextState.inPromptBlock = false;
  }
}

function rebuildLogContexts(): void {
  resetLogContexts();
  rawLogLines.forEach((line) => updateLogContextForLine(line));
}

function finalizePromptBlock(): void {
  if (!renderState || !renderState.promptBlockDetails) return;
  const countEl = renderState.promptBlockDetails.querySelector(
    ".log-context-count"
  );
  if (countEl) {
    countEl.textContent = `(${renderState.promptLineCount} lines)`;
  }
}

function startPromptBlock(output: HTMLElement, label: string): void {
  if (!renderState) return;
  renderState.promptBlockType = label;
  renderState.promptBlockDetails = document.createElement("details");
  renderState.promptBlockDetails.className = "log-context-block";
  const summary = document.createElement("summary");
  summary.className = "log-context-summary";
  summary.innerHTML = `<span class="log-context-icon">▶</span> ${label} <span class="log-context-count"></span>`;
  renderState.promptBlockDetails.appendChild(summary);
  renderState.promptBlockContent = document.createElement("div");
  renderState.promptBlockContent.className = "log-context-content";
  renderState.promptBlockDetails.appendChild(renderState.promptBlockContent);
  renderState.promptLineCount = 0;
  output.appendChild(renderState.promptBlockDetails);
}

function appendRawLine(line: string, output: HTMLElement): void {
  const isBlank = line.trim() === "";
  const div = document.createElement("div");
  div.textContent = line;
  div.className = isBlank ? "log-line log-blank" : "log-line log-raw";
  output.appendChild(div);
}

function appendRenderedLine(line: string, output: HTMLElement): void {
  if (!renderState) resetRenderState();
  if (shouldOmitLine(line)) return;

  const processed = processLine(line).trimEnd();
  const classification = classifyLine(line, renderState);

  if (classification.startDiff) {
    renderState.inDiffBlock = true;
  }
  if (classification.resetDiff) {
    renderState.inDiffBlock = false;
  }

  if (classification.type === "prompt-marker") {
    renderState.inPromptBlock = true;
    renderState.inDiffBlock = false;
    const match = processed.match(/<(\w+)>/);
    const blockLabel = match ? match[1] : "CONTEXT";
    startPromptBlock(output, blockLabel);
    return;
  }

  if (classification.type === "prompt-marker-end") {
    finalizePromptBlock();
    if (renderState) {
      renderState.promptBlockDetails = null;
      renderState.promptBlockContent = null;
      renderState.promptBlockType = null;
      renderState.promptLineCount = 0;
      renderState.inPromptBlock = false;
    }
    return;
  }

  if (
    renderState &&
    renderState.promptBlockContent &&
    renderState.inPromptBlock &&
    (classification.type === "prompt-context" || classification.type === "output")
  ) {
    const div = document.createElement("div");
    div.textContent = processed;
    div.className = "log-line log-prompt-context";
    renderState.promptBlockContent.appendChild(div);
    renderState.promptLineCount++;
    return;
  }

  const isBlank = processed.trim() === "";
  const div = document.createElement("div");
  div.textContent = processed;

  if (isBlank) {
    div.className = "log-line log-blank";
  } else {
    div.className = `log-line log-${classification.type}`;
    div.dataset.logType = classification.type;
    div.dataset.priority = String(classification.priority);
  }

  if (classification.type === "thinking-label" || classification.type === "thinking") {
    div.dataset.icon = "💭";
  } else if (classification.type === "exec-label" || classification.type === "exec-command") {
    div.dataset.icon = "⚡";
  } else if (
    classification.type === "file-update-label" ||
    classification.type === "file-modified"
  ) {
    div.dataset.icon = "📝";
  } else if (classification.type === "agent-output") {
    div.dataset.icon = "✨";
  } else if (classification.type === "run-start" || classification.type === "run-end") {
    div.dataset.icon = "🔄";
  } else if (classification.type === "success") {
    div.dataset.icon = "✓";
  } else if (classification.type === "tokens") {
    div.dataset.icon = "📊";
  }

  output.appendChild(div);
}

function trimLogBuffer(): void {
  const maxLines = CONSTANTS.UI.MAX_LOG_LINES_IN_MEMORY;
  if (!maxLines || rawLogLines.length <= maxLines) return;
  const overflow = rawLogLines.length - maxLines;
  rawLogLines = rawLogLines.slice(overflow);
  if (logContexts.length > overflow) {
    logContexts = logContexts.slice(overflow);
  } else {
    logContexts = [];
  }
  renderedStartIndex = Math.max(0, renderedStartIndex - overflow);
  renderedEndIndex = Math.max(0, renderedEndIndex - overflow);
}

function updateLoadOlderButton(): void {
  if (!loadOlderButton) return;
  if (renderedStartIndex > 0) {
    loadOlderButton.classList.remove("hidden");
  } else {
    loadOlderButton.classList.add("hidden");
  }
}

function applyLogUrlState(): void {
  const params = getUrlParams();
  const runId = params.get("run");
  const tail = params.get("tail");
  const summary = params.get("summary");
  if (runId !== null && logRunIdInput) {
    logRunIdInput.value = runId;
  }
  if (tail !== null && logTailInput) {
    logTailInput.value = tail;
  }
  if (summary !== null && showSummaryToggle) {
    showSummaryToggle.checked = !(
      summary === "0" || summary.toLowerCase() === "false"
    );
  }
  if (runId) {
    isViewingTail = false;
  }
}

function syncLogUrlState(): void {
  const runId = logRunIdInput?.value?.trim() || "";
  const tail = logTailInput?.value?.trim() || "";
  updateUrlParams({
    run: runId || null,
    tail: runId ? null : tail || null,
    summary: showSummaryToggle?.checked ? null : "0",
  });
}

interface RenderLogWindowOptions {
  startIndex?: number | null;
  followTail?: boolean;
}

function renderLogWindow({ startIndex = null, followTail = true }: RenderLogWindowOptions = {}): void {
  const output = document.getElementById("log-output");
  if (!output) return;

  if (rawLogLines.length === 0) {
    output.innerHTML = "";
    output.textContent = "(empty log)";
    output.dataset.isPlaceholder = "true";
    renderedStartIndex = 0;
    renderedEndIndex = 0;
    isViewingTail = true;
    updateLoadOlderButton();
    return;
  }

  const endIndex = rawLogLines.length;
  let windowStart = startIndex;
  if (followTail || windowStart === null) {
    windowStart = Math.max(0, endIndex - CONSTANTS.UI.MAX_LOG_LINES_IN_DOM);
  }
  const windowEnd = Math.min(
    endIndex,
    windowStart + CONSTANTS.UI.MAX_LOG_LINES_IN_DOM
  );

  if (!isSummaryMode()) {
    output.innerHTML = "";
    delete output.dataset.isPlaceholder;
    for (let i = windowStart; i < windowEnd; i += 1) {
      appendRawLine(rawLogLines[i], output);
    }
    renderedStartIndex = windowStart;
    renderedEndIndex = windowEnd;
    isViewingTail = followTail && windowEnd === endIndex;
    updateLoadOlderButton();
    if (isViewingTail) {
      scrollLogsToBottom(true);
    }
    return;
  }

  output.innerHTML = "";
  delete output.dataset.isPlaceholder;
  resetRenderState();
  const startContext = logContexts[windowStart];
  if (startContext && renderState) {
    renderState.inPromptBlock = startContext.inPromptBlock;
    renderState.inDiffBlock = startContext.inDiffBlock;
    if (renderState.inPromptBlock) {
      startPromptBlock(output, "CONTEXT (continued)");
    }
  }

  for (let i = windowStart; i < windowEnd; i += 1) {
    appendRenderedLine(rawLogLines[i], output);
  }
  finalizePromptBlock();

  renderedStartIndex = windowStart;
  renderedEndIndex = windowEnd;
  isViewingTail = followTail && windowEnd === endIndex;
  updateLoadOlderButton();

  if (isViewingTail) {
    scrollLogsToBottom(true);
  }
}

function appendLogLine(line: string): void {
  const output = document.getElementById("log-output");
  if (!output) return;
  if (output.dataset.isPlaceholder === "true") {
    output.innerHTML = "";
    delete output.dataset.isPlaceholder;
    rawLogLines = [];
    resetRenderState();
    resetLogContexts();
    renderedStartIndex = 0;
    renderedEndIndex = 0;
    isViewingTail = true;
  }

  rawLogLines.push(line);
  updateLogContextForLine(line);
  trimLogBuffer();

  if (!isViewingTail) {
    publish("logs:line", line);
    updateLoadOlderButton();
    return;
  }

  if (!isSummaryMode()) {
    appendRawLine(line, output);
  } else {
    appendRenderedLine(line, output);
  }
  renderedEndIndex = rawLogLines.length;
  if (output.childElementCount > CONSTANTS.UI.MAX_LOG_LINES_IN_DOM) {
    output.firstElementChild?.remove();
  }
  renderedStartIndex = Math.max(
    0,
    renderedEndIndex - output.childElementCount
  );
  updateLoadOlderButton();
  publish("logs:line", line);
  scrollLogsToBottom();
}

function scrollLogsToBottom(force = false): void {
  const output = document.getElementById("log-output");
  if (!output) return;
  if (!autoScrollEnabled && !force) return;

  requestAnimationFrame(() => {
    output.scrollTop = output.scrollHeight;
  });
}

function updateJumpButtonVisibility(): void {
  const output = document.getElementById("log-output");
  if (!output || !jumpBottomButton) return;

  const isNearBottom =
    output.scrollHeight - output.scrollTop - output.clientHeight < 100;

  if (isNearBottom) {
    jumpBottomButton.classList.add("hidden");
    autoScrollEnabled = true;
  } else {
    jumpBottomButton.classList.remove("hidden");
    autoScrollEnabled = false;
  }
}

function setLogStreamButton(active: boolean): void {
  if (toggleLogStreamButton) {
    toggleLogStreamButton.textContent = active ? "Stop stream" : "Start stream";
  }
}

async function loadLogs(): Promise<void> {
  syncLogUrlState();
  const runId = logRunIdInput?.value || "";
  const tail = logTailInput?.value || "200";
  const params = new URLSearchParams();
  if (runId) {
    params.set("run_id", runId);
  } else if (tail) {
    params.set("tail", tail);
  }
  const path = params.toString()
    ? `/api/logs?${params.toString()}`
    : "/api/logs";
  try {
    const data = await api(path);
    const text = typeof data === "string" ? data : (data as { log?: string }).log || "";
    const output = document.getElementById("log-output");
    if (!output) return;

    if (text) {
      rawLogLines = text.split("\n");
      trimLogBuffer();
      rebuildLogContexts();
      delete output.dataset.isPlaceholder;
      isViewingTail = true;
      renderLogs();

      if (!runId) {
        const lines = rawLogLines.slice(-200);
        saveToCache("logs:tail", lines.join("\n"));
      }
    } else {
      output.textContent = "(empty log)";
      output.dataset.isPlaceholder = "true";
      rawLogLines = [];
      resetRenderState();
      resetLogContexts();
      renderedStartIndex = 0;
      renderedEndIndex = 0;
      isViewingTail = true;
      updateLoadOlderButton();
      if (!runId) {
        saveToCache("logs:tail", "");
      }
    }

    flash("Logs loaded", "success");
    publish("logs:loaded", { runId, tail, text });
  } catch (err) {
    flash((err as Error).message || "Failed to load logs", "error");
  }
}

function stopLogStreaming(): void {
  if (stopLogStream) {
    stopLogStream();
    stopLogStream = null;
  }
  setLogStreamButton(false);
  publish("logs:streaming", false);
}

function startLogStreaming(): void {
  if (stopLogStream) return;
  const output = document.getElementById("log-output");
  if (!output) return;
  output.textContent = "(listening...)";
  output.dataset.isPlaceholder = "true";
  rawLogLines = [];
  resetRenderState();
  resetLogContexts();
  renderedStartIndex = 0;
  renderedEndIndex = 0;
  isViewingTail = true;
  updateLoadOlderButton();

  stopLogStream = streamEvents("/api/logs/stream", {
    onMessage: (data) => {
      appendLogLine(data || "");
    },
    onError: (err) => {
      flash(err.message || "Stream error", "error");
      stopLogStreaming();
    },
    onFinish: () => {
      stopLogStream = null;
      setLogStreamButton(false);
      publish("logs:streaming", false);
    },
  });
  setLogStreamButton(true);
  publish("logs:streaming", true);
  flash("Streaming logs…", "info");
}

function syncRunIdPlaceholder(state: { last_run_id?: number } | null): void {
  lastKnownRunId = state?.last_run_id ?? null;
  if (logRunIdInput) {
    logRunIdInput.placeholder = lastKnownRunId
      ? `latest (${lastKnownRunId})`
      : "latest";
  }
}

function renderLogs(): void {
  renderLogWindow({ followTail: isViewingTail });
}

export function initLogs(): void {
  applyLogUrlState();
  const loadLogsButton = document.getElementById("load-logs");
  if (loadLogsButton) {
    loadLogsButton.addEventListener("click", loadLogs);
  }
  if (toggleLogStreamButton) {
    toggleLogStreamButton.addEventListener("click", () => {
      if (stopLogStream) {
        stopLogStreaming();
      } else {
        startLogStreaming();
      }
    });
  }

  subscribe("state:update", syncRunIdPlaceholder);
  subscribe("tab:change", (tab) => {
    if (tab !== "logs" && stopLogStream) {
      stopLogStreaming();
    }
  });

  if (showTimestampToggle) {
    showTimestampToggle.addEventListener("change", renderLogs);
  }
  if (showRunToggle) {
    showRunToggle.addEventListener("change", renderLogs);
  }
  if (showSummaryToggle) {
    showSummaryToggle.addEventListener("change", () => {
      syncLogUrlState();
      renderLogs();
    });
  }

  if (jumpBottomButton) {
    jumpBottomButton.addEventListener("click", () => {
      if (!isViewingTail) {
        isViewingTail = true;
        renderLogs();
      }
      autoScrollEnabled = true;
      scrollLogsToBottom(true);
      jumpBottomButton.classList.add("hidden");
    });
  }

  if (loadOlderButton) {
    loadOlderButton.addEventListener("click", () => {
      if (renderedStartIndex <= 0) return;
      const nextStart = Math.max(
        0,
        renderedStartIndex - CONSTANTS.UI.LOG_PAGE_SIZE
      );
      isViewingTail = false;
      autoScrollEnabled = false;
      renderLogWindow({ startIndex: nextStart, followTail: false });
    });
  }

  const output = document.getElementById("log-output");
  if (output) {
    output.addEventListener("scroll", updateJumpButtonVisibility);
  }

  const cachedLogs = loadFromCache("logs:tail");
  if (cachedLogs) {
    const output = document.getElementById("log-output");
    if (output) {
      rawLogLines = (cachedLogs as string).split("\n");
      if (rawLogLines.length > 0) {
        trimLogBuffer();
        rebuildLogContexts();
        delete output.dataset.isPlaceholder;
        isViewingTail = true;
        renderLogs();
        scrollLogsToBottom(true);
      }
    }
  }

  loadLogs();
}
