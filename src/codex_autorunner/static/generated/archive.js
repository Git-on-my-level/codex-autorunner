// GENERATED FILE - do not edit directly. Source: static_src/
import { subscribe } from "./bus.js";
import { downloadArchiveFile, downloadLocalArchiveFile, fetchArchiveSnapshot, listArchiveSnapshots, listArchiveTree, listLocalArchiveTree, listLocalRunArchives, readArchiveFile, readLocalArchiveFile, } from "./archiveApi.js";
import { escapeHtml, flash, statusPill, setButtonLoading } from "./utils.js";
let initialized = false;
let snapshots = [];
let localArchives = [];
let selectedItem = null;
let activeSourceKey = "";
let activeSubTab = "snapshot";
let lastArchiveSignature = "";
/** Compute a signature of the archive lists for change detection. */
function archiveSignature(snapshotItems, localItems) {
    const snapSig = snapshotItems
        .map((s) => `${s.snapshot_id}:${s.worktree_repo_id}:${s.status || ""}`)
        .join("|");
    const localSig = localItems
        .map((s) => `${s.run_id}:${s.archived_at || ""}:${s.has_tickets ? "t" : "f"}:${s.has_runs ? "t" : "f"}`)
        .join("|");
    return `${snapSig}::${localSig}`;
}
const listEl = document.getElementById("archive-snapshot-list");
const detailEl = document.getElementById("archive-snapshot-detail");
const emptyEl = document.getElementById("archive-empty");
const refreshBtn = document.getElementById("archive-refresh");
const MAX_PREVIEW_BYTES = 200000;
const MAX_PREVIEW_CHARS = 200000;
let fileState = null;
let fileEls = null;
let treeRequestToken = 0;
let fileRequestToken = 0;
let artifactRequestToken = 0;
const SNAPSHOT_QUICK_LINKS = [
    { label: "Active Context", path: "workspace/active_context.md", kind: "file" },
    { label: "Decisions", path: "workspace/decisions.md", kind: "file" },
    { label: "Spec", path: "workspace/spec.md", kind: "file" },
    { label: "Tickets", path: "tickets", kind: "folder" },
    { label: "Runs", path: "runs", kind: "folder" },
    { label: "Flows", path: "flows", kind: "folder" },
    { label: "Logs", path: "logs", kind: "folder" },
];
const LOCAL_QUICK_LINKS = [
    { label: "Archived tickets", path: "archived_tickets", kind: "folder" },
    { label: "Archived runs", path: "archived_runs", kind: "folder" },
];
function formatTimestamp(ts) {
    if (!ts)
        return "–";
    const date = new Date(ts);
    if (Number.isNaN(date.getTime()))
        return ts;
    return date.toLocaleString();
}
function formatBytes(bytes) {
    if (bytes === null || bytes === undefined)
        return "–";
    if (bytes < 1024)
        return `${bytes} B`;
    const kb = bytes / 1024;
    if (kb < 1024)
        return `${kb.toFixed(1)} KB`;
    const mb = kb / 1024;
    return `${mb.toFixed(1)} MB`;
}
function snapshotKey(snapshot) {
    return `${snapshot.snapshot_id}::${snapshot.worktree_repo_id}`;
}
function listItemKey(item) {
    if (item.kind === "snapshot") {
        return `snapshot:${snapshotKey(item.summary)}`;
    }
    return `local:${item.summary.run_id}`;
}
function parentPath(path) {
    const parts = path.split("/").filter(Boolean);
    if (parts.length <= 1)
        return "";
    parts.pop();
    return parts.join("/");
}
function renderEmptyDetail(message) {
    if (!detailEl)
        return;
    detailEl.innerHTML = `
    <div class="archive-empty-state">
      <div class="archive-empty-title">${escapeHtml(message)}</div>
      <div class="archive-empty-hint">Select a snapshot or local run archive on the left to view details.</div>
    </div>
  `;
}
function renderList(snapshotItems, localItems) {
    if (!listEl)
        return;
    const hasItems = snapshotItems.length > 0 || localItems.length > 0;
    if (!hasItems) {
        listEl.innerHTML = "";
        if (emptyEl)
            emptyEl.classList.remove("hidden");
        renderEmptyDetail("No archives yet.");
        return;
    }
    if (emptyEl)
        emptyEl.classList.add("hidden");
    const selectedKey = selectedItem ? listItemKey(selectedItem) : "";
    const snapshotHtml = snapshotItems
        .map((item) => {
        const isActive = selectedKey && selectedKey === `snapshot:${snapshotKey(item)}`;
        const created = formatTimestamp(item.created_at);
        const branch = item.branch ? `· ${item.branch}` : "";
        const status = item.status ? item.status : "unknown";
        const note = item.note ? ` · ${item.note}` : "";
        return `
        <button class="archive-snapshot${isActive ? " active" : ""}" data-kind="snapshot" data-snapshot-id="${escapeHtml(item.snapshot_id)}" data-worktree-id="${escapeHtml(item.worktree_repo_id)}">
          <div class="archive-snapshot-title">${escapeHtml(item.snapshot_id)}</div>
          <div class="archive-snapshot-meta muted small">${escapeHtml(created)} ${escapeHtml(branch)}</div>
          <div class="archive-snapshot-meta muted small">Status: ${escapeHtml(status)}${escapeHtml(note)}</div>
        </button>
      `;
    })
        .join("");
    const localHtml = localItems
        .map((item) => {
        const isActive = selectedKey && selectedKey === `local:${item.run_id}`;
        const created = formatTimestamp(item.archived_at);
        const tickets = item.has_tickets ? "tickets" : "no tickets";
        const runs = item.has_runs ? "runs" : "no runs";
        return `
        <button class="archive-snapshot${isActive ? " active" : ""}" data-kind="local" data-run-id="${escapeHtml(item.run_id)}">
          <div class="archive-snapshot-title">${escapeHtml(item.run_id)}</div>
          <div class="archive-snapshot-meta muted small">${escapeHtml(created)} · Local run archive</div>
          <div class="archive-snapshot-meta muted small">${escapeHtml(tickets)} · ${escapeHtml(runs)}</div>
        </button>
      `;
    })
        .join("");
    const snapshotSection = `
    <div class="archive-list-section">
      <div class="archive-list-header muted small">Worktree snapshots</div>
      ${snapshotHtml || `<div class="archive-list-empty muted small">No snapshots.</div>`}
    </div>
  `;
    const localSection = `
    <div class="archive-list-section">
      <div class="archive-list-header muted small">Local run archives</div>
      ${localHtml || `<div class="archive-list-empty muted small">No run archives.</div>`}
    </div>
  `;
    listEl.innerHTML = `${snapshotSection}${localSection}`;
}
function renderSummaryGrid(summary, meta) {
    const created = formatTimestamp(summary.created_at);
    const headSha = summary.head_sha ? summary.head_sha : "–";
    const branch = summary.branch ? summary.branch : "–";
    const note = summary.note ? summary.note : "–";
    const summaryValues = [
        ["Snapshot ID", summary.snapshot_id],
        ["Worktree Repo", summary.worktree_repo_id],
        ["Created", created],
        ["Branch", branch],
        ["Head SHA", headSha],
        ["Note", note],
    ];
    const rows = summaryValues
        .map(([label, value]) => `
        <div class="archive-meta-row">
          <div class="archive-meta-label muted small">${escapeHtml(label)}</div>
          <div class="archive-meta-value">${escapeHtml(value)}</div>
        </div>
      `)
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
function getMetaSourceList(meta, key) {
    if (!meta || typeof meta !== "object")
        return [];
    const source = meta.source;
    if (!source || typeof source !== "object")
        return [];
    const list = source[key];
    if (!Array.isArray(list))
        return [];
    return list.filter((item) => typeof item === "string");
}
function pathPresence(meta, path) {
    if (!meta)
        return "–";
    const copied = getMetaSourceList(meta, "copied_paths");
    if (copied.includes(path))
        return "Yes";
    const missing = getMetaSourceList(meta, "missing_paths");
    if (missing.includes(path))
        return "No";
    return "–";
}
function summaryValue(summary, key) {
    const summaryObj = summary.summary && typeof summary.summary === "object" ? summary.summary : null;
    return summaryObj ? summaryObj[key] : undefined;
}
function formatSummaryCount(value) {
    if (typeof value === "number" && Number.isFinite(value))
        return String(value);
    return "–";
}
function formatSummaryString(value) {
    if (typeof value === "string" && value)
        return value;
    return "–";
}
function renderArtifactSection(summary, meta) {
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
function renderSubTabs(summaryLabel) {
    return `
    <div class="archive-subtabs">
      <button class="archive-subtab${activeSubTab === "snapshot" ? " active" : ""}" data-subtab="snapshot">${escapeHtml(summaryLabel)}</button>
      <button class="archive-subtab${activeSubTab === "files" ? " active" : ""}" data-subtab="files">Files</button>
    </div>
  `;
}
function switchSubTab(tab) {
    activeSubTab = tab;
    // Update tab button states
    const tabBtns = document.querySelectorAll(".archive-subtab");
    tabBtns.forEach((btn) => {
        const btnTab = btn.dataset.subtab;
        btn.classList.toggle("active", btnTab === tab);
    });
    // Update content visibility
    const snapshotContent = document.getElementById("archive-tab-snapshot");
    const filesContent = document.getElementById("archive-tab-files");
    snapshotContent?.classList.toggle("active", tab === "snapshot");
    filesContent?.classList.toggle("active", tab === "files");
}
function wireSubTabs() {
    const container = document.querySelector(".archive-subtabs");
    if (!container)
        return;
    container.addEventListener("click", (e) => {
        const target = e.target;
        if (!target)
            return;
        const btn = target.closest(".archive-subtab");
        if (!btn)
            return;
        const tab = btn.dataset.subtab;
        if (tab && (tab === "snapshot" || tab === "files")) {
            switchSubTab(tab);
        }
    });
}
function renderFileSection(quickLinksData, description) {
    const quickLinks = quickLinksData
        .map((item) => `<button class="ghost sm" data-archive-path="${escapeHtml(item.path)}" data-archive-kind="${item.kind}">${escapeHtml(item.label)}</button>`)
        .join("");
    const descriptionText = description || "Browse archive files (read-only).";
    return `
    <div class="archive-file-section">
      <div class="archive-file-header-row">
        <div>
          <div class="archive-section-title">Archive files</div>
          <div class="muted small">${escapeHtml(descriptionText)}</div>
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
function renderLocalSummary(run) {
    const archivedAt = formatTimestamp(run.archived_at);
    const tickets = run.has_tickets ? "Yes" : "No";
    const runs = run.has_runs ? "Yes" : "No";
    const actionButtons = [
        run.has_tickets
            ? `<button class="ghost sm" data-archive-path="archived_tickets" data-archive-kind="folder">Archived tickets</button>`
            : "",
        run.has_runs
            ? `<button class="ghost sm" data-archive-path="archived_runs" data-archive-kind="folder">Archived runs</button>`
            : "",
    ]
        .filter(Boolean)
        .join("");
    return `
    <div class="archive-meta-grid">
      <div class="archive-meta-row">
        <div class="archive-meta-label muted small">Run ID</div>
        <div class="archive-meta-value">${escapeHtml(run.run_id)}</div>
      </div>
      <div class="archive-meta-row">
        <div class="archive-meta-label muted small">Archived at</div>
        <div class="archive-meta-value">${escapeHtml(archivedAt)}</div>
      </div>
      <div class="archive-meta-row">
        <div class="archive-meta-label muted small">Archived tickets</div>
        <div class="archive-meta-value">${escapeHtml(tickets)}</div>
      </div>
      <div class="archive-meta-row">
        <div class="archive-meta-label muted small">Archived runs</div>
        <div class="archive-meta-value">${escapeHtml(runs)}</div>
      </div>
    </div>
    <div class="archive-summary-block">
      <div class="archive-section-title">Artifacts</div>
      <div class="archive-quick-links archive-artifact-actions" id="archive-local-artifact-actions">
        ${actionButtons || `<span class="muted small">No archived folders found.</span>`}
      </div>
    </div>
  `;
}
function collectFileEls() {
    const list = document.getElementById("archive-file-list");
    const breadcrumbs = document.getElementById("archive-breadcrumbs");
    const fileTitle = document.getElementById("archive-file-title");
    const fileMeta = document.getElementById("archive-file-meta");
    const fileContent = document.getElementById("archive-file-content");
    const fileEmpty = document.getElementById("archive-file-empty");
    const downloadBtn = document.getElementById("archive-file-download");
    const loadBtn = document.getElementById("archive-file-load");
    const quickLinks = document.getElementById("archive-quick-links");
    const refreshButton = document.getElementById("archive-tree-refresh");
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
function resetFileViewer() {
    if (!fileEls)
        return;
    fileEls.fileTitle.textContent = "Select a file";
    fileEls.fileMeta.textContent = "";
    fileEls.fileContent.textContent = "";
    fileEls.fileContent.classList.add("hidden");
    fileEls.fileEmpty.classList.remove("hidden");
    fileEls.downloadBtn.disabled = true;
    fileEls.loadBtn.hidden = true;
}
function renderBreadcrumbs(path) {
    if (!fileEls)
        return;
    const container = fileEls.breadcrumbs;
    container.innerHTML = "";
    const nav = document.createElement("div");
    nav.className = "workspace-breadcrumbs-inner";
    const rootBtn = document.createElement("button");
    rootBtn.type = "button";
    rootBtn.textContent = fileState?.rootLabel || "Archive";
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
function renderFileList() {
    if (!fileState || !fileEls)
        return;
    const list = fileEls.list;
    list.innerHTML = "";
    renderBreadcrumbs(fileState.currentPath);
    if (fileState.currentPath) {
        const upRow = document.createElement("div");
        upRow.className = "workspace-tree-row contextspace-folder-row";
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
            const target = evt.target;
            if (target?.closest("button"))
                return;
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
        if (fileState.selectedFile?.path === node.path)
            row.classList.add("active");
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
            }
            else {
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
        if (meta.textContent)
            label.appendChild(meta);
        const actions = document.createElement("div");
        actions.className = "workspace-item-actions";
        if (node.type === "file") {
            const dlBtn = document.createElement("button");
            dlBtn.type = "button";
            dlBtn.className = "ghost sm contextspace-download-btn";
            dlBtn.textContent = "⬇";
            dlBtn.title = `Download ${node.name}`;
            dlBtn.addEventListener("click", (evt) => {
                evt.stopPropagation();
                if (!fileState)
                    return;
                downloadArchiveFile(fileState.snapshotId, fileState.worktreeRepoId, node.path);
            });
            actions.appendChild(dlBtn);
        }
        row.appendChild(label);
        if (actions.childElementCount)
            row.appendChild(actions);
        row.addEventListener("click", (evt) => {
            const target = evt.target;
            if (target?.closest(".workspace-item-actions"))
                return;
            if (target?.closest("button"))
                return;
            activateNode();
        });
        row.addEventListener("keydown", (evt) => {
            if (evt.target !== row)
                return;
            if (evt.key === "Enter" || evt.key === " ") {
                evt.preventDefault();
                activateNode();
            }
        });
        list.appendChild(row);
    });
}
async function listTreeForState(state, path) {
    if (state.kind === "snapshot" && state.snapshotId && state.worktreeRepoId) {
        return listArchiveTree(state.snapshotId, state.worktreeRepoId, path);
    }
    if (state.kind === "local" && state.runId) {
        return listLocalArchiveTree(state.runId, path);
    }
    throw new Error("Invalid archive source");
}
async function readFileForState(state, path) {
    if (state.kind === "snapshot" && state.snapshotId) {
        return readArchiveFile(state.snapshotId, state.worktreeRepoId ?? null, path);
    }
    if (state.kind === "local" && state.runId) {
        return readLocalArchiveFile(state.runId, path);
    }
    throw new Error("Invalid archive source");
}
function downloadFileForState(state, path) {
    if (state.kind === "snapshot" && state.snapshotId) {
        downloadArchiveFile(state.snapshotId, state.worktreeRepoId ?? null, path);
        return;
    }
    if (state.kind === "local" && state.runId) {
        downloadLocalArchiveFile(state.runId, path);
        return;
    }
    throw new Error("Invalid archive source");
}
async function navigateTo(path) {
    if (!fileState || !fileEls)
        return;
    fileEls.list.innerHTML = "Loading…";
    const requestId = ++treeRequestToken;
    try {
        const res = await listTreeForState(fileState, path);
        if (!fileState || fileState.sourceKey !== activeSourceKey)
            return;
        if (requestId !== treeRequestToken)
            return;
        fileState.currentPath = res.path || "";
        fileState.nodes = res.nodes || [];
        renderFileList();
    }
    catch (err) {
        fileEls.list.innerHTML = "";
        const msg = err.message || "Failed to load archive tree";
        const error = document.createElement("div");
        error.className = "muted small";
        error.textContent = msg;
        fileEls.list.appendChild(error);
        flash("Failed to load archive files.", "error");
    }
}
async function openFilePath(path) {
    if (!fileState)
        return;
    const folder = parentPath(path);
    await navigateTo(folder);
    if (!fileState || fileState.sourceKey !== activeSourceKey)
        return;
    const node = fileState.nodes.find((item) => item.path === path && item.type === "file");
    if (node) {
        await selectFile(node);
    }
    else {
        flash("File not found in archive.", "error");
    }
}
async function selectFile(node, forceLoad = false) {
    if (!fileState || !fileEls)
        return;
    if (node.type !== "file")
        return;
    fileState.selectedFile = node;
    renderFileList();
    fileEls.fileTitle.textContent = node.name;
    fileEls.fileMeta.textContent = `${formatBytes(node.size_bytes)} · ${node.path}`;
    fileEls.fileContent.textContent = "";
    fileEls.fileContent.classList.add("hidden");
    fileEls.fileEmpty.classList.add("hidden");
    fileEls.downloadBtn.disabled = false;
    fileEls.downloadBtn.onclick = () => {
        if (!fileState)
            return;
        downloadFileForState(fileState, node.path);
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
        const text = await readFileForState(fileState, node.path);
        if (!fileState || fileState.sourceKey !== activeSourceKey)
            return;
        if (requestId !== fileRequestToken)
            return;
        let display = text;
        if (display.length > MAX_PREVIEW_CHARS) {
            display = `${display.slice(0, MAX_PREVIEW_CHARS)}\n\n… truncated; download for full file.`;
        }
        fileEls.fileContent.textContent = display;
    }
    catch (err) {
        const msg = err.message || "Failed to load file";
        fileEls.fileContent.textContent = msg;
        flash("Failed to load archive file.", "error");
    }
}
function initArchiveFileViewer(source) {
    fileEls = collectFileEls();
    if (!fileEls)
        return;
    const key = listItemKey(source);
    activeSourceKey = key;
    if (source.kind === "snapshot") {
        fileState = {
            kind: "snapshot",
            snapshotId: source.summary.snapshot_id,
            worktreeRepoId: source.summary.worktree_repo_id,
            sourceKey: key,
            rootLabel: "Snapshot",
            currentPath: "",
            nodes: [],
            selectedFile: null,
        };
    }
    else {
        fileState = {
            kind: "local",
            runId: source.summary.run_id,
            sourceKey: key,
            rootLabel: "Run archive",
            currentPath: "",
            nodes: [],
            selectedFile: null,
        };
    }
    resetFileViewer();
    wireArchivePathButtons(fileEls.quickLinks);
    fileEls.refreshBtn?.addEventListener("click", () => {
        void navigateTo(fileState?.currentPath || "");
    });
    void navigateTo("");
}
function wireArchivePathButtons(container) {
    if (!container)
        return;
    container.addEventListener("click", (event) => {
        const target = event.target;
        if (!target)
            return;
        const btn = target.closest("button[data-archive-path]");
        if (!btn)
            return;
        const path = btn.dataset.archivePath;
        const kind = btn.dataset.archiveKind;
        if (!path)
            return;
        if (kind === "folder") {
            void navigateTo(path);
        }
        else {
            void openFilePath(path);
        }
    });
}
function renderArtifactList(container, nodes, emptyMessage) {
    if (!container)
        return;
    const folders = nodes.filter((node) => node.type === "folder");
    if (!folders.length) {
        container.textContent = emptyMessage;
        return;
    }
    container.innerHTML = folders
        .map((node) => `<button class="ghost sm" data-archive-path="${escapeHtml(node.path)}" data-archive-kind="folder">${escapeHtml(node.name)}</button>`)
        .join(" ");
}
async function loadArtifactListings(summary) {
    const runList = document.getElementById("archive-run-list");
    const flowList = document.getElementById("archive-flow-list");
    const requestId = ++artifactRequestToken;
    if (runList)
        runList.textContent = "Loading…";
    if (flowList)
        flowList.textContent = "Loading…";
    try {
        const runs = await listArchiveTree(summary.snapshot_id, summary.worktree_repo_id, "runs");
        if (!fileState || fileState.sourceKey !== activeSourceKey)
            return;
        if (requestId !== artifactRequestToken)
            return;
        renderArtifactList(runList, runs.nodes || [], "No run IDs found.");
    }
    catch {
        if (requestId !== artifactRequestToken)
            return;
        if (runList)
            runList.textContent = "Runs folder not present.";
    }
    try {
        const flows = await listArchiveTree(summary.snapshot_id, summary.worktree_repo_id, "flows");
        if (!fileState || fileState.sourceKey !== activeSourceKey)
            return;
        if (requestId !== artifactRequestToken)
            return;
        renderArtifactList(flowList, flows.nodes || [], "No flow IDs found.");
    }
    catch {
        if (requestId !== artifactRequestToken)
            return;
        if (flowList)
            flowList.textContent = "Flows folder not present.";
    }
    wireArchivePathButtons(runList);
    wireArchivePathButtons(flowList);
}
async function loadSnapshotDetail(target) {
    if (!detailEl)
        return;
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
      ${renderSubTabs("Snapshot")}
      <div id="archive-tab-snapshot" class="archive-tab-content archive-tab-snapshot${activeSubTab === "snapshot" ? " active" : ""}">
        ${renderSummaryGrid(summary, meta)}
        ${renderArtifactSection(summary, meta)}
      </div>
      <div id="archive-tab-files" class="archive-tab-content archive-tab-files${activeSubTab === "files" ? " active" : ""}">
        ${renderFileSection(SNAPSHOT_QUICK_LINKS, "Browse snapshot files (read-only).")}
      </div>
    `;
        const statusEl = document.getElementById("archive-detail-status");
        if (statusEl)
            statusPill(statusEl, summary.status || "unknown");
        wireSubTabs();
        initArchiveFileViewer({ kind: "snapshot", summary });
        wireArchivePathButtons(document.getElementById("archive-artifact-actions"));
        void loadArtifactListings(summary);
    }
    catch (err) {
        detailEl.innerHTML = `<div class="archive-empty-state">
      <div class="archive-empty-title">Failed to load snapshot.</div>
      <div class="archive-empty-hint muted small">${escapeHtml(err.message || "Unknown error")}</div>
    </div>`;
        flash("Failed to load archive snapshot.", "error");
    }
}
async function loadLocalDetail(target) {
    if (!detailEl)
        return;
    detailEl.innerHTML = `<div class="muted small">Loading run archive…</div>`;
    detailEl.innerHTML = `
    <div class="archive-detail-header">
      <div>
        <div class="archive-detail-title">${escapeHtml(target.run_id)}</div>
        <div class="archive-detail-subtitle muted small">Local run archive</div>
      </div>
      <span class="pill pill-idle" id="archive-detail-status">local</span>
    </div>
    ${renderSubTabs("Overview")}
    <div id="archive-tab-snapshot" class="archive-tab-content archive-tab-snapshot${activeSubTab === "snapshot" ? " active" : ""}">
      ${renderLocalSummary(target)}
    </div>
    <div id="archive-tab-files" class="archive-tab-content archive-tab-files${activeSubTab === "files" ? " active" : ""}">
      ${renderFileSection(LOCAL_QUICK_LINKS, "Browse archived run files (read-only).")}
    </div>
  `;
    wireSubTabs();
    initArchiveFileViewer({ kind: "local", summary: target });
    wireArchivePathButtons(document.getElementById("archive-local-artifact-actions"));
}
function selectItem(target) {
    selectedItem = target;
    renderList(snapshots, localArchives);
    if (target.kind === "snapshot") {
        void loadSnapshotDetail(target.summary);
    }
    else {
        void loadLocalDetail(target.summary);
    }
}
async function loadArchiveData(forceReload = false) {
    if (!listEl)
        return;
    const isInitialLoad = snapshots.length === 0 && localArchives.length === 0;
    const showRefreshIndicator = !isInitialLoad;
    if (showRefreshIndicator) {
        setButtonLoading(refreshBtn, true);
    }
    // Only show loading indicator on initial load to avoid UI flicker
    if (isInitialLoad) {
        listEl.innerHTML = "Loading…";
    }
    if (emptyEl)
        emptyEl.classList.add("hidden");
    try {
        const [snapshotItems, localItems] = await Promise.all([listArchiveSnapshots(), listLocalRunArchives()]);
        const sortedSnapshots = snapshotItems.slice().sort((a, b) => {
            const aTime = a.created_at ? new Date(a.created_at).getTime() : 0;
            const bTime = b.created_at ? new Date(b.created_at).getTime() : 0;
            if (aTime !== bTime)
                return bTime - aTime;
            return (b.snapshot_id || "").localeCompare(a.snapshot_id || "");
        });
        const sortedLocals = localItems.slice().sort((a, b) => {
            const aTime = a.archived_at ? new Date(a.archived_at).getTime() : 0;
            const bTime = b.archived_at ? new Date(b.archived_at).getTime() : 0;
            if (aTime !== bTime)
                return bTime - aTime;
            return (b.run_id || "").localeCompare(a.run_id || "");
        });
        // Check if archives have changed
        const newSignature = archiveSignature(sortedSnapshots, sortedLocals);
        const hasChanged = newSignature !== lastArchiveSignature;
        // Skip update if nothing changed and not forced
        if (!forceReload && !hasChanged && !isInitialLoad) {
            return;
        }
        lastArchiveSignature = newSignature;
        snapshots = sortedSnapshots;
        localArchives = sortedLocals;
        renderList(sortedSnapshots, sortedLocals);
        if (!sortedSnapshots.length && !sortedLocals.length)
            return;
        const selectedKey = selectedItem ? listItemKey(selectedItem) : "";
        const matchSnapshot = selectedKey && selectedKey.startsWith("snapshot:")
            ? sortedSnapshots.find((item) => `snapshot:${snapshotKey(item)}` === selectedKey)
            : null;
        const matchLocal = selectedKey && selectedKey.startsWith("local:")
            ? sortedLocals.find((item) => `local:${item.run_id}` === selectedKey)
            : null;
        // Only reload detail if selection changed or forced
        if (forceReload || (!matchSnapshot && !matchLocal) || isInitialLoad) {
            const nextSnapshot = sortedSnapshots[0];
            const nextLocal = sortedLocals[0];
            if (nextSnapshot) {
                selectItem({ kind: "snapshot", summary: nextSnapshot });
            }
            else if (nextLocal) {
                selectItem({ kind: "local", summary: nextLocal });
            }
        }
        else if (matchSnapshot) {
            selectedItem = { kind: "snapshot", summary: matchSnapshot };
            renderList(sortedSnapshots, sortedLocals);
        }
        else if (matchLocal) {
            selectedItem = { kind: "local", summary: matchLocal };
            renderList(sortedSnapshots, sortedLocals);
        }
    }
    catch (err) {
        listEl.innerHTML = "";
        renderEmptyDetail("Unable to load archives.");
        if (emptyEl)
            emptyEl.classList.add("hidden");
        flash("Failed to load archives.", "error");
    }
    finally {
        if (showRefreshIndicator) {
            setButtonLoading(refreshBtn, false);
        }
    }
}
function handleListClick(event) {
    const target = event.target;
    if (!target)
        return;
    const btn = target.closest(".archive-snapshot");
    if (!btn)
        return;
    const kind = btn.dataset.kind;
    if (kind === "snapshot") {
        const snapshotId = btn.dataset.snapshotId;
        const worktreeId = btn.dataset.worktreeId;
        if (!snapshotId || !worktreeId)
            return;
        const match = snapshots.find((item) => item.snapshot_id === snapshotId && item.worktree_repo_id === worktreeId);
        selectItem({ kind: "snapshot", summary: match || { snapshot_id: snapshotId, worktree_repo_id: worktreeId } });
    }
    else if (kind === "local") {
        const runId = btn.dataset.runId;
        if (!runId)
            return;
        const match = localArchives.find((item) => item.run_id === runId);
        selectItem({ kind: "local", summary: match || { run_id: runId, has_tickets: false, has_runs: false } });
    }
}
export function initArchive() {
    if (initialized)
        return;
    initialized = true;
    if (!listEl || !detailEl)
        return;
    listEl.addEventListener("click", handleListClick);
    refreshBtn?.addEventListener("click", () => {
        void loadArchiveData(true); // Force reload on manual refresh
    });
    subscribe("repo:health", (payload) => {
        const status = payload?.status || "";
        if (status === "ok" || status === "degraded") {
            void loadArchiveData(); // Non-forced: only updates if data changed
        }
    });
    void loadArchiveData(true); // Initial load
}
