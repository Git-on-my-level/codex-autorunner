import { subscribe } from "./bus.js";
import {
  ArchiveSnapshotSummary,
  ArchiveTreeNode,
  downloadArchiveFile,
  fetchArchiveSnapshot,
  listArchiveSnapshots,
  listArchiveTree,
  readArchiveFile,
} from "./archiveApi.js";
import { escapeHtml, flash, statusPill } from "./utils.js";

let initialized = false;
let snapshots: ArchiveSnapshotSummary[] = [];
let selected: ArchiveSnapshotSummary | null = null;
let activeSnapshotKey = "";

const listEl = document.getElementById("archive-snapshot-list");
const detailEl = document.getElementById("archive-snapshot-detail");
const emptyEl = document.getElementById("archive-empty");
const refreshBtn = document.getElementById("archive-refresh") as HTMLButtonElement | null;

const MAX_PREVIEW_BYTES = 200_000;
const MAX_PREVIEW_CHARS = 200_000;

type ArchiveFileState = {
  snapshotId: string;
  worktreeRepoId: string;
  snapshotKey: string;
  currentPath: string;
  nodes: ArchiveTreeNode[];
  selectedFile: ArchiveTreeNode | null;
};

type ArchiveFileEls = {
  list: HTMLElement;
  breadcrumbs: HTMLElement;
  fileTitle: HTMLElement;
  fileMeta: HTMLElement;
  fileContent: HTMLElement;
  fileEmpty: HTMLElement;
  downloadBtn: HTMLButtonElement;
  loadBtn: HTMLButtonElement;
  quickLinks: HTMLElement | null;
  refreshBtn: HTMLButtonElement | null;
};

let fileState: ArchiveFileState | null = null;
let fileEls: ArchiveFileEls | null = null;
let treeRequestToken = 0;
let fileRequestToken = 0;
let artifactRequestToken = 0;

const QUICK_LINKS: Array<{ label: string; path: string; kind: "file" | "folder" }> = [
  { label: "Active Context", path: "workspace/active_context.md", kind: "file" },
  { label: "Decisions", path: "workspace/decisions.md", kind: "file" },
  { label: "Spec", path: "workspace/spec.md", kind: "file" },
  { label: "Tickets", path: "tickets", kind: "folder" },
  { label: "Runs", path: "runs", kind: "folder" },
  { label: "Flows", path: "flows", kind: "folder" },
  { label: "Logs", path: "logs", kind: "folder" },
];

function formatTimestamp(ts?: string | null): string {
  if (!ts) return "–";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  return date.toLocaleString();
}

function formatBytes(bytes?: number | null): string {
  if (bytes === null || bytes === undefined) return "–";
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  const mb = kb / 1024;
  return `${mb.toFixed(1)} MB`;
}

function snapshotKey(snapshot: ArchiveSnapshotSummary): string {
  return `${snapshot.snapshot_id}::${snapshot.worktree_repo_id}`;
}

function parentPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  if (parts.length <= 1) return "";
  parts.pop();
  return parts.join("/");
}

function renderEmptyDetail(message: string): void {
  if (!detailEl) return;
  detailEl.innerHTML = `
    <div class="archive-empty-state">
      <div class="archive-empty-title">${escapeHtml(message)}</div>
      <div class="archive-empty-hint">Select a snapshot on the left to view metadata.</div>
    </div>
  `;
}

function renderList(items: ArchiveSnapshotSummary[]): void {
  if (!listEl) return;
  if (!items.length) {
    listEl.innerHTML = "";
    if (emptyEl) emptyEl.classList.remove("hidden");
    renderEmptyDetail("No archived snapshots yet.");
    return;
  }
  if (emptyEl) emptyEl.classList.add("hidden");

  const selectedKey = selected ? snapshotKey(selected) : "";
  listEl.innerHTML = items
    .map((item) => {
      const isActive = selectedKey && selectedKey === snapshotKey(item);
      const created = formatTimestamp(item.created_at);
      const branch = item.branch ? `· ${item.branch}` : "";
      const status = item.status ? item.status : "unknown";
      const note = item.note ? ` · ${item.note}` : "";
      return `
        <button class="archive-snapshot${isActive ? " active" : ""}" data-snapshot-id="${escapeHtml(item.snapshot_id)}" data-worktree-id="${escapeHtml(item.worktree_repo_id)}">
          <div class="archive-snapshot-title">${escapeHtml(item.snapshot_id)}</div>
          <div class="archive-snapshot-meta muted small">${escapeHtml(created)} ${escapeHtml(branch)}</div>
          <div class="archive-snapshot-meta muted small">Status: ${escapeHtml(status)}${escapeHtml(note)}</div>
        </button>
      `;
    })
    .join("");
}

