import { api, flash } from "./utils.js";
import { renderMarkdown } from "./messages.js";
import { resolvePath } from "./utils.js";
import { publish } from "./bus.js";
import {
  type TicketFile,
  type DispatchEntry,
  type DispatchAttachment,
  DISPATCH_PANEL_COLLAPSED_KEY,
  formatDispatchTime,
  formatNumber,
  formatElapsedSeconds,
  truncate,
  isFlowActiveStatus,
} from "./ticketFlowState.js";
import type { TicketData } from "./ticketEditor.js";

let dragSourceIndex: number | null = null;
let dragTargetIndex: number | null = null;
let dragPlaceAfter = false;
let selectedTicketPath: string | null = null;
let dispatchPanelCollapsed = false;

export function getSelectedTicketPath(): string | null {
  return selectedTicketPath;
}

function clearTicketDragState(): void {
  dragSourceIndex = null;
  dragTargetIndex = null;
  dragPlaceAfter = false;
  const ticketList = document.getElementById("ticket-flow-tickets");
  if (!ticketList) return;
  ticketList
    .querySelectorAll(".ticket-item.drag-source, .ticket-item.drop-before, .ticket-item.drop-after")
    .forEach((el) => {
      el.classList.remove("drag-source", "drop-before", "drop-after");
    });
}

function getTicketMoveToPosition(
  tickets: TicketFile[],
  sourceIndex: number,
  destinationIndex: number,
  placeAfter: boolean
): number | null {
  const ordered = tickets
    .map((ticket) => ticket.index)
    .filter((index): index is number => typeof index === "number");
  const sourcePos = ordered.indexOf(sourceIndex) + 1;
  const destinationPos = ordered.indexOf(destinationIndex) + 1;
  if (!sourcePos || !destinationPos) return null;
  const desiredPos = destinationPos + (placeAfter ? 1 : 0);
  const toPos = sourcePos < desiredPos ? desiredPos - 1 : desiredPos;
  return Math.max(1, Math.min(toPos, ordered.length));
}

async function reorderTicket(
  sourceIndex: number,
  destinationIndex: number,
  placeAfter: boolean
): Promise<void> {
  await api("/api/flows/ticket_flow/tickets/reorder", {
    method: "POST",
    body: {
      source_index: sourceIndex,
      destination_index: destinationIndex,
      place_after: placeAfter,
    },
  });
}

function renderDispatchMiniList(entries: DispatchEntry[]): void {
  const dispatchMiniList = document.getElementById("dispatch-mini-list");
  const dispatchPanel = document.getElementById("dispatch-panel");
  if (!dispatchMiniList) return;
  dispatchMiniList.innerHTML = "";

  const maxMiniItems = 8;
  entries.slice(0, maxMiniItems).forEach((entry) => {
    const dispatch = entry.dispatch;
    const isTurnSummary = dispatch?.mode === "turn_summary" || dispatch?.extra?.is_turn_summary;
    const isNotify = dispatch?.mode === "notify";

    const mini = document.createElement("div");
    mini.className = `dispatch-mini-item${isNotify ? " notify" : ""}`;
    mini.textContent = `#${entry.seq || "?"}`;
    mini.title = isTurnSummary
      ? "Agent turn output"
      : dispatch?.title || `Dispatch #${entry.seq}`;

    mini.addEventListener("click", () => {
      if (dispatchPanel && dispatchPanelCollapsed) {
        dispatchPanelCollapsed = false;
        dispatchPanel.classList.remove("collapsed");
        localStorage.setItem(DISPATCH_PANEL_COLLAPSED_KEY, "false");
      }
    });

    dispatchMiniList.appendChild(mini);
  });

  if (entries.length > maxMiniItems) {
    const more = document.createElement("div");
    more.className = "dispatch-mini-item";
    more.textContent = `+${entries.length - maxMiniItems}`;
    more.title = `${entries.length - maxMiniItems} more dispatches`;
    more.addEventListener("click", () => {
      if (dispatchPanel && dispatchPanelCollapsed) {
        dispatchPanelCollapsed = false;
        dispatchPanel.classList.remove("collapsed");
        localStorage.setItem(DISPATCH_PANEL_COLLAPSED_KEY, "false");
      }
    });
    dispatchMiniList.appendChild(more);
  }
}

