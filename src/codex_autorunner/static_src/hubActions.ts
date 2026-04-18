import { api, flash } from "./utils.js";
import { normalizePinnedParentRepoIds } from "./hubFilters.js";
import type { HubData, HubChannelEntry, HubJob } from "./hubTypes.js";

export const HUB_JOB_POLL_INTERVAL_MS = 1200;
export const HUB_JOB_TIMEOUT_MS = 180000;

export let hubData: HubData = {
  repos: [],
  agent_workspaces: [],
  last_scan_at: null,
  pinned_parent_repo_ids: [],
};
export let pinnedParentRepoIds = new Set<string>();
let hubChannelEntries: HubChannelEntry[] = [];

export function getHubData(): HubData {
  return hubData;
}

export function getHubChannelEntries(): HubChannelEntry[] {
  return hubChannelEntries;
}

export function setHubChannelEntries(entries: HubChannelEntry[]): void {
  hubChannelEntries = Array.isArray(entries) ? [...entries] : [];
}

export function getPinnedParentRepoIds(): Set<string> {
  return pinnedParentRepoIds;
}

export function setPinnedParentRepoIds(ids: Set<string>, data: HubData): void {
  pinnedParentRepoIds = ids;
  data.pinned_parent_repo_ids = Array.from(ids);
}

export function applyHubData(data: HubData): void {
  hubData = {
    repos: Array.isArray(data?.repos) ? data.repos : [],
    agent_workspaces: Array.isArray(data?.agent_workspaces)
      ? data.agent_workspaces
      : [],
    last_scan_at: data?.last_scan_at || null,
    pinned_parent_repo_ids: normalizePinnedParentRepoIds(
      data?.pinned_parent_repo_ids
    ),
  };
  pinnedParentRepoIds = new Set(
    normalizePinnedParentRepoIds(hubData.pinned_parent_repo_ids)
  );
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

interface PollHubJobOptions {
  timeoutMs?: number;
}

async function pollHubJob(jobId: string, { timeoutMs = HUB_JOB_TIMEOUT_MS }: PollHubJobOptions = {}): Promise<HubJob> {
  const start = Date.now();
  for (;;) {
    const job = await api(`/hub/jobs/${jobId}`, { method: "GET" }) as HubJob;
    if (job.status === "succeeded") return job;
    if (job.status === "failed") {
      const err = job.error || "Hub job failed";
      throw new Error(err);
    }
    if (Date.now() - start > timeoutMs) {
      throw new Error("Hub job timed out");
    }
    await sleep(HUB_JOB_POLL_INTERVAL_MS);
  }
}

interface StartHubJobOptions {
  body?: unknown;
  startedMessage?: string;
}

export async function startHubJob(path: string, { body, startedMessage }: StartHubJobOptions = {}): Promise<HubJob> {
  const job = await api(path, { method: "POST", body }) as { job_id: string };
  if (startedMessage) {
    flash(startedMessage);
  }
  return pollHubJob(job.job_id);
}

export { applyHubPanelState, toggleHubPanel, initInteractionHarness, attachHandlersAndControls } from "./hubDomBindings.js";
export { bootstrapHubData, refreshHub, triggerHubScan, loadHubUsage, handleSystemUpdate } from "./hubRefresh.js";
export {
  handleCleanupAll,
  showCreateRepoModal,
  showCreateAgentWorkspaceModal,
  hideCreateRepoModal,
  hideCreateAgentWorkspaceModal,
  handleCreateRepoSubmit,
  handleCreateAgentWorkspaceSubmit,
  initHubSettings,
} from "./hubModals.js";