function renderSummaryGrid(
  summary: ArchiveSnapshotSummary,
  meta?: Record<string, unknown> | null
): string {
  const created = formatTimestamp(summary.created_at);
  const headSha = summary.head_sha ? summary.head_sha : "–";
  const branch = summary.branch ? summary.branch : "–";
  const note = summary.note ? summary.note : "–";
  const summaryValues: Array<[string, string]> = [
    ["Snapshot ID", summary.snapshot_id],
    ["Worktree Repo", summary.worktree_repo_id],
    ["Created", created],
    ["Branch", branch],
    ["Head SHA", headSha],
    ["Note", note],
  ];
  const rows = summaryValues
    .map(
      ([label, value]) => `
        <div class="archive-meta-row">
          <div class="archive-meta-label muted small">${escapeHtml(label)}</div>
          <div class="archive-meta-value">${escapeHtml(value)}</div>
        </div>
      `
    )
    .join("");

  const summaryObj = summary.summary && typeof summary.summary === "object" ? summary.summary : null;
  const summaryBlock = summaryObj
    ? `
        <div class="archive-summary-block">
          <div class="archive-section-title">Summary</div>
          <pre>${escapeHtml(JSON.stringify(summaryObj, null, 2))}</pre>
        </div>
      `
    : "";

  const metaBlock = meta
    ? `
        <details class="archive-summary-block">
          <summary class="archive-section-title">META.json</summary>
          <pre>${escapeHtml(JSON.stringify(meta, null, 2))}</pre>
        </details>
      `
    : `
        <div class="archive-summary-block muted small">META.json not available for this snapshot.</div>
      `;

  return `
    <div class="archive-meta-grid">
      ${rows}
    </div>
    ${summaryBlock}
    ${metaBlock}
  `;
}

function getMetaSourceList(
  meta: Record<string, unknown> | null | undefined,
  key: "copied_paths" | "missing_paths"
): string[] {
  if (!meta || typeof meta !== "object") return [];
  const source = meta.source as Record<string, unknown> | undefined;
  if (!source || typeof source !== "object") return [];
  const list = source[key];
  if (!Array.isArray(list)) return [];
  return list.filter((item): item is string => typeof item === "string");
}

function pathPresence(meta: Record<string, unknown> | null | undefined, path: string): string {
  if (!meta) return "–";
  const copied = getMetaSourceList(meta, "copied_paths");
  if (copied.includes(path)) return "Yes";
  const missing = getMetaSourceList(meta, "missing_paths");
  if (missing.includes(path)) return "No";
  return "–";
}

function summaryValue(summary: ArchiveSnapshotSummary, key: string): unknown {
  const summaryObj = summary.summary && typeof summary.summary === "object" ? summary.summary : null;
  return summaryObj ? summaryObj[key] : undefined;
}

function formatSummaryCount(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return "–";
}

function formatSummaryString(value: unknown): string {
  if (typeof value === "string" && value) return value;
  return "–";
}