function updateSelectedTicketInternal(path: string | null): void {
  selectedTicketPath = path;
  const ticketList = document.getElementById("ticket-flow-tickets");
  if (!ticketList) return;

  const items = ticketList.querySelectorAll(".ticket-item");
  items.forEach((item) => {
    const ticketPath = item.getAttribute("data-ticket-path");
    if (ticketPath === path) {
      item.classList.add("selected");
    } else {
      item.classList.remove("selected");
    }
  });
}

export function updateSelectedTicket(path: string | null): void {
  updateSelectedTicketInternal(path);
}

export function updateScrollFade(): void {
  const ticketList = document.getElementById("ticket-flow-tickets");
  const dispatchHistory = document.getElementById("ticket-dispatch-history");

  [ticketList, dispatchHistory].forEach((list) => {
    if (!list) return;
    const panel = list.closest(".ticket-panel");
    if (!panel) return;

    const hasScrollableContent = list.scrollHeight > list.clientHeight;
    const isNotAtBottom = list.scrollTop + list.clientHeight < list.scrollHeight - 10;

    if (hasScrollableContent && isNotAtBottom) {
      panel.classList.add("has-scroll-bottom");
    } else {
      panel.classList.remove("has-scroll-bottom");
    }
  });
}

export function renderTickets(
  data: { tickets?: TicketFile[]; lint_errors?: string[] } | null,
  options?: { currentActiveTicket?: string | null; currentFlowStatus?: string | null; ticketListCache?: { tickets?: TicketFile[] } | null }
): { tickets?: TicketFile[] } | null {
  const currentActiveTicket = options?.currentActiveTicket ?? null;
  const currentFlowStatus = options?.currentFlowStatus ?? null;
  const cache = options?.ticketListCache ?? null;
  clearTicketDragState();
  const ticketsEl = document.getElementById("ticket-flow-tickets");
  const dirEl = document.getElementById("ticket-flow-dir");
  if (dirEl) dirEl.textContent = ".codex-autorunner/tickets";
  if (!ticketsEl) return cache;

  ticketsEl.innerHTML = "";

  if (data?.lint_errors && data.lint_errors.length > 0) {
    const lintBanner = document.createElement("div");
    lintBanner.className = "ticket-lint-errors";
    data.lint_errors.forEach((error) => {
      const errorLine = document.createElement("div");
      errorLine.textContent = error;
      lintBanner.appendChild(errorLine);
    });
    ticketsEl.appendChild(lintBanner);
  }

  const list = (data?.tickets || []) as TicketFile[];

  const progressBar = document.getElementById("ticket-progress-bar");
  const progressFill = document.getElementById("ticket-progress-fill");
  if (progressBar && progressFill) {
    if (list.length === 0) {
      progressBar.classList.add("hidden");
    } else {
      progressBar.classList.remove("hidden");
      const doneCount = list.filter((t) => Boolean((t.frontmatter || {})?.done)).length;
      const percent = Math.round((doneCount / list.length) * 100);
      progressFill.style.width = `${percent}%`;
      progressBar.title = `${doneCount} of ${list.length} tickets done`;
    }
  }

  if (!list.length) {
    ticketsEl.textContent = "No tickets found. Start the ticket flow to create TICKET-001.md.";
    return cache;
  }

  list.forEach((ticket) => {
    const item = document.createElement("div");
    const fm = (ticket.frontmatter || {}) as Record<string, unknown>;
    const done = Boolean(fm?.done);
    const isActive = Boolean(
      currentActiveTicket &&
        ticket.path === currentActiveTicket &&
        isFlowActiveStatus(currentFlowStatus)
    );
    item.className = `ticket-item ${done ? "done" : ""} ${isActive ? "active" : ""} ${selectedTicketPath === ticket.path ? "selected" : ""} clickable`;
    item.title = "Click to edit";
    item.setAttribute("data-ticket-path", ticket.path || "");

    const ticketIndex = typeof ticket.index === "number" ? ticket.index : null;

    if (ticketIndex !== null) {
      const dragHandle = document.createElement("button");
      dragHandle.className = "ticket-reorder-handle";
      dragHandle.type = "button";
      dragHandle.title = "Drag to reorder ticket";
      dragHandle.setAttribute("aria-label", "Drag to reorder ticket");
      dragHandle.draggable = true;
      for (let i = 0; i < 6; i++) {
        dragHandle.appendChild(document.createElement("span"));
      }
      dragHandle.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
      });
      dragHandle.addEventListener("dragstart", (e: DragEvent) => {
        dragSourceIndex = ticketIndex;
        dragTargetIndex = null;
        dragPlaceAfter = false;
        item.classList.add("drag-source");
        if (e.dataTransfer) {
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("text/plain", String(ticketIndex));
        }
      });
      dragHandle.addEventListener("dragend", () => {
        clearTicketDragState();
      });
      item.appendChild(dragHandle);
    }

    item.addEventListener("click", async () => {
      updateSelectedTicketInternal(ticket.path || null);
      try {
        if (ticket.index == null) {
          flash("Invalid ticket: missing index", "error");
          return;
        }
        const data = (await api(`/api/flows/ticket_flow/tickets/${ticket.index}`)) as TicketFile;
        const { openTicketEditor } = await import("./ticketEditor.js");
        openTicketEditor(data as TicketData);
      } catch (err) {
        flash(`Failed to load ticket: ${(err as Error).message}`, "error");
      }
    });
    item.addEventListener("dragover", (e: DragEvent) => {
      if (dragSourceIndex === null || ticketIndex === null || dragSourceIndex === ticketIndex) {
        return;
      }
      e.preventDefault();
      const rect = item.getBoundingClientRect();
      dragPlaceAfter = e.clientY > rect.top + rect.height / 2;
      dragTargetIndex = ticketIndex;
      item.classList.toggle("drop-before", !dragPlaceAfter);
      item.classList.toggle("drop-after", dragPlaceAfter);
      if (e.dataTransfer) {
        e.dataTransfer.dropEffect = "move";
      }
    });
    item.addEventListener("dragleave", () => {
      item.classList.remove("drop-before", "drop-after");
    });
    item.addEventListener("drop", async (e: DragEvent) => {
      if (dragSourceIndex === null || dragTargetIndex === null) return;
      e.preventDefault();
      const sourceIndex = dragSourceIndex;
      const destinationIndex = dragTargetIndex;
      const placeAfter = dragPlaceAfter;
      clearTicketDragState();
      const toPos = getTicketMoveToPosition(list, sourceIndex, destinationIndex, placeAfter);
      if (toPos === null) return;
      const ordered = list
        .map((t) => t.index)
        .filter((idx): idx is number => typeof idx === "number");
      const fromPos = ordered.indexOf(sourceIndex) + 1;
      if (!fromPos || toPos === fromPos) return;
      try {
        await reorderTicket(sourceIndex, destinationIndex, placeAfter);
        publish("tickets:updated", {});
      } catch (err) {
        flash((err as Error).message || "Failed to reorder ticket", "error");
      }
    });

    const head = document.createElement("div");
    head.className = "ticket-item-head";

    const ticketPath = ticket.path || "";
    const ticketMatch = ticketPath.match(/TICKET-\d+/);
    const ticketNumber = ticketMatch ? ticketMatch[0] : "TICKET";
    const ticketTitle = fm?.title ? String(fm.title) : "";

    const name = document.createElement("span");
    name.className = "ticket-name";

    const numSpan = document.createElement("span");
    numSpan.className = "ticket-num";
    const numMatch = ticketNumber.match(/\d+/);
    numSpan.textContent = numMatch ? numMatch[0] : ticketNumber;
    name.appendChild(numSpan);

    if (ticketTitle) {
      const titleSpan = document.createElement("span");
      titleSpan.className = "ticket-title-text";
      titleSpan.textContent = `: ${ticketTitle}`;
      name.appendChild(titleSpan);
    }

    item.title = ticketTitle ? `${ticketNumber}: ${ticketTitle}` : ticketNumber;
    head.appendChild(name);

    const badges = document.createElement("span");
    badges.className = "ticket-badges";

    if (isActive) {
      const workingBadge = document.createElement("span");
      workingBadge.className = "ticket-working-badge";
      const workingText = document.createElement("span");
      workingText.className = "badge-text";
      workingText.textContent = "Working";
      workingBadge.appendChild(workingText);
      badges.appendChild(workingBadge);
    }

    if (done && !isActive) {
      const doneBadge = document.createElement("span");
      doneBadge.className = "ticket-done-badge";
      const doneText = document.createElement("span");
      doneText.className = "badge-text";
      doneText.textContent = "Done";
      doneBadge.appendChild(doneText);
      badges.appendChild(doneBadge);
    }

    const agent = document.createElement("span");
    agent.className = "ticket-agent";
    agent.textContent = (fm?.agent as string) || "codex";
    badges.appendChild(agent);

    const diffStats = ticket.diff_stats || null;
    if (diffStats && (diffStats.insertions > 0 || diffStats.deletions > 0)) {
      const statsEl = document.createElement("span");
      statsEl.className = "ticket-diff-stats";
      const ins = diffStats.insertions || 0;
      const del = diffStats.deletions || 0;
      statsEl.innerHTML = `<span class="diff-add">+${formatNumber(ins)}</span><span class="diff-del">-${formatNumber(del)}</span>`;
      statsEl.title = `${ins} insertions, ${del} deletions${diffStats.files_changed ? `, ${diffStats.files_changed} files` : ""}`;
      head.appendChild(statsEl);
    }

    if (typeof ticket.duration_seconds === "number" && ticket.duration_seconds > 0) {
      const durEl = document.createElement("span");
      durEl.className = "ticket-duration";
      durEl.textContent = formatElapsedSeconds(ticket.duration_seconds);
      durEl.title = `Time taken: ${formatElapsedSeconds(ticket.duration_seconds)}`;
      head.appendChild(durEl);
    }

    head.appendChild(badges);
    item.appendChild(head);

    if (ticket.errors && ticket.errors.length) {
      const errors = document.createElement("div");
      errors.className = "ticket-errors";
      errors.textContent = `Frontmatter issues: ${ticket.errors.join("; ")}`;
      item.appendChild(errors);
    }

    if (ticket.body) {
      const body = document.createElement("div");
      body.className = "ticket-body";
      body.textContent = truncate(ticket.body.replace(/\s+/g, " ").trim());
      item.appendChild(body);
    }

    ticketsEl.appendChild(item);
  });

  updateScrollFade();
  return cache;
}

