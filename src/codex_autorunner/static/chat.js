import { streamEvents, flash } from "./utils.js";
import { subscribe } from "./bus.js";

const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const chatThread = document.getElementById("chat-thread");
const chatHint = document.getElementById("chat-hint");
const clearChatBtn = document.getElementById("clear-chat");
let stopChatStream = null;

function clearChat() {
  if (stopChatStream) {
    stopChatStream();
    stopChatStream = null;
  }
  chatThread.innerHTML = `
    <div class="chat-empty">
      <div class="chat-empty-icon">💬</div>
      <p>No messages yet</p>
      <p class="small">Start a conversation with Codex</p>
    </div>
  `;
  chatHint.textContent = "Responses stream live from Codex";
  flash("Chat cleared");
}

function formatMessageContent(text) {
  const escapeHtml = (str) =>
    str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  let formatted = escapeHtml(text);

  formatted = formatted.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const langLabel = lang ? `<span class="code-lang">${lang}</span>` : "";
    return `<div class="code-block">${langLabel}<pre><code>${code.trim()}</code></pre></div>`;
  });

  formatted = formatted.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');

  formatted = formatted.replace(
    /\*\*thinking\s*([\s\S]*?)\*\*/gi,
    '<div class="thinking-block"><span class="thinking-label">💭 Thinking</span>$1</div>'
  );

  formatted = formatted.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

  formatted = formatted.replace(
    /exec\s+(\/\S+)\s+'([^']+)'/g,
    '<div class="exec-block"><span class="exec-label">⚡ exec</span><code>$1 \'$2\'</code></div>'
  );

  formatted = formatted.replace(/\n/g, "<br>");

  return formatted;
}

function appendMessage(role, text) {
  const emptyState = chatThread.querySelector(".chat-empty");
  if (emptyState) emptyState.remove();

  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  bubble.innerHTML = formatMessageContent(text);
  chatThread.appendChild(bubble);
  chatThread.scrollTop = chatThread.scrollHeight;
}

async function sendChat(message) {
  const includeTodo = document.getElementById("include-todo").checked;
  const includeProgress = document.getElementById("include-progress").checked;
  const includeOpinions = document.getElementById("include-opinions").checked;
  const button = chatForm.querySelector("button[type=submit]");
  button.disabled = true;
  appendMessage("user", message);

  const botBubble = document.createElement("div");
  botBubble.className = "bubble codex";
  chatThread.appendChild(botBubble);
  chatThread.scrollTop = chatThread.scrollHeight;
  chatHint.textContent = "Streaming response...";

  if (stopChatStream) {
    stopChatStream();
    stopChatStream = null;
  }

  stopChatStream = streamEvents("/api/chat/stream", {
    method: "POST",
    body: {
      message,
      include_todo: includeTodo,
      include_progress: includeProgress,
      include_opinions: includeOpinions,
    },
    onMessage: (data, event) => {
      if (event === "done") {
        if (data && data !== "0") {
          flash(`Chat finished with exit ${data}`);
        }
        button.disabled = false;
        chatHint.textContent = "Responses stream live from Codex.";
        if (stopChatStream) {
          stopChatStream();
          stopChatStream = null;
        }
        return;
      }
      if (event === "error") {
        botBubble.innerHTML = `<span class="error-text">Error: ${data.replace(/</g, "&lt;").replace(/>/g, "&gt;")}</span>`;
        button.disabled = false;
        chatHint.textContent = "Responses stream live from Codex.";
        if (stopChatStream) {
          stopChatStream();
          stopChatStream = null;
        }
        return;
      }
      
      const currentText = botBubble.dataset.rawText || "";
      const newText = currentText ? `${currentText}\n${data}` : data;
      botBubble.dataset.rawText = newText;
      
      // Throttle DOM updates using requestAnimationFrame
      if (!botBubble.dataset.renderPending) {
        botBubble.dataset.renderPending = "true";
        requestAnimationFrame(() => {
          botBubble.innerHTML = formatMessageContent(botBubble.dataset.rawText);
          chatThread.scrollTop = chatThread.scrollHeight;
          delete botBubble.dataset.renderPending;
        });
      }
    },
    onError: (err) => {
      const safeMsg = err.message.replace(/</g, "&lt;").replace(/>/g, "&gt;");
      botBubble.innerHTML = `<span class="error-text">Error: ${safeMsg}</span>`;
      button.disabled = false;
      chatHint.textContent = "Responses stream live from Codex.";
      stopChatStream = null;
    },
    onFinish: () => {
      stopChatStream = null;
    },
  });
}

export function initChat() {
  clearChatBtn.addEventListener("click", clearChat);
  chatForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const message = chatInput.value.trim();
    if (!message) return;
    chatInput.value = "";
    await sendChat(message);
  });

  subscribe("tab:change", (tab) => {
    if (tab !== "chat" && stopChatStream) {
      stopChatStream();
      stopChatStream = null;
      chatHint.textContent = "Responses stream live from Codex.";
    }
  });
}