function renderArtifactSection(
  summary: ArchiveSnapshotSummary,
  meta?: Record<string, unknown> | null
): string {
  const flowCount = formatSummaryCount(summaryValue(summary, "flow_run_count"));
  const latestFlow = formatSummaryString(summaryValue(summary, "latest_flow_run_id"));
  const runsPresent = pathPresence(meta, "runs");
  const flowsPresent = pathPresence(meta, "flows");
  const flowsDbPresent = pathPresence(meta, "flows.db");

  return `
    <div class="archive-summary-block">
      <div class="archive-section-title">Runs &amp; Flows</div>
      <div class="archive-meta-grid">
        <div class="archive-meta-row">
          <div class="archive-meta-label muted small">Runs present</div>
          <div class="archive-meta-value">${escapeHtml(runsPresent)}</div>
        </div>
        <div class="archive-meta-row">
          <div class="archive-meta-label muted small">Flows present</div>
          <div class="archive-meta-value">${escapeHtml(flowsPresent)}</div>
        </div>
        <div class="archive-meta-row">
          <div class="archive-meta-label muted small">Flows DB present</div>
          <div class="archive-meta-value">${escapeHtml(flowsDbPresent)}</div>
        </div>
        <div class="archive-meta-row">
          <div class="archive-meta-label muted small">Flow run count</div>
          <div class="archive-meta-value">${escapeHtml(flowCount)}</div>
        </div>
        <div class="archive-meta-row">
          <div class="archive-meta-label muted small">Latest flow run</div>
          <div class="archive-meta-value">${escapeHtml(latestFlow)}</div>
        </div>
      </div>
      <div class="archive-quick-links archive-artifact-actions" id="archive-artifact-actions">
        <button class="ghost sm" data-archive-path="runs" data-archive-kind="folder">Runs</button>
        <button class="ghost sm" data-archive-path="flows" data-archive-kind="folder">Flows</button>
      </div>
      <div class="archive-meta-grid">
        <div class="archive-meta-row">
          <div class="archive-meta-label muted small">Run IDs</div>
          <div class="archive-meta-value" id="archive-run-list">Loading…</div>
        </div>
        <div class="archive-meta-row">
          <div class="archive-meta-label muted small">Flow IDs</div>
          <div class="archive-meta-value" id="archive-flow-list">Loading…</div>
        </div>
      </div>
    </div>
  `;
}

function renderFileSection(): string {
  const quickLinks = QUICK_LINKS.map(
    (item) =>
      `<button class="ghost sm" data-archive-path="${escapeHtml(item.path)}" data-archive-kind="${item.kind}">${escapeHtml(item.label)}</button>`
  ).join("");
  return `
    <div class="archive-file-section">
      <div class="archive-file-header-row">
        <div>
          <div class="archive-section-title">Archive files</div>
          <div class="muted small">Browse snapshot files (read-only).</div>
        </div>
        <div class="archive-quick-links" id="archive-quick-links">
          ${quickLinks}
        </div>
      </div>
      <div class="archive-file-path-row">
        <nav class="workspace-breadcrumbs" id="archive-breadcrumbs"></nav>
        <button class="ghost sm" id="archive-tree-refresh">Reload</button>
      </div>
      <div class="workspace-grid archive-file-grid">
        <aside class="workspace-file-browser archive-file-browser">
          <div class="workspace-file-list" id="archive-file-list"></div>
        </aside>
        <div class="workspace-main archive-file-main">
          <div class="archive-file-viewer-header">
            <div>
              <div class="archive-file-title" id="archive-file-title">Select a file</div>
              <div class="archive-file-meta muted small" id="archive-file-meta"></div>
            </div>
            <div class="archive-file-actions">
              <button class="ghost sm" id="archive-file-load" hidden>Load anyway</button>
              <button class="ghost sm" id="archive-file-download" disabled>Download</button>
            </div>
          </div>
          <div class="archive-file-empty muted small" id="archive-file-empty">Select a file to preview.</div>
          <pre class="archive-file-content hidden" id="archive-file-content"></pre>
        </div>
      </div>
    </div>
  `;
}

