import { CONSTANTS } from "./constants.js";
import { registerAutoRefresh, triggerRefresh } from "./autoRefresh.js";
import {
  chatUI,
  docActionsUI,
  docButtons,
  snapshotUI,
  specIngestUI,
  specIssueUI,
  threadRegistryUI,
} from "./docsElements.js";
import {
  getActiveDoc,
  getChatState,
  setActiveDoc,
  setHistoryNavIndex,
  getHistoryNavIndex,
  type ChatHistoryEntry,
  type DocType,
} from "./docsState.js";
import { autoResizeTextarea, getDocTextarea, updateDocControls } from "./docsUi.js";
import { renderChat } from "./docChatRender.js";
import {
  applyPatch,
  cancelDocChat,
  discardPatch,
  refreshAllDrafts,
  reloadPatch,
  sendDocChat,
  startNewDocChatThread,
  toggleDraftPreview,
} from "./docChatActions.js";
import { copyDocToClipboard, pasteSpecFromClipboard } from "./docsClipboard.js";
import {
  clearDocs,
  getDocFromUrl,
  loadDocs,
  safeLoadDocs,
  saveDoc,
  setDoc,
} from "./docsCrud.js";
import {
  applySpecIngestPatch,
  cancelSpecIngest,
  continueSpecIngest,
  discardSpecIngestPatch,
  ingestSpec,
  reloadSpecIngestPatch,
} from "./docsSpecIngest.js";
import { initDocVoice } from "./docsVoice.js";
import { loadSnapshot, runSnapshot } from "./docsSnapshot.js";
import { initAgentControls } from "./agentControls.js";
import {
  downloadThreadRegistryBackup,
  loadThreadRegistryStatus,
  resetThreadRegistry,
} from "./docsThreadRegistry.js";
import { publish, subscribe } from "./bus.js";

