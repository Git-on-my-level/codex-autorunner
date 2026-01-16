import { CONSTANTS } from "./constants.js";
import { registerAutoRefresh } from "./autoRefresh.js";
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
  importIssueToSpec,
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
import {
  downloadThreadRegistryBackup,
  loadThreadRegistryStatus,
  resetThreadRegistry,
} from "./docsThreadRegistry.js";
import { publish } from "./bus.js";

export function initDocs() {
  if (!chatUI.send || !chatUI.input) {
    console.warn("Doc chat UI elements missing; skipping doc chat init.");
  }
  const urlDoc = getDocFromUrl();
  if (urlDoc) {
    setActiveDoc(urlDoc);
  }
  docButtons.forEach((btn) =>
    btn.addEventListener("click", () => {
      setDoc(btn.dataset.doc);
    })
  );
  document.getElementById("save-doc").addEventListener("click", saveDoc);
  document.getElementById("reload-doc").addEventListener("click", () => {
    if (getActiveDoc() === "snapshot") {
      loadSnapshot({ notify: true });
    } else {
      loadDocs();
    }
  });
  document.getElementById("ingest-spec").addEventListener("click", ingestSpec);
  document.getElementById("clear-docs").addEventListener("click", clearDocs);
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
  if (specIssueUI.button) {
    specIssueUI.button.addEventListener("click", () => {
      if (getActiveDoc() !== "spec") setDoc("spec");
      importIssueToSpec();
    });
  }
  if (specIssueUI.input) {
    specIssueUI.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        if (getActiveDoc() !== "spec") setDoc("spec");
        importIssueToSpec();
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
            chatUI.input.value = state.history[getHistoryNavIndex()].prompt || "";
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
            chatUI.input.value = state.history[getHistoryNavIndex()].prompt || "";
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
  loadSnapshot().catch(() => {});
  renderChat();
  document.body.dataset.docsReady = "true";
  publish("docs:ready");

  registerAutoRefresh("docs-content", {
    callback: safeLoadDocs,
    tabId: "docs",
    interval: CONSTANTS.UI.AUTO_REFRESH_INTERVAL,
    refreshOnActivation: true,
    immediate: false,
  });
}