function collectFileEls(): ArchiveFileEls | null {
  const list = document.getElementById("archive-file-list");
  const breadcrumbs = document.getElementById("archive-breadcrumbs");
  const fileTitle = document.getElementById("archive-file-title");
  const fileMeta = document.getElementById("archive-file-meta");
  const fileContent = document.getElementById("archive-file-content");
  const fileEmpty = document.getElementById("archive-file-empty");
  const downloadBtn = document.getElementById("archive-file-download") as HTMLButtonElement | null;
  const loadBtn = document.getElementById("archive-file-load") as HTMLButtonElement | null;
  const quickLinks = document.getElementById("archive-quick-links");
  const refreshButton = document.getElementById("archive-tree-refresh") as HTMLButtonElement | null;
  if (!list || !breadcrumbs || !fileTitle || !fileMeta || !fileContent || !fileEmpty || !downloadBtn || !loadBtn) {
    return null;
  }
  return {
    list,
    breadcrumbs,
    fileTitle,
    fileMeta,
    fileContent,
    fileEmpty,
    downloadBtn,
    loadBtn,
    quickLinks,
    refreshBtn: refreshButton,
  };
}

function resetFileViewer(): void {
  if (!fileEls) return;
  fileEls.fileTitle.textContent = "Select a file";
  fileEls.fileMeta.textContent = "";
  fileEls.fileContent.textContent = "";
  fileEls.fileContent.classList.add("hidden");
  fileEls.fileEmpty.classList.remove("hidden");
  fileEls.downloadBtn.disabled = true;
  fileEls.loadBtn.hidden = true;
}

function renderBreadcrumbs(path: string): void {
  if (!fileEls) return;
  const container = fileEls.breadcrumbs;
  container.innerHTML = "";
  const nav = document.createElement("div");
  nav.className = "workspace-breadcrumbs-inner";

  const rootBtn = document.createElement("button");
  rootBtn.type = "button";
  rootBtn.textContent = "Snapshot";
  rootBtn.addEventListener("click", () => {
    void navigateTo("");
  });
  nav.appendChild(rootBtn);

  const parts = path ? path.split("/") : [];
  let accum = "";
  parts.forEach((part) => {
    const sep = document.createElement("span");
    sep.textContent = " / ";
    nav.appendChild(sep);
    accum = accum ? `${accum}/${part}` : part;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = part;
    const target = accum;
    btn.addEventListener("click", () => {
      void navigateTo(target);
    });
    nav.appendChild(btn);
  });

  container.appendChild(nav);
}

function renderFileList(): void {
  if (!fileState || !fileEls) return;
  const list = fileEls.list;
  list.innerHTML = "";

  renderBreadcrumbs(fileState.currentPath);

  if (fileState.currentPath) {
    const upRow = document.createElement("div");
    upRow.className = "workspace-tree-row workspace-folder-row";
    const label = document.createElement("div");
    label.className = "workspace-tree-label";
    const main = document.createElement("div");
    main.className = "workspace-tree-main";
    const caret = document.createElement("span");
    caret.className = "workspace-tree-caret";
    caret.textContent = "◂";
    main.appendChild(caret);
    const name = document.createElement("button");
    name.type = "button";
    name.className = "workspace-tree-name";
    name.textContent = "Up one level";
    const navigateUp = () => {
      void navigateTo(parentPath(fileState.currentPath));
    };
    name.addEventListener("click", navigateUp);
    main.appendChild(name);
    label.appendChild(main);
    upRow.appendChild(label);
    upRow.addEventListener("click", (evt) => {
      const target = evt.target as HTMLElement | null;
      if (target?.closest("button")) return;
      navigateUp();
    });
    list.appendChild(upRow);
  }

  if (!fileState.nodes.length) {
    const empty = document.createElement("div");
    empty.className = "muted small";
    empty.textContent = "This folder is empty.";
    list.appendChild(empty);
    return;
  }

  fileState.nodes.forEach((node) => {
    const row = document.createElement("div");
    row.className = `workspace-tree-row ${node.type === "folder" ? "workspace-folder-row" : "workspace-file-row"}`;
    if (fileState.selectedFile?.path === node.path) row.classList.add("active");
    row.tabIndex = 0;

    const label = document.createElement("div");
    label.className = "workspace-tree-label";
    const main = document.createElement("div");
    main.className = "workspace-tree-main";

    if (node.type === "folder") {
      const caret = document.createElement("span");
      caret.className = "workspace-tree-caret";
      caret.textContent = "▸";
      main.appendChild(caret);
    }

    const name = document.createElement("button");
    name.type = "button";
    name.className = "workspace-tree-name";
    name.textContent = node.name;
    const activateNode = () => {
      if (node.type === "folder") {
        void navigateTo(node.path);
      } else {
        void selectFile(node);
      }
    };
    name.addEventListener("click", activateNode);
    main.appendChild(name);
    label.appendChild(main);

    const meta = document.createElement("span");
    meta.className = "workspace-tree-meta";
    if (node.type === "file" && node.size_bytes != null) {
      meta.textContent = formatBytes(node.size_bytes);
    }
    if (meta.textContent) label.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "workspace-item-actions";
    if (node.type === "file") {
      const dlBtn = document.createElement("button");
      dlBtn.type = "button";
      dlBtn.className = "ghost sm workspace-download-btn";
      dlBtn.textContent = "⬇";
      dlBtn.title = `Download ${node.name}`;
      dlBtn.addEventListener("click", (evt) => {
        evt.stopPropagation();
        if (!fileState) return;
        downloadArchiveFile(fileState.snapshotId, fileState.worktreeRepoId, node.path);
      });
      actions.appendChild(dlBtn);
    }

    row.appendChild(label);
    if (actions.childElementCount) row.appendChild(actions);

    row.addEventListener("click", (evt) => {
      const target = evt.target as HTMLElement | null;
      if (target?.closest(".workspace-item-actions")) return;
      if (target?.closest("button")) return;
      activateNode();
    });
    row.addEventListener("keydown", (evt) => {
      if (evt.target !== row) return;
      if (evt.key === "Enter" || evt.key === " ") {
        evt.preventDefault();
        activateNode();
      }
    });

    list.appendChild(row);
  });
}