export function initDocs(): void {
  if (!chatUI.send || !chatUI.input) {
    console.warn("Doc chat UI elements missing; skipping doc chat init.");
  }
  const urlDoc = getDocFromUrl();
  if (urlDoc) {
    setActiveDoc(urlDoc);
  }
  docButtons.forEach((btn) =>
    btn.addEventListener("click", () => {
      setDoc(btn.dataset.doc as DocType | null);
    })
  );
  const saveDocBtn = document.getElementById("save-doc") as HTMLButtonElement | null;
  if (saveDocBtn) {
    saveDocBtn.addEventListener("click", saveDoc);
  }
  const reloadDocBtn = document.getElementById("reload-doc") as HTMLButtonElement | null;
  if (reloadDocBtn) {
    reloadDocBtn.addEventListener("click", () => {
      if (getActiveDoc() === "snapshot") {
        loadSnapshot({ notify: true });
      } else {
        loadDocs();
      }
    });
  }
  const ingestSpecBtn = document.getElementById("ingest-spec") as HTMLButtonElement | null;
  if (ingestSpecBtn) {
    ingestSpecBtn.addEventListener("click", ingestSpec);
  }
  const clearDocsBtn = document.getElementById("clear-docs") as HTMLButtonElement | null;
  if (clearDocsBtn) {
    clearDocsBtn.addEventListener("click", clearDocs);
  }
  if (specIngestUI.continueBtn) {
    specIngestUI.continueBtn.addEventListener("click", continueSpecIngest);
  }
  if (specIngestUI.cancelBtn) {
    specIngestUI.cancelBtn.addEventListener("click", cancelSpecIngest);
  }
  if (specIngestUI.input) {
    specIngestUI.input.addEventListener("input", () => {
      autoResizeTextarea(specIngestUI.input);
    });
    specIngestUI.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        continueSpecIngest();
      }
    });
  }
  if (specIngestUI.patchApply)
    specIngestUI.patchApply.addEventListener("click", applySpecIngestPatch);
  if (specIngestUI.patchDiscard)
    specIngestUI.patchDiscard.addEventListener("click", discardSpecIngestPatch);
  if (specIngestUI.patchReload)
    specIngestUI.patchReload.addEventListener("click", () =>
      reloadSpecIngestPatch(false)
    );
  if (docActionsUI.copy) {
    docActionsUI.copy.addEventListener("click", () =>
      copyDocToClipboard()
    );
  }
  if (docActionsUI.paste) {
    docActionsUI.paste.addEventListener("click", pasteSpecFromClipboard);
  }
  if (threadRegistryUI.reset) {
    threadRegistryUI.reset.addEventListener("click", resetThreadRegistry);
  }
  if (threadRegistryUI.download) {
    threadRegistryUI.download.addEventListener(
      "click",
      downloadThreadRegistryBackup
    );
  }
  const docContent = getDocTextarea();
  if (docContent) {
    docContent.addEventListener("input", () => {
      if (getActiveDoc() !== "snapshot") {
        updateDocControls();
      }
    });
    docContent.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing && !e.shiftKey && getActiveDoc() === "todo") {
        const text = docContent.value;
        const pos = docContent.selectionStart;
        const lineStart = text.lastIndexOf("\n", pos - 1) + 1;
        const lineEnd = text.indexOf("\n", pos);
        const currentLine = text.slice(lineStart, lineEnd === -1 ? text.length : lineEnd);
        const match = currentLine.match(/^(\s*)- \[(x|X| )?\]/);
        if (match) {
          e.preventDefault();
          const indent = match[1];
          const newLine = "\n" + indent + "- [ ] ";
          const endOfCurrentLine = lineEnd === -1 ? text.length : lineEnd;
          const newValue = text.slice(0, endOfCurrentLine) + newLine + text.slice(endOfCurrentLine);
          docContent.value = newValue;
          const newPos = endOfCurrentLine + newLine.length;
          docContent.setSelectionRange(newPos, newPos);
          updateDocControls();
        }
      }
    });
  }
  let suppressNextSendClick = false;
  let lastSendTapAt = 0;
  const triggerSend = () => {
    const now = Date.now();
    if (now - lastSendTapAt < 300) return;
    lastSendTapAt = now;
    sendDocChat();
  };
  if (chatUI.send) {
    chatUI.send.addEventListener("pointerup", (e) => {
      if (e.pointerType !== "touch") return;
      if (e.cancelable) e.preventDefault();
      suppressNextSendClick = true;
      triggerSend();
    });
    chatUI.send.addEventListener("click", () => {
      if (suppressNextSendClick) {
        suppressNextSendClick = false;
        return;
      }
      triggerSend();
    });
  }
  if (chatUI.cancel) {
    chatUI.cancel.addEventListener("click", cancelDocChat);
  }
  if (chatUI.newThread) {
    chatUI.newThread.addEventListener("click", startNewDocChatThread);
  }
  if (chatUI.eventsToggle) {
    chatUI.eventsToggle.addEventListener("click", () => {
      const state = getChatState();
      state.eventsExpanded = !state.eventsExpanded;
      renderChat();
    });
  }
  if (chatUI.patchApply)
    chatUI.patchApply.addEventListener("click", () => applyPatch());
  if (chatUI.patchDiscard)
    chatUI.patchDiscard.addEventListener("click", () => discardPatch());
  if (chatUI.patchReload)
    chatUI.patchReload.addEventListener("click", () => reloadPatch());
  if (chatUI.patchPreview)
    chatUI.patchPreview.addEventListener("click", () => toggleDraftPreview());
  if (specIssueUI.toggle) {
    specIssueUI.toggle.addEventListener("click", () => {
      if (specIssueUI.inputRow) {
        const isHidden = specIssueUI.inputRow.classList.toggle("hidden");
        if (!isHidden && specIssueUI.input) {
          specIssueUI.input.focus();
        }
        specIssueUI.toggle.textContent = isHidden
          ? "Import Issue → SPEC"
          : "Cancel";
      }
    });
  }

  if (snapshotUI.generate) {
    snapshotUI.generate.addEventListener("click", () => runSnapshot());
  }
  if (snapshotUI.update) {
    snapshotUI.update.addEventListener("click", () => runSnapshot());
  }
  if (snapshotUI.regenerate) {
    snapshotUI.regenerate.addEventListener("click", () => runSnapshot());
  }
  if (snapshotUI.copy) {
    snapshotUI.copy.addEventListener("click", () => copyDocToClipboard("snapshot"));
  }
  if (snapshotUI.refresh) {
    snapshotUI.refresh.addEventListener("click", () => loadSnapshot({ notify: true }));
  }

  initDocVoice();
  initAgentControls({
    agentSelect: chatUI.agentSelect,
    modelSelect: chatUI.modelSelect,
    reasoningSelect: chatUI.reasoningSelect,
  });
  loadThreadRegistryStatus();
  refreshAllDrafts();
  reloadSpecIngestPatch(true);

  if (chatUI.input) {
    chatUI.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) {
        const shouldSend = e.metaKey || e.ctrlKey;
        if (shouldSend) {
          e.preventDefault();
          sendDocChat();
        }
        e.stopPropagation();
        return;
      }

      if (e.key === "ArrowUp") {
        const state = getChatState();
        const isEmpty = chatUI.input.value.trim() === "";
        const atStart = chatUI.input.selectionStart === 0;
        if ((isEmpty || atStart) && state.history.length > 0) {
          e.preventDefault();
          const maxIndex = state.history.length - 1;
          if (getHistoryNavIndex() < maxIndex) {
            setHistoryNavIndex(getHistoryNavIndex() + 1);
            const entry = state.history[getHistoryNavIndex()] as ChatHistoryEntry | undefined;
            chatUI.input.value = entry?.prompt || "";
            autoResizeTextarea(chatUI.input);
            chatUI.input.setSelectionRange(
              chatUI.input.value.length,
              chatUI.input.value.length
            );
          }
        }
        return;
      }

      if (e.key === "ArrowDown") {
        const state = getChatState();
        const atEnd = chatUI.input.selectionStart === chatUI.input.value.length;
        if (getHistoryNavIndex() >= 0 && atEnd) {
          e.preventDefault();
          setHistoryNavIndex(getHistoryNavIndex() - 1);
          if (getHistoryNavIndex() >= 0) {
            const entry = state.history[getHistoryNavIndex()] as ChatHistoryEntry | undefined;
            chatUI.input.value = entry?.prompt || "";
          } else {
            chatUI.input.value = "";
          }
          autoResizeTextarea(chatUI.input);
          chatUI.input.setSelectionRange(
            chatUI.input.value.length,
            chatUI.input.value.length
          );
        }
        return;
      }
    });
  }

  if (chatUI.input) {
    chatUI.input.addEventListener("input", () => {
      const state = getChatState();
      if (state.error) {
        state.error = "";
        renderChat();
      }
      setHistoryNavIndex(-1);
      autoResizeTextarea(chatUI.input);
    });
  }

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") {
      const docsTab = document.getElementById("docs");
      if (docsTab && !docsTab.classList.contains("hidden")) {
        e.preventDefault();
        saveDoc();
      }
    }
  });

  loadDocs();
  loadSnapshot({ notify: false }).catch(() => {});
  renderChat();
  document.body.dataset.docsReady = "true";
  publish("docs:ready", undefined);

  registerAutoRefresh("docs-content", {
    callback: safeLoadDocs,
    tabId: "docs",
    interval: CONSTANTS.UI.AUTO_REFRESH_INTERVAL,
    refreshOnActivation: true,
    immediate: false,
  });

  let docsInvalidateTimer: ReturnType<typeof setTimeout> | null = null;
  const scheduleDocsRefresh = () => {
    if (docsInvalidateTimer) {
      clearTimeout(docsInvalidateTimer);
    }
    docsInvalidateTimer = setTimeout(() => {
      triggerRefresh("docs-content");
    }, 500);
  };
  subscribe("todo:invalidate", scheduleDocsRefresh);
  subscribe("runs:invalidate", scheduleDocsRefresh);
}
