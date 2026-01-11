import { api, flash, streamEvents, getUrlParams, updateUrlParams } from "./utils.js";
import { publish, subscribe } from "./bus.js";
import { saveToCache, loadFromCache } from "./cache.js";
import { CONSTANTS } from "./constants.js";

const logRunIdInput = document.getElementById("log-run-id");
const logTailInput = document.getElementById("log-tail");
const toggleLogStreamButton = document.getElementById("toggle-log-stream");
const showTimestampToggle = document.getElementById("log-show-timestamp");
const showRunToggle = document.getElementById("log-show-run");
const jumpBottomButton = document.getElementById("log-jump-bottom");
const loadOlderButton = document.getElementById("log-load-older");
let stopLogStream = null;
let lastKnownRunId = null;
let rawLogLines = [];
let autoScrollEnabled = true;
let renderedStartIndex = 0;
let renderedEndIndex = 0;
let isViewingTail = true;
let renderState = null;
let logContexts = [];
let logContextState = { inPromptBlock: false, inDiffBlock: false };
// Matches doc-chat metadata lines (start/result) that we might want to hide for cleaner view
const DOC_CHAT_META_RE = /doc-chat id=[a-f0-9]+ (result=|exit_code=)/i;

// Log line classification patterns
const LINE_PATTERNS = {
  // Run boundaries
  runStart: /^=== run \d+ start/,
  runEnd: /^=== run \d+ end/,

  // Agent thinking/reasoning
  thinking: /^thinking$/i,
  thinkingContent: /^\*\*.+\*\*$/,
  thinkingMultiline:
    /^I'm (preparing|planning|considering|reviewing|analyzing|checking|looking|reading|searching)/i,

  // Tool execution
  execStart: /^exec$/i,
  execCommand: /^\/bin\/(zsh|bash|sh)\s+-[a-z]+\s+['"]?.+in\s+\//i,
  applyPatch: /^apply_patch\(/i,
  fileUpdate: /^file update:?$/i,
  fileModified: /^M\s+[\w./]/,

  // Diff patterns - need context tracking to avoid false positives
  // These patterns identify the START of a diff block
  diffGitHeader: /^diff --git /,
  diffFileHeader: /^(---|\+\+\+)\s+[ab]\//,
  diffIndex: /^index [a-f0-9]+\.\.[a-f0-9]+/,
  diffHunk: /^@@\s+-\d+,?\d*\s+\+\d+,?\d*\s+@@/,

  // Prompt/context markers (verbose)
  promptMarker:
    /^<(SPEC|WORK_DOCS|TODO|PROGRESS|OPINIONS|TARGET_DOC|RECENT_RUN|SYSTEM|USER|ASSISTANT)>$/,
  promptMarkerEnd:
    /^<\/(SPEC|WORK_DOCS|TODO|PROGRESS|OPINIONS|TARGET_DOC|RECENT_RUN|SYSTEM|USER|ASSISTANT)>$/,

  // System messages
  mcpStartup: /^mcp startup:/i,
  tokensUsed: /^tokens used/i,

  // Agent summary/output (lines after tokens used)
  agentOutput: /^Agent:\s*/i,

  // Success/error indicators
  success: /succeeded in \d+ms/i,
  exitCode: /exited \d+ in \d+ms/i,

  // Additional patterns for better classification
  testOutput:
    /^(={3,}\s*(test session|.*passed|.*failed)|PASSED|FAILED|ERROR)/i,
  pythonTraceback: /^(Traceback \(most recent|File ".*", line \d+|.*Error:)/i,

  // Markdown list items - explicitly NOT diff lines
  markdownList: /^- (\[[ x]\]\s)?[A-Z]/,
};

// Determine the type of a log line
function classifyLine(line, context = {}) {
  const stripped = line
    .replace(/^\[[^\]]*]\s*/, "")
    .replace(/^(run=\d+\s*)?(stdout|stderr):\s*/, "")
    .replace(/^doc-chat id=[a-f0-9]+ stdout:\s*/i, "")
    .trim();

  // Run boundaries - highest priority (also resets diff context)
  if (LINE_PATTERNS.runStart.test(stripped))
    return { type: "run-start", priority: 1, resetDiff: true };
  if (LINE_PATTERNS.runEnd.test(stripped))
    return { type: "run-end", priority: 1, resetDiff: true };

  // Agent output (summary) - also high priority as this is final output for user
  if (LINE_PATTERNS.agentOutput.test(stripped))
    return { type: "agent-output", priority: 1 };

  // Thinking/reasoning
  if (LINE_PATTERNS.thinking.test(stripped))
    return { type: "thinking-label", priority: 2 };
  if (LINE_PATTERNS.thinkingContent.test(stripped))
    return { type: "thinking", priority: 2 };
  if (LINE_PATTERNS.thinkingMultiline.test(stripped))
    return { type: "thinking", priority: 2 };

  // Tool execution
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

  // Test output
  if (LINE_PATTERNS.testOutput.test(stripped))
    return { type: "test-output", priority: 3 };

  // Error/traceback
  if (LINE_PATTERNS.pythonTraceback.test(stripped))
    return { type: "error-output", priority: 2 };

  // Diff headers - mark start of diff context
  if (LINE_PATTERNS.diffGitHeader.test(stripped))
    return { type: "diff-header", priority: 4, startDiff: true };
  if (LINE_PATTERNS.diffFileHeader.test(stripped))
    return { type: "diff-header", priority: 4 };
  if (LINE_PATTERNS.diffIndex.test(stripped))
    return { type: "diff-header", priority: 4 };
  if (LINE_PATTERNS.diffHunk.test(stripped))
    return { type: "diff-hunk", priority: 4 };

  // Diff add/del lines - ONLY if we're in diff context
  if (context.inDiffBlock) {
    // Check for actual diff lines (not markdown lists)
    if (/^\+[^+]/.test(stripped) && !LINE_PATTERNS.markdownList.test(stripped))
      return { type: "diff-add", priority: 4 };
    if (/^-[^-]/.test(stripped) && !LINE_PATTERNS.markdownList.test(stripped))
      return { type: "diff-del", priority: 4 };
  }

  // Prompt/context (verbose - collapsible)
  if (LINE_PATTERNS.promptMarker.test(stripped))
    return { type: "prompt-marker", priority: 5 };
  if (LINE_PATTERNS.promptMarkerEnd.test(stripped))
    return { type: "prompt-marker-end", priority: 5 };

  // System messages
  if (LINE_PATTERNS.mcpStartup.test(stripped))
    return { type: "system", priority: 6 };
  if (LINE_PATTERNS.tokensUsed.test(stripped))
    return { type: "tokens", priority: 6 };

  // Success/error in command output
  if (LINE_PATTERNS.success.test(stripped))
    return { type: "success", priority: 3 };
  if (LINE_PATTERNS.exitCode.test(stripped))
    return { type: "exit-code", priority: 3 };

  // If we're in a context block, mark as context
  if (context.inPromptBlock) return { type: "prompt-context", priority: 5 };

  // Default: regular output
  return { type: "output", priority: 4 };
}

function processLine(line) {
  let next = line;
  // Normalize run markers that include "chat"
  next = next.replace(/^=== run (\d+)\s+chat(\s|$)/, "=== run $1$2");

  if (!showTimestampToggle.checked) {
    next = next.replace(/^\[[^\]]*]\s*/, "");
  }
  if (!showRunToggle.checked) {
    if (next.startsWith("[")) {
      next = next.replace(/^(\[[^\]]+]\s*)run=\d+\s*/, "$1");
    } else {
      next = next.replace(/^run=\d+\s*/, "");
    }
  }
  // Remove redundant channel prefix
  next = next.replace(/^(\[[^\]]+]\s*)?(run=\d+\s*)?chat:\s*/, "$1$2");
  // Strip stdout/stderr markers that make logs noisy
  next = next.replace(
    /^(\[[^\]]+]\s*)?(run=\d+\s*)?(stdout|stderr):\s*/,
    "$1$2"
  );
  // Strip doc-chat id prefix for cleaner display
  next = next.replace(
    /^(\[[^\]]+]\s*)?(run=\d+\s*)?doc-chat id=[a-f0-9]+ stdout:\s*/i,
    "$1$2"
  );
  return next.trimEnd();
}