async function navigateTo(path: string): Promise<void> {
  if (!fileState || !fileEls) return;
  fileEls.list.innerHTML = "Loading…";
  const requestId = ++treeRequestToken;
  try {
    const res = await listArchiveTree(fileState.snapshotId, fileState.worktreeRepoId, path);
    if (!fileState || fileState.snapshotKey !== activeSnapshotKey) return;
    if (requestId !== treeRequestToken) return;
    fileState.currentPath = res.path || "";
    fileState.nodes = res.nodes || [];
    renderFileList();
  } catch (err) {
    fileEls.list.innerHTML = "";
    const msg = (err as Error).message || "Failed to load archive tree";
    const error = document.createElement("div");
    error.className = "muted small";
    error.textContent = msg;
    fileEls.list.appendChild(error);
    flash("Failed to load archive files.", "error");
  }
}

async function openFilePath(path: string): Promise<void> {
  if (!fileState) return;
  const folder = parentPath(path);
  await navigateTo(folder);
  if (!fileState || fileState.snapshotKey !== activeSnapshotKey) return;
  const node = fileState.nodes.find((item) => item.path === path && item.type === "file");
  if (node) {
    await selectFile(node);
  } else {
    flash("File not found in archive snapshot.", "error");
  }
}

async function selectFile(node: ArchiveTreeNode, forceLoad = false): Promise<void> {
  if (!fileState || !fileEls) return;
  if (node.type !== "file") return;
  fileState.selectedFile = node;
  renderFileList();

  fileEls.fileTitle.textContent = node.name;
  fileEls.fileMeta.textContent = `${formatBytes(node.size_bytes)} · ${node.path}`;
  fileEls.fileContent.textContent = "";
  fileEls.fileContent.classList.add("hidden");
  fileEls.fileEmpty.classList.add("hidden");
  fileEls.downloadBtn.disabled = false;
  fileEls.downloadBtn.onclick = () => {
    if (!fileState) return;
    downloadArchiveFile(fileState.snapshotId, fileState.worktreeRepoId, node.path);
  };

  if (node.size_bytes && node.size_bytes > MAX_PREVIEW_BYTES && !forceLoad) {
    fileEls.fileContent.textContent = `Preview disabled for ${formatBytes(node.size_bytes)} file. Use Download or Load anyway.`;
    fileEls.fileContent.classList.remove("hidden");
    fileEls.loadBtn.hidden = false;
    fileEls.loadBtn.onclick = () => {
      void selectFile(node, true);
    };
    return;
  }

  fileEls.loadBtn.hidden = true;
  const requestId = ++fileRequestToken;
  fileEls.fileContent.textContent = "Loading…";
  fileEls.fileContent.classList.remove("hidden");

  try {
    const text = await readArchiveFile(fileState.snapshotId, fileState.worktreeRepoId, node.path);
    if (!fileState || fileState.snapshotKey !== activeSnapshotKey) return;
    if (requestId !== fileRequestToken) return;
    let display = text;
    if (display.length > MAX_PREVIEW_CHARS) {
      display = `${display.slice(0, MAX_PREVIEW_CHARS)}\n\n… truncated; download for full file.`;
    }
    fileEls.fileContent.textContent = display;
  } catch (err) {
    const msg = (err as Error).message || "Failed to load file";
    fileEls.fileContent.textContent = msg;
    flash("Failed to load archive file.", "error");
  }
}