export function renderDispatchHistory(
  runId: string | null,
  data: { history?: DispatchEntry[] } | null
): void {
  const historyEl = document.getElementById("ticket-dispatch-history");
  const dispatchNote = document.getElementById("ticket-dispatch-note");
  if (!historyEl) return;
  historyEl.innerHTML = "";

  const dispatchMiniList = document.getElementById("dispatch-mini-list");

  if (!runId) {
    historyEl.textContent = "Start the ticket flow to see agent dispatches.";
    if (dispatchNote) dispatchNote.textContent = "–";
    if (dispatchMiniList) dispatchMiniList.innerHTML = "";
    return;
  }

  const entries = (data?.history || []) as DispatchEntry[];
  if (!entries.length) {
    historyEl.textContent = "No dispatches yet.";
    if (dispatchNote) dispatchNote.textContent = "–";
    if (dispatchMiniList) dispatchMiniList.innerHTML = "";
    return;
  }

  if (dispatchNote) dispatchNote.textContent = `Latest #${entries[0]?.seq ?? "–"}`;

  renderDispatchMiniList(entries);

  entries.forEach((entry, index) => {
    const dispatch = entry.dispatch;
    const isTurnSummary = dispatch?.mode === "turn_summary" || dispatch?.extra?.is_turn_summary;
    const isHandoff = dispatch?.mode === "pause";
    const isNotify = dispatch?.mode === "notify";

    const isFirst = index === 0;
    const isCollapsed = !isFirst;

    const container = document.createElement("div");
    container.className = `dispatch-item${isTurnSummary ? " turn-summary" : ""}${isHandoff ? " pause" : ""}${isNotify ? " notify" : ""}${isCollapsed ? " collapsed" : ""}`;

    const collapseBar = document.createElement("div");
    collapseBar.className = "dispatch-collapse-bar";
    collapseBar.title = isCollapsed ? "Click to expand" : "Click to collapse";
    collapseBar.setAttribute("role", "button");
    collapseBar.setAttribute("tabindex", "0");
    collapseBar.setAttribute("aria-label", isCollapsed ? "Expand dispatch" : "Collapse dispatch");
    collapseBar.setAttribute("aria-expanded", String(!isCollapsed));

    const toggleCollapse = () => {
      container.classList.toggle("collapsed");
      const isNowCollapsed = container.classList.contains("collapsed");
      collapseBar.title = isNowCollapsed ? "Click to expand" : "Click to collapse";
      collapseBar.setAttribute("aria-expanded", String(!isNowCollapsed));
      collapseBar.setAttribute("aria-label", isNowCollapsed ? "Expand dispatch" : "Collapse dispatch");
    };

    collapseBar.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleCollapse();
    });
    collapseBar.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggleCollapse();
      }
    });

    const contentWrapper = document.createElement("div");
    contentWrapper.className = "dispatch-content-wrapper";

    const header = document.createElement("div");
    header.className = "dispatch-header";

    header.addEventListener("click", (e) => {
      if ((e.target as HTMLElement).closest("a")) return;
      toggleCollapse();
    });

    const headerContent = document.createElement("div");
    headerContent.className = "dispatch-header-content";
    headerContent.title = isTurnSummary ? "Agent turn output" : "Click header to expand/collapse";

    let modeLabel: string;
    if (isTurnSummary) {
      modeLabel = "TURN";
    } else if (isHandoff) {
      modeLabel = "HANDOFF";
    } else {
      modeLabel = ((dispatch?.mode as string) || "notify").toUpperCase();
    }

    const head = document.createElement("div");
    head.className = "dispatch-item-head";
    const seq = document.createElement("span");
    seq.className = "ticket-name";
    seq.textContent = `#${entry.seq || "?"}`;
    const mode = document.createElement("span");
    mode.className = `ticket-agent${isTurnSummary ? " turn-summary-badge" : ""}`;
    mode.textContent = modeLabel;
    head.append(seq, mode);

    headerContent.appendChild(head);
    header.appendChild(headerContent);
    contentWrapper.appendChild(header);
    container.append(collapseBar, contentWrapper);

    const diffStats = dispatch?.diff_stats as
      | { insertions?: number; deletions?: number; files_changed?: number }
      | undefined;
    if (diffStats && (diffStats.insertions || diffStats.deletions)) {
      const statsEl = document.createElement("span");
      statsEl.className = "dispatch-diff-stats";
      const ins = diffStats.insertions || 0;
      const del = diffStats.deletions || 0;
      statsEl.innerHTML = `<span class="diff-add">+${formatNumber(ins)}</span><span class="diff-del">-${formatNumber(del)}</span>`;
      statsEl.title = `${ins} insertions, ${del} deletions${diffStats.files_changed ? `, ${diffStats.files_changed} files` : ""}`;
      head.appendChild(statsEl);
    }

    const thisTime = entry.created_at ? new Date(entry.created_at).getTime() : 0;
    const prevEntry = entries[index - 1];
    const prevTime = prevEntry?.created_at ? new Date(prevEntry.created_at).getTime() : 0;
    if (thisTime && prevTime && prevTime > thisTime) {
      const durSecs = Math.max(0, Math.round((prevTime - thisTime) / 1000));
      const durEl = document.createElement("span");
      durEl.className = "dispatch-duration";
      durEl.textContent = formatElapsedSeconds(durSecs);
      durEl.title = `Turn duration: ${formatElapsedSeconds(durSecs)}`;
      head.appendChild(durEl);
    }

    const ticketId = dispatch?.extra?.ticket_id as string | undefined;
    if (ticketId) {
      const ticketMatch = ticketId.match(/TICKET-\d+/);
      if (ticketMatch) {
        const ticketLabel = document.createElement("span");
        ticketLabel.className = "dispatch-ticket-ref";
        ticketLabel.textContent = ticketMatch[0];
        ticketLabel.title = ticketId;
        head.appendChild(ticketLabel);
      }
    }

    const timeAgo = formatDispatchTime(entry.created_at);
    if (timeAgo) {
      const timeLabel = document.createElement("span");
      timeLabel.className = "dispatch-time";
      timeLabel.textContent = timeAgo;
      head.appendChild(timeLabel);
    }

    const bodyWrapper = document.createElement("div");
    bodyWrapper.className = "dispatch-body-wrapper";

    if (entry.errors && entry.errors.length) {
      const err = document.createElement("div");
      err.className = "ticket-errors";
      err.textContent = entry.errors.join("; ");
      bodyWrapper.appendChild(err);
    }

    const title = dispatch?.title as string | undefined;
    if (title) {
      const titleEl = document.createElement("div");
      titleEl.className = "ticket-body ticket-dispatch-title";
      titleEl.textContent = title;
      bodyWrapper.appendChild(titleEl);
    }

    const bodyText = dispatch?.body as string | undefined;
    if (bodyText) {
      const body = document.createElement("div");
      body.className = "ticket-body ticket-dispatch-body messages-markdown";
      body.innerHTML = renderMarkdown(bodyText);
      bodyWrapper.appendChild(body);
    }

    const attachments = (entry.attachments || []) as DispatchAttachment[];
    if (attachments.length) {
      const wrap = document.createElement("div");
      wrap.className = "ticket-attachments";
      attachments.forEach((att) => {
        if (!att.url) return;
        const link = document.createElement("a");
        const resolved = new URL(resolvePath(att.url), window.location.origin);
        link.href = resolved.toString();
        link.textContent = att.name || att.rel_path || "attachment";
        if (resolved.origin === window.location.origin) {
          link.download = "";
          link.rel = "noopener";
        } else {
          link.target = "_blank";
          link.rel = "noreferrer noopener";
        }
        link.title = att.path || "";
        wrap.appendChild(link);
      });
      bodyWrapper.appendChild(wrap);
    }

    contentWrapper.appendChild(bodyWrapper);
    historyEl.appendChild(container);
  });

  updateScrollFade();
}

export function initDispatchPanelToggle(): void {
  const dispatchPanel = document.getElementById("dispatch-panel");
  const dispatchPanelToggle = document.getElementById("dispatch-panel-toggle") as HTMLButtonElement | null;
  if (!dispatchPanel || !dispatchPanelToggle) return;

  const stored = localStorage.getItem(DISPATCH_PANEL_COLLAPSED_KEY);
  dispatchPanelCollapsed = stored === "true";
  if (dispatchPanelCollapsed) {
    dispatchPanel.classList.add("collapsed");
  }

  dispatchPanelToggle.addEventListener("click", () => {
    dispatchPanelCollapsed = !dispatchPanelCollapsed;
    dispatchPanel.classList.toggle("collapsed", dispatchPanelCollapsed);
    localStorage.setItem(DISPATCH_PANEL_COLLAPSED_KEY, String(dispatchPanelCollapsed));
  });
}
