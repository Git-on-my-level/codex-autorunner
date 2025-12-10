import { api, flash, streamEvents } from "./utils.js";
import { publish, subscribe } from "./bus.js";
import { CONSTANTS } from "./constants.js";

const logRunIdInput = document.getElementById("log-run-id");
const logTailInput = document.getElementById("log-tail");
const toggleLogStreamButton = document.getElementById("toggle-log-stream");
const showTimestampToggle = document.getElementById("log-show-timestamp");
const showRunToggle = document.getElementById("log-show-run");
const jumpBottomButton = document.getElementById("log-jump-bottom");
let stopLogStream = null;
let lastKnownRunId = null;
let rawLogLines = [];
let autoScrollEnabled = true;
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
  fileModified: /^M\s+[\w.\/]/,

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

function formatLogLines(lines) {
  const cleaned = [];
  let context = {
    inPromptBlock: false,
    promptBlockType: null,
    inDiffBlock: false,
  };

  for (const raw of lines) {
    if (shouldOmitLine(raw)) continue;
    const processed = processLine(raw).trimEnd();
    const classification = classifyLine(raw, context);

    // Track diff context
    if (classification.startDiff) {
      context.inDiffBlock = true;
    }
    if (classification.resetDiff) {
      context.inDiffBlock = false;
    }
    // Blank lines or non-diff content after several lines ends diff context
    const isBlankLine = processed.trim() === "";
    if (isBlankLine && context.inDiffBlock) {
      // Keep diff context for now, but consecutive blanks will end it
    }

    // Track prompt block context
    if (classification.type === "prompt-marker") {
      context.inPromptBlock = true;
      context.inDiffBlock = false; // Reset diff in prompt blocks
      const match = processed.match(/<(\w+)>/);
      context.promptBlockType = match ? match[1] : null;
    } else if (classification.type === "prompt-marker-end") {
      context.inPromptBlock = false;
      context.promptBlockType = null;
    }

    const isRunBoundary = /^=== run \d+/.test(processed);
    if (
      isRunBoundary &&
      cleaned.length &&
      cleaned[cleaned.length - 1].text !== ""
    ) {
      cleaned.push({ text: "", type: "blank", priority: 10 });
    }

    const isBlank = processed.trim() === "";
    if (isBlank) {
      if (cleaned.length && cleaned[cleaned.length - 1].text === "") continue;
      cleaned.push({ text: "", type: "blank", priority: 10 });
      continue;
    }

    cleaned.push({
      text: processed,
      type: classification.type,
      priority: classification.priority,
      raw: raw,
    });
  }
  return cleaned;
}

function appendLogLine(line) {
  const output = document.getElementById("log-output");
  if (output.dataset.isPlaceholder === "true") {
    output.innerHTML = "";
    delete output.dataset.isPlaceholder;
    rawLogLines = [];
  }

  rawLogLines.push(line);
  if (rawLogLines.length > CONSTANTS.UI.MAX_LOG_LINES_IN_DOM) {
    rawLogLines.shift();
  }

  const processed = processLine(line).trimEnd();
  if (shouldOmitLine(line)) {
    publish("logs:line", line);
    return;
  }

  const classification = classifyLine(line, {});
  const div = document.createElement("div");
  div.textContent = processed;
  div.className = `log-line log-${classification.type}`;
  div.dataset.logType = classification.type;
  output.appendChild(div);

  if (output.childElementCount > CONSTANTS.UI.MAX_LOG_LINES_IN_DOM) {
    output.firstElementChild.remove();
  }

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
      delete output.dataset.isPlaceholder;
      renderLogs();
    } else {
      output.textContent = "(empty log)";
      output.dataset.isPlaceholder = "true";
      rawLogLines = [];
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
  const output = document.getElementById("log-output");

  if (rawLogLines.length === 0) {
    output.innerHTML = "";
    output.textContent = "(empty log)";
    output.dataset.isPlaceholder = "true";
    return;
  }

  // Full re-render with classification
  const lines = formatLogLines(rawLogLines);

  if (lines.length > 0) {
    delete output.dataset.isPlaceholder;
    output.innerHTML = "";
    const fragment = document.createDocumentFragment();

    let promptBlockDetails = null;
    let promptBlockContent = null;
    let promptBlockType = null;
    let promptLineCount = 0;

    lines.forEach((lineData, idx) => {
      // Handle collapsible prompt context blocks
      if (lineData.type === "prompt-marker") {
        // Start a new collapsible block
        const match = lineData.text.match(/<(\w+)>/);
        promptBlockType = match ? match[1] : "CONTEXT";
        promptBlockDetails = document.createElement("details");
        promptBlockDetails.className = "log-context-block";
        const summary = document.createElement("summary");
        summary.className = "log-context-summary";
        summary.innerHTML = `<span class="log-context-icon">▶</span> ${promptBlockType} <span class="log-context-count"></span>`;
        promptBlockDetails.appendChild(summary);
        promptBlockContent = document.createElement("div");
        promptBlockContent.className = "log-context-content";
        promptBlockDetails.appendChild(promptBlockContent);
        promptLineCount = 0;
        fragment.appendChild(promptBlockDetails);
        return;
      }

      if (lineData.type === "prompt-marker-end") {
        // Close the block and update count
        if (promptBlockDetails) {
          const countEl =
            promptBlockDetails.querySelector(".log-context-count");
          if (countEl) {
            countEl.textContent = `(${promptLineCount} lines)`;
          }
        }
        promptBlockDetails = null;
        promptBlockContent = null;
        promptBlockType = null;
        promptLineCount = 0;
        return;
      }

      // If we're inside a prompt block, add to it
      if (
        promptBlockContent &&
        (lineData.type === "prompt-context" || lineData.type === "output")
      ) {
        const div = document.createElement("div");
        div.textContent = lineData.text;
        div.className = "log-line log-prompt-context";
        promptBlockContent.appendChild(div);
        promptLineCount++;
        return;
      }

      const div = document.createElement("div");
      div.textContent = lineData.text;

      if (lineData.type === "blank") {
        div.className = "log-line log-blank";
      } else {
        div.className = `log-line log-${lineData.type}`;
        div.dataset.logType = lineData.type;
        div.dataset.priority = lineData.priority;
      }

      // Add icons/prefixes for certain types
      if (lineData.type === "thinking-label" || lineData.type === "thinking") {
        div.dataset.icon = "💭";
      } else if (
        lineData.type === "exec-label" ||
        lineData.type === "exec-command"
      ) {
        div.dataset.icon = "⚡";
      } else if (
        lineData.type === "file-update-label" ||
        lineData.type === "file-modified"
      ) {
        div.dataset.icon = "📝";
      } else if (lineData.type === "agent-output") {
        div.dataset.icon = "✨";
      } else if (lineData.type === "run-start" || lineData.type === "run-end") {
        div.dataset.icon = "🔄";
      } else if (lineData.type === "success") {
        div.dataset.icon = "✓";
      } else if (lineData.type === "tokens") {
        div.dataset.icon = "📊";
      }

      fragment.appendChild(div);
    });

    output.appendChild(fragment);
  } else {
    output.innerHTML = "";
    output.textContent = "(empty log)";
    output.dataset.isPlaceholder = "true";
  }
  scrollLogsToBottom();
}

export function initLogs() {
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
      autoScrollEnabled = true;
      scrollLogsToBottom(true);
      jumpBottomButton.classList.add("hidden");
    });
  }

  // Track scroll position to show/hide jump button
  const output = document.getElementById("log-output");
  if (output) {
    output.addEventListener("scroll", updateJumpButtonVisibility);
  }

  loadLogs();
}
