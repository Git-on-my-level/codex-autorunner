import { subscribe } from "./bus.js";
import { ArchiveSnapshotSummary, fetchArchiveSnapshot, listArchiveSnapshots } from "./archiveApi.js";
import { escapeHtml, flash, statusPill } from "./utils.js";

let initialized = false;
let snapshots: ArchiveSnapshotSummary[] = [];
let selected: ArchiveSnapshotSummary | null = null;

const listEl = document.getElementById("archive-snapshot-list");
const detailEl = document.getElementById("archive-snapshot-detail");
const emptyEl = document.getElementById("archive-empty");
const refreshBtn = document.getElementById("archive-refresh") as HTMLButtonElement | null;

function formatTimestamp(ts?: string | null): string {
  if (!ts) return "–";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  return date.toLocaleString();
}

function snapshotKey(snapshot: ArchiveSnapshotSummary): string {
  return `${snapshot.snapshot_id}::${snapshot.worktree_repo_id}`;
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
    `;
    const statusEl = document.getElementById("archive-detail-status");
    if (statusEl) statusPill(statusEl, summary.status || "unknown");
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