function shouldOmitLine(line) {
  // Only omit doc-chat metadata lines (result=, exit_code=) when Run toggle is off
  // We still want to show the actual content from doc-chat
  if (!showRunToggle.checked && DOC_CHAT_META_RE.test(line)) {
    return true;
  }
  return false;
}

function resetRenderState() {
  renderState = {
    inPromptBlock: false,
    promptBlockDetails: null,
    promptBlockContent: null,
    promptBlockType: null,
    promptLineCount: 0,
    inDiffBlock: false,
  };
}

function resetLogContexts() {
  logContexts = [];
  logContextState = { inPromptBlock: false, inDiffBlock: false };
}

function updateLogContextForLine(line) {
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

function rebuildLogContexts() {
  resetLogContexts();
  rawLogLines.forEach((line) => updateLogContextForLine(line));
}

function finalizePromptBlock() {
  if (!renderState || !renderState.promptBlockDetails) return;
  const countEl = renderState.promptBlockDetails.querySelector(
    ".log-context-count"
  );
  if (countEl) {
    countEl.textContent = `(${renderState.promptLineCount} lines)`;
  }
}

function startPromptBlock(output, label) {
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

function appendRenderedLine(line, output) {
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
    renderState.promptBlockDetails = null;
    renderState.promptBlockContent = null;
    renderState.promptBlockType = null;
    renderState.promptLineCount = 0;
    renderState.inPromptBlock = false;
    return;
  }

  if (
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
    div.dataset.priority = classification.priority;
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

function trimLogBuffer() {
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

function updateLoadOlderButton() {
  if (!loadOlderButton) return;
  if (renderedStartIndex > 0) {
    loadOlderButton.classList.remove("hidden");
  } else {
    loadOlderButton.classList.add("hidden");
  }
}

function applyLogUrlState() {
  const params = getUrlParams();
  const runId = params.get("run");
  const tail = params.get("tail");
  if (runId !== null && logRunIdInput) {
    logRunIdInput.value = runId;
  }
  if (tail !== null && logTailInput) {
    logTailInput.value = tail;
  }
  if (runId) {
    isViewingTail = false;
  }
}

function syncLogUrlState() {
  const runId = logRunIdInput?.value?.trim() || "";
  const tail = logTailInput?.value?.trim() || "";
  updateUrlParams({
    run: runId || null,
    tail: runId ? null : tail || null,
  });
}

function renderLogWindow({ startIndex = null, followTail = true } = {}) {
  const output = document.getElementById("log-output");

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

  output.innerHTML = "";
  delete output.dataset.isPlaceholder;
  resetRenderState();
  const startContext = logContexts[windowStart];
  if (startContext) {
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

function appendLogLine(line) {
  const output = document.getElementById("log-output");
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

  appendRenderedLine(line, output);
  renderedEndIndex = rawLogLines.length;
  if (output.childElementCount > CONSTANTS.UI.MAX_LOG_LINES_IN_DOM) {
    output.firstElementChild.remove();
  }
  renderedStartIndex = Math.max(
    0,
    renderedEndIndex - output.childElementCount
  );
  updateLoadOlderButton();
  publish("logs:line", line);
  scrollLogsToBottom();
}

function scrollLogsToBottom(force = false) {
  const output = document.getElementById("log-output");
  if (!output) return;
  if (!autoScrollEnabled && !force) return;

  requestAnimationFrame(() => {
    output.scrollTop = output.scrollHeight;
  });
}

function updateJumpButtonVisibility() {
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

function setLogStreamButton(active) {
  toggleLogStreamButton.textContent = active ? "Stop stream" : "Start stream";
}

async function loadLogs() {
  syncLogUrlState();
  const runId = logRunIdInput.value;
  const tail = logTailInput.value || "200";
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
    const text = typeof data === "string" ? data : data.log || "";
    const output = document.getElementById("log-output");

    if (text) {
      rawLogLines = text.split("\n");
      trimLogBuffer();
      rebuildLogContexts();
      delete output.dataset.isPlaceholder;
      isViewingTail = true;
      renderLogs();

      // Update cache if we are looking at the latest logs (no specific run ID)
      if (!runId) {
        // Limit to last 200 lines to avoid localStorage quota issues
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

    flash("Logs loaded");
    publish("logs:loaded", { runId, tail, text });
  } catch (err) {
    flash(err.message);
  }
}

function stopLogStreaming() {
  if (stopLogStream) {
    stopLogStream();
    stopLogStream = null;
  }
  setLogStreamButton(false);
  publish("logs:streaming", false);
}

function startLogStreaming() {
  if (stopLogStream) return;
  const output = document.getElementById("log-output");
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
      flash(err.message);
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
  flash("Streaming logs…");
}

function syncRunIdPlaceholder(state) {
  lastKnownRunId = state?.last_run_id ?? null;
  logRunIdInput.placeholder = lastKnownRunId
    ? `latest (${lastKnownRunId})`
    : "latest";
}

function renderLogs() {
  renderLogWindow({ followTail: isViewingTail });
}

export function initLogs() {
  applyLogUrlState();
  document.getElementById("load-logs").addEventListener("click", loadLogs);
  toggleLogStreamButton.addEventListener("click", () => {
    if (stopLogStream) {
      stopLogStreaming();
    } else {
      startLogStreaming();
    }
  });

  subscribe("state:update", syncRunIdPlaceholder);
  subscribe("tab:change", (tab) => {
    if (tab !== "logs" && stopLogStream) {
      stopLogStreaming();
    }
  });

  showTimestampToggle.addEventListener("change", renderLogs);
  showRunToggle.addEventListener("change", renderLogs);

  // Jump to bottom button
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

  // Track scroll position to show/hide jump button
  const output = document.getElementById("log-output");
  if (output) {
    output.addEventListener("scroll", updateJumpButtonVisibility);
  }

  // Try loading from cache first
  const cachedLogs = loadFromCache("logs:tail");
  if (cachedLogs) {
    const output = document.getElementById("log-output");
    rawLogLines = cachedLogs.split("\n");
    if (rawLogLines.length > 0) {
      trimLogBuffer();
      rebuildLogContexts();
      delete output.dataset.isPlaceholder;
      isViewingTail = true;
      renderLogs();
      scrollLogsToBottom(true);
    }
  }

  loadLogs();
}