function initArchiveFileViewer(summary: ArchiveSnapshotSummary): void {
  fileEls = collectFileEls();
  if (!fileEls) return;
  const key = snapshotKey(summary);
  activeSnapshotKey = key;
  fileState = {
    snapshotId: summary.snapshot_id,
    worktreeRepoId: summary.worktree_repo_id,
    snapshotKey: key,
    currentPath: "",
    nodes: [],
    selectedFile: null,
  };
  resetFileViewer();
  wireArchivePathButtons(fileEls.quickLinks);
  fileEls.refreshBtn?.addEventListener("click", () => {
    void navigateTo(fileState?.currentPath || "");
  });
  void navigateTo("");
}

function wireArchivePathButtons(container: HTMLElement | null): void {
  if (!container) return;
  container.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (!target) return;
    const btn = target.closest("button[data-archive-path]") as HTMLButtonElement | null;
    if (!btn) return;
    const path = btn.dataset.archivePath;
    const kind = btn.dataset.archiveKind as "file" | "folder" | undefined;
    if (!path) return;
    if (kind === "folder") {
      void navigateTo(path);
    } else {
      void openFilePath(path);
    }
  });
}

function renderArtifactList(
  container: HTMLElement | null,
  nodes: ArchiveTreeNode[],
  emptyMessage: string
): void {
  if (!container) return;
  const folders = nodes.filter((node) => node.type === "folder");
  if (!folders.length) {
    container.textContent = emptyMessage;
    return;
  }
  container.innerHTML = folders
    .map(
      (node) =>
        `<button class="ghost sm" data-archive-path="${escapeHtml(node.path)}" data-archive-kind="folder">${escapeHtml(node.name)}</button>`
    )
    .join(" ");
}

async function loadArtifactListings(summary: ArchiveSnapshotSummary): Promise<void> {
  const runList = document.getElementById("archive-run-list");
  const flowList = document.getElementById("archive-flow-list");
  const requestId = ++artifactRequestToken;
  if (runList) runList.textContent = "Loading…";
  if (flowList) flowList.textContent = "Loading…";
  try {
    const runs = await listArchiveTree(summary.snapshot_id, summary.worktree_repo_id, "runs");
    if (!fileState || fileState.snapshotKey !== activeSnapshotKey) return;
    if (requestId !== artifactRequestToken) return;
    renderArtifactList(runList, runs.nodes || [], "No run IDs found.");
  } catch {
    if (requestId !== artifactRequestToken) return;
    if (runList) runList.textContent = "Runs folder not present.";
  }

  try {
    const flows = await listArchiveTree(summary.snapshot_id, summary.worktree_repo_id, "flows");
    if (!fileState || fileState.snapshotKey !== activeSnapshotKey) return;
    if (requestId !== artifactRequestToken) return;
    renderArtifactList(flowList, flows.nodes || [], "No flow IDs found.");
  } catch {
    if (requestId !== artifactRequestToken) return;
    if (flowList) flowList.textContent = "Flows folder not present.";
  }

  wireArchivePathButtons(runList);
  wireArchivePathButtons(flowList);
}

async function loadSnapshotDetail(target: ArchiveSnapshotSummary): Promise<void> {
  if (!detailEl) return;
  detailEl.innerHTML = `<div class="muted small">Loading snapshot…</div>`;
  try {
    const res = await fetchArchiveSnapshot(target.snapshot_id, target.worktree_repo_id);
    const summary = res.snapshot;
    const meta = res.meta ?? null;
    detailEl.innerHTML = `
      <div class="archive-detail-header">
        <div>
          <div class="archive-detail-title">${escapeHtml(summary.snapshot_id)}</div>
          <div class="archive-detail-subtitle muted small">${escapeHtml(summary.worktree_repo_id)}</div>
        </div>
        <span class="pill pill-idle" id="archive-detail-status">${escapeHtml(summary.status || "unknown")}</span>
      </div>
      ${renderSummaryGrid(summary, meta)}
      ${renderArtifactSection(summary, meta)}
      ${renderFileSection()}
    `;
    const statusEl = document.getElementById("archive-detail-status");
    if (statusEl) statusPill(statusEl, summary.status || "unknown");
    initArchiveFileViewer(summary);
    wireArchivePathButtons(document.getElementById("archive-artifact-actions"));
    void loadArtifactListings(summary);
  } catch (err) {
    detailEl.innerHTML = `<div class="archive-empty-state">
      <div class="archive-empty-title">Failed to load snapshot.</div>
      <div class="archive-empty-hint muted small">${escapeHtml((err as Error).message || "Unknown error")}</div>
    </div>`;
    flash("Failed to load archive snapshot.", "error");
  }
}

function selectSnapshot(target: ArchiveSnapshotSummary): void {
  selected = target;
  renderList(snapshots);
  void loadSnapshotDetail(target);
}

async function loadSnapshots(): Promise<void> {
  if (!listEl) return;
  listEl.innerHTML = "Loading…";
  if (emptyEl) emptyEl.classList.add("hidden");
  try {
    const items = await listArchiveSnapshots();
    const sorted = items.slice().sort((a, b) => {
      const aTime = a.created_at ? new Date(a.created_at).getTime() : 0;
      const bTime = b.created_at ? new Date(b.created_at).getTime() : 0;
      if (aTime !== bTime) return bTime - aTime;
      return (b.snapshot_id || "").localeCompare(a.snapshot_id || "");
    });
    snapshots = sorted;
    renderList(sorted);
    if (!sorted.length) return;
    const selectedKey = selected ? snapshotKey(selected) : "";
    const match = selectedKey
      ? sorted.find((item) => snapshotKey(item) === selectedKey)
      : null;
    const next = match || sorted[0];
    selectSnapshot(next);
  } catch (err) {
    listEl.innerHTML = "";
    renderEmptyDetail("Unable to load archive snapshots.");
    if (emptyEl) emptyEl.classList.add("hidden");
    flash("Failed to load archive snapshots.", "error");
  }
}

function handleListClick(event: Event): void {
  const target = event.target as HTMLElement | null;
  if (!target) return;
  const btn = target.closest(".archive-snapshot") as HTMLElement | null;
  if (!btn) return;
  const snapshotId = btn.dataset.snapshotId;
  const worktreeId = btn.dataset.worktreeId;
  if (!snapshotId || !worktreeId) return;
  const match = snapshots.find(
    (item) => item.snapshot_id === snapshotId && item.worktree_repo_id === worktreeId
  );
  selectSnapshot(match || { snapshot_id: snapshotId, worktree_repo_id: worktreeId });
}

export function initArchive(): void {
  if (initialized) return;
  initialized = true;

  if (!listEl || !detailEl) return;

  listEl.addEventListener("click", handleListClick);
  refreshBtn?.addEventListener("click", () => {
    void loadSnapshots();
  });

  subscribe("repo:health", (payload: unknown) => {
    const status = (payload as { status?: string } | null)?.status || "";
    if (status === "ok" || status === "degraded") {
      void loadSnapshots();
    }
  });

  void loadSnapshots();
}
