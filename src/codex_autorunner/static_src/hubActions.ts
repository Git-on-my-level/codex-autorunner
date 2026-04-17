import {
  api,
  flash,
  confirmModal,
  inputModal,
  openModal,
} from "./utils.js";
import {
  HUB_CACHE_TTL_MS,
  HUB_USAGE_CACHE_KEY,
  saveSessionCache,
  loadSessionCache,
  loadHubBootstrapCache,
  saveHubBootstrapCache,
  indexHubUsage,
} from "./hubCache.js";
import {
  isCleanupBlockedByChatBinding,
  normalizePinnedParentRepoIds,
  saveHubOpenPanel,
  saveHubViewPrefs,
  hubViewPrefs,
  loadHubViewPrefs,
  loadHubOpenPanel,
  unboundManagedThreadCount,
} from "./hubFilters.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { initNotificationBell } from "./notificationBell.js";
import {
  renderReposWithScroll,
  renderAgentWorkspaces,
  renderSummary,
  setCleanupAllInFlight,
} from "./hubRepoCards.js";
import {
  describeUpdateTarget,
  getUpdateTarget,
  includesWebUpdateTarget,
  normalizeUpdateTarget,
  type UpdateTargetsResponse,
  updateRestartNotice,
  updateTargetOptionsFromResponse,
} from "./updateTargets.js";
import type {
  HubRepo,
  HubAgentWorkspace,
  HubAgentWorkspaceDetail,
  HubData,
  HubDestinationResponse,
  HubChannelEntry,
  HubChannelDirectoryResponse,
  HubUsageData,
  HubJob,
  UpdateCheckResponse,
  UpdateResponse,
  CleanupAllPreview,
} from "./hubTypes.js";

export const HUB_REFRESH_ACTIVE_MS = 5000;
export const HUB_REFRESH_IDLE_MS = 30000;
export const HUB_JOB_POLL_INTERVAL_MS = 1200;
export const HUB_JOB_TIMEOUT_MS = 180000;

const hubUsageMeta = document.getElementById("hub-usage-meta");
const hubUsageRefresh = document.getElementById("hub-usage-refresh");
const hubVersionEl = document.getElementById("hub-version");
const pmaVersionEl = document.getElementById("pma-version");
const hubRepoSearchInput = document.getElementById(
  "hub-repo-search"
) as HTMLInputElement | null;
const hubFlowFilterEl = document.getElementById(
  "hub-flow-filter"
) as HTMLSelectElement | null;
const hubSortOrderEl = document.getElementById(
  "hub-sort-order"
) as HTMLSelectElement | null;
const hubRepoPanelEl = document.getElementById("hub-repo-panel");
const hubAgentPanelEl = document.getElementById("hub-agent-panel");
const hubShellEl = document.getElementById("hub-shell");
const hubRepoPanelSummaryEl = document.getElementById(
  "hub-repo-panel-summary"
) as HTMLButtonElement | null;
const hubAgentPanelSummaryEl = document.getElementById(
  "hub-agent-panel-summary"
) as HTMLButtonElement | null;
const hubRepoPanelStateEl = document.getElementById("hub-repo-panel-state");
const hubAgentPanelStateEl = document.getElementById("hub-agent-panel-state");
const UPDATE_STATUS_SEEN_KEY = "car_update_status_seen";

const repoListEl = document.getElementById("hub-repo-list");
const agentWorkspaceListEl = document.getElementById("hub-agent-workspace-list");

let hubUsageSummaryRetryTimer: ReturnType<typeof setTimeout> | null = null;

export let hubData: HubData = {
  repos: [],
  agent_workspaces: [],
  last_scan_at: null,
  pinned_parent_repo_ids: [],
};
export let pinnedParentRepoIds = new Set<string>();
let hubChannelEntries: HubChannelEntry[] = [];
let hubOpenPanel: string = loadHubOpenPanel();
let hubCleanupAllClickBound = false;
let lastHubAutoRefreshAt = 0;
const prefetchedUrls = new Set<string>();

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

function setButtonLoading(scanning: boolean): void {
  const buttons = [document.getElementById("hub-refresh")] as (
    | HTMLButtonElement
    | null
  )[];
  buttons.forEach((btn) => {
    if (!btn) return;
    btn.disabled = scanning;
    if (scanning) {
      btn.classList.add("loading");
    } else {
      btn.classList.remove("loading");
    }
  });
}

function splitCommaSeparated(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function currentDockerEnvPassthrough(
  destination: Record<string, unknown> | null | undefined
): string {
  const raw = destination?.env_passthrough;
  if (!Array.isArray(raw)) return "";
  return raw
    .map((item) => String(item || "").trim())
    .filter(Boolean)
    .join(", ");
}

function currentDockerProfile(
  destination: Record<string, unknown> | null | undefined
): string {
  return typeof destination?.profile === "string"
    ? String(destination.profile).trim()
    : "";
}

function currentDockerWorkdir(
  destination: Record<string, unknown> | null | undefined
): string {
  return typeof destination?.workdir === "string"
    ? String(destination.workdir).trim()
    : "";
}

function currentDockerExplicitEnv(
  destination: Record<string, unknown> | null | undefined
): string {
  const raw = destination?.env;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return "";
  return Object.entries(raw as Record<string, unknown>)
    .map(([key, value]) => {
      const cleanKey = String(key || "").trim();
      if (!cleanKey) return "";
      if (value === null || value === undefined) return "";
      return `${cleanKey}=${String(value)}`;
    })
    .filter(Boolean)
    .join(", ");
}

function currentDockerMounts(
  destination: Record<string, unknown> | null | undefined
): string {
  const raw = destination?.mounts;
  if (!Array.isArray(raw)) return "";
  const mounts = raw
    .map((item) => {
      if (!item || typeof item !== "object") return "";
      const source = String((item as Record<string, unknown>).source || "").trim();
      const target = String((item as Record<string, unknown>).target || "").trim();
      const rawReadOnly =
        (item as Record<string, unknown>).read_only ??
        (item as Record<string, unknown>).readOnly ??
        (item as Record<string, unknown>).readonly;
      const readOnly = rawReadOnly === true;
      if (!source || !target) return "";
      return readOnly ? `${source}:${target}:ro` : `${source}:${target}`;
    })
    .filter(Boolean);
  return mounts.join(", ");
}

function parseDockerEnvMap(
  value: string
): { env: Record<string, string>; error: string | null } {
  const env: Record<string, string> = {};
  const entries = splitCommaSeparated(value);
  for (const entry of entries) {
    const splitAt = entry.indexOf("=");
    if (splitAt <= 0) {
      return {
        env: {},
        error: `Invalid env entry "${entry}". Use KEY=VALUE (comma-separated).`,
      };
    }
    const key = entry.slice(0, splitAt).trim();
    const mapValue = entry.slice(splitAt + 1);
    if (!key) {
      return {
        env: {},
        error: `Invalid env entry "${entry}". Use KEY=VALUE (comma-separated).`,
      };
    }
    env[key] = mapValue;
  }
  return { env, error: null };
}

function parseDockerMountList(
  value: string
): {
  mounts: Array<{ source: string; target: string; read_only?: boolean }>;
  error: string | null;
} {
  const mounts: Array<{ source: string; target: string; read_only?: boolean }> = [];
  const entries = splitCommaSeparated(value);
  for (const entry of entries) {
    let mountSpec = entry;
    let readOnly: boolean | null = null;
    const lowerEntry = entry.toLowerCase();
    if (lowerEntry.endsWith(":ro")) {
      mountSpec = entry.slice(0, -3);
      readOnly = true;
    } else if (lowerEntry.endsWith(":rw")) {
      mountSpec = entry.slice(0, -3);
      readOnly = false;
    }
    const splitAt = mountSpec.lastIndexOf(":");
    if (splitAt <= 0 || splitAt >= mountSpec.length - 1) {
      return {
        mounts: [],
        error: `Invalid mount "${entry}". Use source:target[:ro] (comma-separated).`,
      };
    }
    const source = mountSpec.slice(0, splitAt).trim();
    const target = mountSpec.slice(splitAt + 1).trim();
    if (!source || !target) {
      return {
        mounts: [],
        error: `Invalid mount "${entry}". Use source:target[:ro] (comma-separated).`,
      };
    }
    if (readOnly === true) {
      mounts.push({ source, target, read_only: true });
    } else {
      mounts.push({ source, target });
    }
  }
  return { mounts, error: null };
}

async function chooseDestinationKind(
  resourceLabel: string,
  currentKind: string
): Promise<string | null> {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.hidden = true;

  const dialog = document.createElement("div");
  dialog.className = "modal-dialog repo-settings-dialog";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");

  const header = document.createElement("div");
  header.className = "modal-header";
  const title = document.createElement("span");
  title.className = "label";
  title.textContent = `Set destination: ${resourceLabel}`;
  header.appendChild(title);

  const body = document.createElement("div");
  body.className = "modal-body";
  const hint = document.createElement("p");
  hint.className = "muted small";
  hint.textContent = "Choose execution destination kind.";
  body.appendChild(hint);

  const footer = document.createElement("div");
  footer.className = "modal-actions";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "ghost";
  cancelBtn.textContent = "Cancel";

  const localBtn = document.createElement("button");
  localBtn.className = currentKind === "local" ? "primary" : "ghost";
  localBtn.textContent = "Local";

  const dockerBtn = document.createElement("button");
  dockerBtn.className = currentKind === "docker" ? "primary" : "ghost";
  dockerBtn.textContent = "Docker";

  footer.append(cancelBtn, localBtn, dockerBtn);
  dialog.append(header, body, footer);
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  return new Promise((resolve) => {
    let closeModal: (() => void) | null = null;
    let settled = false;
    const returnFocusTo = document.activeElement as HTMLElement | null;

    const finalize = (selected: string | null) => {
      if (settled) return;
      settled = true;
      if (closeModal) {
        const close = closeModal;
        closeModal = null;
        close();
      }
      overlay.remove();
      resolve(selected);
    };

    closeModal = openModal(overlay, {
      initialFocus: currentKind === "docker" ? dockerBtn : localBtn,
      returnFocusTo,
      onRequestClose: () => finalize(null),
    });

    cancelBtn.addEventListener("click", () => finalize(null));
    localBtn.addEventListener("click", () => finalize("local"));
    dockerBtn.addEventListener("click", () => finalize("docker"));
  });
}

function formatDestinationSummary(
  destination: Record<string, unknown> | null | undefined
): string {
  if (!destination || typeof destination !== "object") return "local";
  const kindRaw = destination.kind;
  const kind = typeof kindRaw === "string" ? kindRaw.trim().toLowerCase() : "local";
  if (kind === "docker") {
    const image = typeof destination.image === "string" ? destination.image.trim() : "";
    return image ? `docker:${image}` : "docker";
  }
  return "local";
}

async function promptForDestinationBody(
  resourceLabel: string,
  currentDestination: Record<string, unknown> | null | undefined
): Promise<Record<string, unknown> | null> {
  const current = formatDestinationSummary(currentDestination);
  const currentKind =
    current.startsWith("docker:") || current === "docker" ? "docker" : "local";
  const kind = await chooseDestinationKind(resourceLabel, currentKind);
  if (!kind) return null;
  const body: Record<string, unknown> = { kind };
  if (kind === "docker") {
    const currentImage =
      typeof currentDestination?.image === "string"
        ? String(currentDestination.image)
        : "";
    const imageValue = await inputModal("Docker image:", {
      placeholder: "ghcr.io/acme/repo:tag",
      defaultValue: currentImage,
      confirmText: "Save",
    });
    if (!imageValue) {
      flash("Docker destination requires an image", "error");
      return null;
    }
    body.image = imageValue.trim();
    const configureAdvanced = await confirmModal(
      "Configure optional docker fields (container name, profile, workdir, env passthrough, explicit env, mounts)?",
      {
        confirmText: "Configure",
        cancelText: "Skip",
        danger: false,
      }
    );
    if (configureAdvanced) {
      const currentContainerName =
        typeof currentDestination?.container_name === "string"
          ? String(currentDestination.container_name)
          : "";
      const containerNameValue = await inputModal(
        "Docker container name (optional):",
        {
          placeholder: "car-runner",
          defaultValue: currentContainerName,
          confirmText: "Next",
          allowEmpty: true,
        }
      );
      if (containerNameValue === null) return null;
      const containerName = containerNameValue.trim();
      if (containerName) {
        body.container_name = containerName;
      }

      const profileValue = await inputModal("Docker profile (optional):", {
        placeholder: "full-dev",
        defaultValue: currentDockerProfile(currentDestination),
        confirmText: "Next",
        allowEmpty: true,
      });
      if (profileValue === null) return null;
      const profile = profileValue.trim();
      if (profile) {
        body.profile = profile;
      }

      const workdirValue = await inputModal("Docker workdir (optional):", {
        placeholder: "/workspace",
        defaultValue: currentDockerWorkdir(currentDestination),
        confirmText: "Next",
        allowEmpty: true,
      });
      if (workdirValue === null) return null;
      const workdir = workdirValue.trim();
      if (workdir) {
        body.workdir = workdir;
      }

      const envPassthroughValue = await inputModal(
        "Docker env passthrough (optional, comma-separated):",
        {
          placeholder: "CAR_*, PATH",
          defaultValue: currentDockerEnvPassthrough(currentDestination),
          confirmText: "Next",
          allowEmpty: true,
        }
      );
      if (envPassthroughValue === null) return null;
      const envPassthrough = splitCommaSeparated(envPassthroughValue);
      if (envPassthrough.length) {
        body.env_passthrough = envPassthrough;
      }

      const envMapValue = await inputModal(
        "Docker explicit env map (optional, KEY=VALUE pairs, comma-separated):",
        {
          placeholder: "OPENAI_API_KEY=sk-..., CODEX_HOME=/workspace/.codex",
          defaultValue: currentDockerExplicitEnv(currentDestination),
          confirmText: "Next",
          allowEmpty: true,
        }
      );
      if (envMapValue === null) return null;
      const parsedEnvMap = parseDockerEnvMap(envMapValue);
      if (parsedEnvMap.error) {
        flash(parsedEnvMap.error, "error");
        return null;
      }
      if (Object.keys(parsedEnvMap.env).length) {
        body.env = parsedEnvMap.env;
      }

      const mountsValue = await inputModal(
        "Docker mounts (optional, source:target[:ro] pairs, comma-separated):",
        {
          placeholder: "/host/path:/workspace/path, /cache:/cache:ro",
          defaultValue: currentDockerMounts(currentDestination),
          confirmText: "Save",
          allowEmpty: true,
        }
      );
      if (mountsValue === null) return null;
      const parsedMounts = parseDockerMountList(mountsValue);
      if (parsedMounts.error) {
        flash(parsedMounts.error, "error");
        return null;
      }
      if (parsedMounts.mounts.length) {
        body.mounts = parsedMounts.mounts;
      }
    }
  }
  return body;
}

async function promptAndSetRepoDestination(repo: HubRepo): Promise<boolean> {
  const body = await promptForDestinationBody(
    repo.display_name || repo.id,
    repo.effective_destination
  );
  if (!body) return false;

  const payload = (await api(`/hub/repos/${encodeURIComponent(repo.id)}/destination`, {
    method: "POST",
    body,
  })) as HubDestinationResponse;
  const effective = formatDestinationSummary(payload.effective_destination);
  flash(`Updated destination for ${repo.id}: ${effective}`, "success");
  return true;
}

async function promptAndSetAgentWorkspaceDestination(
  workspace: HubAgentWorkspace
): Promise<boolean> {
  const body = await promptForDestinationBody(
    workspace.display_name || workspace.id,
    workspace.effective_destination
  );
  if (!body) return false;

  const payload = (await api(
    `/hub/agent-workspaces/${encodeURIComponent(workspace.id)}/destination`,
    {
      method: "POST",
      body,
    }
  )) as HubAgentWorkspaceDetail;
  const effective = formatDestinationSummary(payload.effective_destination);
  flash(`Updated destination for ${workspace.id}: ${effective}`, "success");
  return true;
}

async function openRepoSettingsModal(repo: HubRepo): Promise<void> {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.hidden = true;

  const dialog = document.createElement("div");
  dialog.className = "modal-dialog repo-settings-dialog";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");

  const header = document.createElement("div");
  header.className = "modal-header";
  const title = document.createElement("span");
  title.className = "label";
  title.textContent = `Settings: ${repo.display_name || repo.id}`;
  header.appendChild(title);

  const body = document.createElement("div");
  body.className = "modal-body";

  const worktreeSection = document.createElement("div");
  worktreeSection.className = "form-group";
  const worktreeLabel = document.createElement("label");
  worktreeLabel.textContent = "Worktree Setup Commands";
  const worktreeHint = document.createElement("p");
  worktreeHint.className = "muted small";
  worktreeHint.textContent =
    "Commands run with /bin/sh -lc after creating a new worktree. One per line, leave blank to disable.";
  const textarea = document.createElement("textarea");
  textarea.rows = 4;
  textarea.style.width = "100%";
  textarea.style.resize = "vertical";
  textarea.placeholder = "make setup\npnpm install\npre-commit install";
  textarea.value = (repo.worktree_setup_commands || []).join("\n");
  worktreeSection.append(worktreeLabel, worktreeHint, textarea);
  body.appendChild(worktreeSection);

  const destinationSection = document.createElement("div");
  destinationSection.className = "form-group";
  const destinationLabel = document.createElement("label");
  destinationLabel.textContent = "Execution Destination";
  const destinationHint = document.createElement("p");
  destinationHint.className = "muted small";
  destinationHint.textContent = "Set where runs execute for this repo.";
  const destinationRow = document.createElement("div");
  destinationRow.className = "settings-actions";
  const destinationPill = document.createElement("span");
  destinationPill.className = "pill pill-small hub-destination-settings-pill";
  destinationPill.textContent = formatDestinationSummary(repo.effective_destination);
  const destinationBtn = document.createElement("button");
  destinationBtn.className = "ghost";
  destinationBtn.textContent = "Change destination";
  destinationRow.append(destinationPill, destinationBtn);
  destinationSection.append(destinationLabel, destinationHint, destinationRow);
  body.appendChild(destinationSection);

  const dangerSection = document.createElement("div");
  dangerSection.className = "form-group settings-section-danger";
  const dangerLabel = document.createElement("label");
  dangerLabel.textContent = "Danger Zone";
  const dangerHint = document.createElement("p");
  dangerHint.className = "muted small";
  dangerHint.textContent =
    "Remove this repo from hub and delete its local directory.";
  const removeBtn = document.createElement("button");
  removeBtn.className = "danger sm";
  removeBtn.textContent = "Remove repo";
  dangerSection.append(dangerLabel, dangerHint, removeBtn);
  body.appendChild(dangerSection);

  const footer = document.createElement("div");
  footer.className = "modal-actions";
  const cancelBtn = document.createElement("button");
  cancelBtn.className = "ghost";
  cancelBtn.textContent = "Cancel";
  const saveBtn = document.createElement("button");
  saveBtn.className = "primary";
  saveBtn.textContent = "Save";
  footer.append(cancelBtn, saveBtn);

  dialog.append(header, body, footer);
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  return new Promise((resolve) => {
    let closeModal: (() => void) | null = null;
    let settled = false;

    const finalize = async (
      action: "cancel" | "save" | "destination" | "remove"
    ) => {
      if (settled) return;
      settled = true;
      if (closeModal) {
        const close = closeModal;
        closeModal = null;
        close();
      }
      overlay.remove();

      if (action === "save") {
        const commands = textarea.value
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean);
        try {
          await api(`/hub/repos/${encodeURIComponent(repo.id)}/worktree-setup`, {
            method: "POST",
            body: { commands },
          });
          flash(
            commands.length
              ? `Saved ${commands.length} setup command(s) for ${repo.id}`
              : `Cleared setup commands for ${repo.id}`,
            "success"
          );
          await refreshHub();
        } catch (err) {
          flash(
            (err as Error).message || "Failed to save settings",
            "error"
          );
        }
      }
      if (action === "destination") {
        try {
          const updated = await promptAndSetRepoDestination(repo);
          if (updated) {
            await refreshHub();
          }
        } catch (err) {
          flash(
            (err as Error).message || "Failed to update destination",
            "error"
          );
        }
      }
      if (action === "remove") {
        try {
          await removeRepoWithChecks(repo.id);
        } catch (err) {
          flash((err as Error).message || "Failed to remove repo", "error");
        }
      }
      resolve();
    };

    closeModal = openModal(overlay, {
      initialFocus: textarea,
      returnFocusTo: document.activeElement as HTMLElement | null,
      onRequestClose: () => finalize("cancel"),
      onKeydown: (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
          event.preventDefault();
          finalize("save");
        }
      },
    });

    cancelBtn.addEventListener("click", () => finalize("cancel"));
    saveBtn.addEventListener("click", () => finalize("save"));
    destinationBtn.addEventListener("click", () => finalize("destination"));
    removeBtn.addEventListener("click", () => finalize("remove"));
  });
}

async function removeRepoWithChecks(repoId: string): Promise<void> {
  const check = await api(`/hub/repos/${repoId}/remove-check`, {
    method: "GET",
  });
  const warnings: string[] = [];
  const dirty = (check as { is_clean?: boolean }).is_clean === false;
  if (dirty) {
    warnings.push("Working tree has uncommitted changes.");
  }
  const upstream = (check as {
    upstream?: { has_upstream?: boolean; ahead?: number; behind?: number };
  }).upstream;
  const hasUpstream = upstream?.has_upstream === false;
  if (hasUpstream) {
    warnings.push("No upstream tracking branch is configured.");
  }
  const ahead = Number(upstream?.ahead || 0);
  if (ahead > 0) {
    warnings.push(`Local branch is ahead of upstream by ${ahead} commit(s).`);
  }
  const behind = Number(upstream?.behind || 0);
  if (behind > 0) {
    warnings.push(`Local branch is behind upstream by ${behind} commit(s).`);
  }
  const worktrees = Array.isArray((check as { worktrees?: string[] }).worktrees)
    ? (check as { worktrees?: string[] }).worktrees
    : [];
  if (worktrees.length) {
    warnings.push(`This repo has ${worktrees.length} worktree(s).`);
  }

  const messageParts = [`Remove repo "${repoId}" and delete its local directory?`];
  if (warnings.length) {
    messageParts.push("", "Warnings:", ...warnings.map((w) => `- ${w}`));
  }
  if (worktrees.length) {
    messageParts.push(
      "",
      "Worktrees to delete:",
      ...worktrees.map((w) => `- ${w}`)
    );
  }

  const ok = await confirmModal(messageParts.join("\n"), {
    confirmText: "Remove",
    danger: true,
  });
  if (!ok) return;
  const needsForce = dirty || ahead > 0;
  const requestBody: Record<string, unknown> = {
    force: needsForce,
    delete_dir: true,
    delete_worktrees: worktrees.length > 0,
  };
  if (needsForce) {
    const requiredAttestation = `REMOVE ${repoId}`;
    const forceAttestation = await inputModal(
      `This repo has uncommitted or unpushed changes.\n\nType this confirmation text to force removal:\n${requiredAttestation}`,
      { placeholder: requiredAttestation, confirmText: "Remove anyway" }
    );
    if (!forceAttestation) return;
    if (forceAttestation !== requiredAttestation) {
      flash(`Confirmation text must exactly match: ${requiredAttestation}`, "error");
      return;
    }
    requestBody.force_attestation = forceAttestation;
  }
  await startHubJob(`/hub/jobs/repos/${repoId}/remove`, {
    body: requestBody,
    startedMessage: "Repo removal queued",
  });
  flash(`Removed repo: ${repoId}`, "success");
  await refreshHub();
}

async function setParentRepoPinned(repoId: string, pinned: boolean): Promise<void> {
  const response = await api(`/hub/repos/${encodeURIComponent(repoId)}/pin`, {
    method: "POST",
    body: { pinned },
  }) as { pinned_parent_repo_ids?: unknown };
  pinnedParentRepoIds = new Set(
    normalizePinnedParentRepoIds(response?.pinned_parent_repo_ids)
  );
  hubData.pinned_parent_repo_ids = Array.from(pinnedParentRepoIds);
}

async function handleRepoAction(repoId: string, action: string): Promise<void> {
  const buttons = repoListEl?.querySelectorAll(
    `button[data-repo="${repoId}"][data-action="${action}"]`
  );
  buttons?.forEach((btn) => (btn as HTMLButtonElement).disabled = true);
  try {
    if (action === "pin_parent" || action === "unpin_parent") {
      const pinned = action === "pin_parent";
      await setParentRepoPinned(repoId, pinned);
      renderReposWithScroll(hubData.repos || [], hubChannelEntries, pinnedParentRepoIds);
      flash(`${pinned ? "Pinned" : "Unpinned"}: ${repoId}`, "success");
      return;
    }

    const pathMap: Record<string, string> = {
      init: `/hub/repos/${repoId}/init`,
      sync_main: `/hub/repos/${repoId}/sync-main`,
    };
    if (action === "new_worktree") {
      const branch = await inputModal("New worktree branch name:", {
        placeholder: "feature/my-branch",
        confirmText: "Create",
      });
      if (!branch) return;
      const job = await startHubJob("/hub/jobs/worktrees/create", {
        body: { base_repo_id: repoId, branch },
        startedMessage: "Worktree creation queued",
      });
      const created = job?.result;
      flash(`Created worktree: ${created?.id || branch}`, "success");
      await refreshHub();
      if (created?.mounted) {
        window.location.href = resolvePath(`/repos/${created.id}/`);
      }
      return;
    }
    if (action === "repo_settings") {
      const repo = hubData.repos.find((item) => item.id === repoId);
      if (!repo) {
        flash(`Repo not found: ${repoId}`, "error");
        return;
      }
      await openRepoSettingsModal(repo);
      return;
    }
    if (action === "set_destination") {
      const repo = hubData.repos.find((item) => item.id === repoId);
      if (!repo) {
        flash(`Repo not found: ${repoId}`, "error");
        return;
      }
      const updated = await promptAndSetRepoDestination(repo);
      if (updated) {
        await refreshHub();
      }
      return;
    }
    if (action === "cleanup_worktree") {
      const repo = hubData.repos.find((item) => item.id === repoId);
      if (repo && isCleanupBlockedByChatBinding(repo)) {
        flash(
          "Unbind Discord/Telegram chats before cleaning up this worktree",
          "error"
        );
        return;
      }
      const displayName = repoId.includes("--")
        ? repoId.split("--").pop()
        : repoId;
      const ok = await confirmModal(
        `Clean up worktree "${displayName}"?\n\nCAR will archive a review snapshot for the Archive tab, then remove the worktree directory and branch. The default snapshot keeps tickets, contextspace, runs, flow artifacts, and lightweight metadata.`,
        { confirmText: "Archive & remove" }
      );
      if (!ok) return;
      await startHubJob("/hub/jobs/worktrees/cleanup", {
        body: {
          worktree_repo_id: repoId,
          archive: true,
          force_archive: false,
          archive_note: null,
        },
        startedMessage: "Worktree cleanup queued",
      });
      flash(`Removed worktree: ${repoId}`, "success");
      await refreshHub();
      return;
    }
    if (action === "archive_state") {
      const repo = hubData.repos.find((item) => item.id === repoId);
      const cleanupCount = repo ? unboundManagedThreadCount(repo) : 0;
      if (!repo || (!repo.has_car_state && cleanupCount <= 0)) return;
      const displayName = repo.display_name || repoId;
      const subject = repo.kind === "worktree" ? "worktree" : "repo";
      const archiveSummary = repo.has_car_state
        ? "archive reviewable runtime artifacts for later viewing in the Archive tab"
        : "skip the snapshot because CAR state is already clean";
      const threadSummary =
        cleanupCount > 0
          ? ` It will also archive ${cleanupCount} stale non-chat-bound managed thread${
              cleanupCount === 1 ? "" : "s"
            }.`
          : "";
      const ok = await confirmModal(
        `Archive ${subject} "${displayName}"?\n\nCAR will ${archiveSummary} before resetting local CAR state when needed.${threadSummary} Git state is not touched, and active chat bindings remain available for fresh work.`,
        { confirmText: "Archive" }
      );
      if (!ok) return;
      const response = (await api("/hub/repos/archive-state", {
        method: "POST",
        body: { repo_id: repoId, archive_note: null },
      })) as {
        snapshot_id?: string | null;
        archived_thread_count?: number | null;
      } | null;
      const archivedThreadCount =
        typeof response?.archived_thread_count === "number"
          ? response.archived_thread_count
          : 0;
      const snapshotText = response?.snapshot_id
        ? `snapshot ${response.snapshot_id}`
        : "managed threads only";
      const threadText =
        archivedThreadCount > 0
          ? ` and ${archivedThreadCount} managed thread${
              archivedThreadCount === 1 ? "" : "s"
            }`
          : "";
      flash(`Archived ${subject}: ${displayName} (${snapshotText}${threadText})`, "success");
      await refreshHub();
      return;
    }
    if (action === "remove_repo") {
      await removeRepoWithChecks(repoId);
      return;
    }

    const path = pathMap[action];
    if (!path) return;
    await api(path, { method: "POST" });
    flash(`${action} sent to ${repoId}`, "success");
    await refreshHub();
  } catch (err) {
    flash((err as Error).message || "Hub action failed", "error");
  } finally {
    buttons?.forEach((btn) => (btn as HTMLButtonElement).disabled = false);
  }
}

async function handleAgentWorkspaceAction(
  workspaceId: string,
  action: string
): Promise<void> {
  const buttons = agentWorkspaceListEl?.querySelectorAll(
    `button[data-agent-workspace="${workspaceId}"][data-action="${action}"]`
  );
  buttons?.forEach((btn) => ((btn as HTMLButtonElement).disabled = true));
  try {
    const workspace = hubData.agent_workspaces.find((item) => item.id === workspaceId);
    if (!workspace) {
      flash(`Agent workspace not found: ${workspaceId}`, "error");
      return;
    }

    if (action === "enable" || action === "disable") {
      const enabled = action === "enable";
      await api(`/hub/agent-workspaces/${encodeURIComponent(workspaceId)}`, {
        method: "PATCH",
        body: { enabled },
      });
      flash(`${enabled ? "Enabled" : "Disabled"}: ${workspaceId}`, "success");
      await refreshHub();
      return;
    }

    if (action === "set_destination") {
      const updated = await promptAndSetAgentWorkspaceDestination(workspace);
      if (updated) {
        await refreshHub();
      }
      return;
    }

    if (action === "remove") {
      const ok = await confirmModal(
        `Remove agent workspace "${workspace.display_name || workspace.id}" from CAR?\n\nManaged files will stay on disk at:\n${workspace.path}`,
        { confirmText: "Remove" }
      );
      if (!ok) return;
      await startHubJob(`/hub/jobs/agent-workspaces/${encodeURIComponent(workspaceId)}/remove`, {
        body: { delete_dir: false },
        startedMessage: "Agent workspace removal queued",
      });
      flash(`Removed agent workspace: ${workspaceId}`, "success");
      await refreshHub();
      return;
    }

    if (action === "delete") {
      const ok = await confirmModal(
        `Delete agent workspace "${workspace.display_name || workspace.id}"?\n\nCAR will unregister it and delete its managed directory:\n${workspace.path}`,
        { confirmText: "Delete", danger: true }
      );
      if (!ok) return;
      await startHubJob(`/hub/jobs/agent-workspaces/${encodeURIComponent(workspaceId)}/delete`, {
        body: { delete_dir: true },
        startedMessage: "Agent workspace delete queued",
      });
      flash(`Deleted agent workspace: ${workspaceId}`, "success");
      await refreshHub();
    }
  } catch (err) {
    flash((err as Error).message || "Agent workspace action failed", "error");
  } finally {
    buttons?.forEach((btn) => ((btn as HTMLButtonElement).disabled = false));
  }
}

async function handleCleanupAll(): Promise<void> {
  const btn = document.getElementById("hub-cleanup-all") as HTMLButtonElement | null;
  if (!btn) {
    flash("Cleanup control is missing from the page.", "error");
    return;
  }

  let preview: CleanupAllPreview;
  try {
    preview = (await api("/hub/cleanup-all/preview", {
      method: "GET",
    })) as CleanupAllPreview;
  } catch (err) {
    flash((err as Error).message || "Failed to load cleanup preview", "error");
    return;
  }

  const threadCount = preview.threads?.archived_count || 0;
  const worktreeCount = preview.worktrees?.archived_count || 0;
  const flowRunCount = preview.flow_runs?.archived_count || 0;
  const totalCount = threadCount + worktreeCount + flowRunCount;

  if (totalCount <= 0) {
    flash("Nothing to clean up", "success");
    return;
  }

  const lines: string[] = [];
  if (threadCount > 0) {
    const repoSummaries = (preview.threads?.by_repo || [])
      .map((r) => `${r.repo_id} (${r.count})`)
      .join(", ");
    lines.push(
      `${threadCount} unbound thread${threadCount === 1 ? "" : "s"}: ${repoSummaries}`
    );
  }
  if (worktreeCount > 0) {
    const worktreeNames = (preview.worktrees?.items || [])
      .map((w) => w.branch || w.id)
      .join(", ");
    lines.push(
      `${worktreeCount} worktree${worktreeCount === 1 ? "" : "s"}: ${worktreeNames}`
    );
  }
  if (flowRunCount > 0) {
    const flowSummaries = (preview.flow_runs?.by_repo || [])
      .map((r) => `${r.repo_id} (${r.count})`)
      .join(", ");
    lines.push(
      `${flowRunCount} completed flow run${flowRunCount === 1 ? "" : "s"}: ${flowSummaries}`
    );
  }

  const message = `Clean slate?\n\nCAR will archive and clean up:\n\n${lines
    .map((l) => "• " + l)
    .join("\n")}`;
  const ok = await confirmModal(message, { confirmText: "Cleanup all" });
  if (!ok) return;

  setCleanupAllInFlight(true);
  renderSummary(hubData.repos || [], hubData);
  try {
    const job = await startHubJob("/hub/jobs/cleanup-all", {
      startedMessage: "Cleanup started",
    });
    const resultMessage =
      typeof job.result?.message === "string" && job.result.message.trim()
        ? job.result.message.trim()
        : "Cleanup complete";
    flash(resultMessage, "success");
    await refreshHub();
  } catch (err) {
    flash((err as Error).message || "Cleanup failed", "error");
  } finally {
    setCleanupAllInFlight(false);
    renderSummary(hubData.repos || [], hubData);
  }
}

let closeCreateRepoModal: (() => void) | null = null;
let closeCreateAgentWorkspaceModal: (() => void) | null = null;

function hideCreateRepoModal(): void {
  if (closeCreateRepoModal) {
    const close = closeCreateRepoModal;
    closeCreateRepoModal = null;
    close();
  }
}

function hideCreateAgentWorkspaceModal(): void {
  if (closeCreateAgentWorkspaceModal) {
    const close = closeCreateAgentWorkspaceModal;
    closeCreateAgentWorkspaceModal = null;
    close();
  }
}

function showCreateRepoModal(): void {
  const modal = document.getElementById("create-repo-modal");
  if (!modal) return;
  const triggerEl = document.activeElement;
  hideCreateRepoModal();
  const input = document.getElementById("create-repo-id") as HTMLInputElement | null;
  closeCreateRepoModal = openModal(modal, {
    initialFocus: input || modal,
    returnFocusTo: triggerEl as HTMLElement | null,
    onRequestClose: hideCreateRepoModal,
  });
  if (input) {
    input.value = "";
    input.focus();
  }
  const pathInput = document.getElementById("create-repo-path") as HTMLInputElement | null;
  if (pathInput) pathInput.value = "";
  const urlInput = document.getElementById("create-repo-url") as HTMLInputElement | null;
  if (urlInput) urlInput.value = "";
  const gitCheck = document.getElementById("create-repo-git") as HTMLInputElement | null;
  if (gitCheck) gitCheck.checked = true;
}

function showCreateAgentWorkspaceModal(): void {
  const modal = document.getElementById("create-agent-workspace-modal");
  if (!modal) return;
  const triggerEl = document.activeElement;
  hideCreateAgentWorkspaceModal();
  const input = document.getElementById(
    "create-agent-workspace-id"
  ) as HTMLInputElement | null;
  closeCreateAgentWorkspaceModal = openModal(modal, {
    initialFocus: input || modal,
    returnFocusTo: triggerEl as HTMLElement | null,
    onRequestClose: hideCreateAgentWorkspaceModal,
  });
  if (input) {
    input.value = "";
    input.focus();
  }
  const runtimeInput = document.getElementById(
    "create-agent-workspace-runtime"
  ) as HTMLInputElement | null;
  if (runtimeInput) runtimeInput.value = "";
  const nameInput = document.getElementById(
    "create-agent-workspace-name"
  ) as HTMLInputElement | null;
  if (nameInput) nameInput.value = "";
}

async function createRepo(repoId: string | null, repoPath: string | null, gitInit: boolean, gitUrl: string | null): Promise<boolean> {
  try {
    const payload: Record<string, unknown> = {};
    if (repoId) payload.id = repoId;
    if (repoPath) payload.path = repoPath;
    payload.git_init = gitInit;
    if (gitUrl) payload.git_url = gitUrl;
    const job = await startHubJob("/hub/jobs/repos", {
      body: payload,
      startedMessage: "Repo creation queued",
    });
    const label = repoId || repoPath || "repo";
    flash(`Created repo: ${label}`, "success");
    await refreshHub();
    if (job?.result?.mounted && job?.result?.id) {
      window.location.href = resolvePath(`/repos/${job.result.id}/`);
    }
    return true;
  } catch (err) {
    flash((err as Error).message || "Failed to create repo", "error");
    return false;
  }
}

async function createAgentWorkspace(
  workspaceId: string | null,
  runtime: string | null,
  displayName: string | null
): Promise<boolean> {
  try {
    const payload: Record<string, unknown> = {};
    if (workspaceId) payload.id = workspaceId;
    if (runtime) payload.runtime = runtime;
    if (displayName) payload.display_name = displayName;
    await startHubJob("/hub/jobs/agent-workspaces", {
      body: payload,
      startedMessage: "Agent workspace creation queued",
    });
    flash(`Created agent workspace: ${workspaceId || displayName || "workspace"}`, "success");
    await refreshHub();
    return true;
  } catch (err) {
    flash((err as Error).message || "Failed to create agent workspace", "error");
    return false;
  }
}

async function handleCreateRepoSubmit(): Promise<void> {
  const idInput = document.getElementById("create-repo-id") as HTMLInputElement | null;
  const pathInput = document.getElementById("create-repo-path") as HTMLInputElement | null;
  const urlInput = document.getElementById("create-repo-url") as HTMLInputElement | null;
  const gitCheck = document.getElementById("create-repo-git") as HTMLInputElement | null;

  const repoId = idInput?.value?.trim() || null;
  const repoPath = pathInput?.value?.trim() || null;
  const gitUrl = urlInput?.value?.trim() || null;
  const gitInit = gitCheck?.checked ?? true;

  if (!repoId && !gitUrl) {
    flash("Repo ID or Git URL is required", "error");
    return;
  }

  const ok = await createRepo(repoId, repoPath, gitInit, gitUrl);
  if (ok) {
    hideCreateRepoModal();
  }
}

async function handleCreateAgentWorkspaceSubmit(): Promise<void> {
  const idInput = document.getElementById(
    "create-agent-workspace-id"
  ) as HTMLInputElement | null;
  const runtimeInput = document.getElementById(
    "create-agent-workspace-runtime"
  ) as HTMLInputElement | null;
  const nameInput = document.getElementById(
    "create-agent-workspace-name"
  ) as HTMLInputElement | null;

  const workspaceId = idInput?.value?.trim() || null;
  const runtime = runtimeInput?.value?.trim() || null;
  const displayName = nameInput?.value?.trim() || null;

  if (!workspaceId || !runtime) {
    flash("Workspace ID and runtime are required", "error");
    return;
  }

  const ok = await createAgentWorkspace(workspaceId, runtime, displayName);
  if (ok) {
    hideCreateAgentWorkspaceModal();
  }
}

function renderHubUsageMeta(data: HubUsageData | null): void {
  if (hubUsageMeta) {
    hubUsageMeta.textContent = data?.codex_home || "–";
  }
}

function scheduleHubUsageSummaryRetry(): void {
  clearHubUsageSummaryRetry();
  hubUsageSummaryRetryTimer = setTimeout(() => {
    loadHubUsage();
  }, 1500);
}

function clearHubUsageSummaryRetry(): void {
  if (hubUsageSummaryRetryTimer) {
    clearTimeout(hubUsageSummaryRetryTimer);
    hubUsageSummaryRetryTimer = null;
  }
}

interface HandleHubUsagePayloadOptions {
  cachedUsage?: HubUsageData | null;
  allowRetry?: boolean;
}

function handleHubUsagePayload(data: HubUsageData | null, { cachedUsage, allowRetry }: HandleHubUsagePayloadOptions): boolean {
  const hasSummary = data && Array.isArray(data.repos);
  const effective = hasSummary ? data : cachedUsage;

  if (effective) {
    indexHubUsage(effective);
    renderHubUsageMeta(effective);
    renderReposWithScroll(hubData.repos || [], hubChannelEntries, pinnedParentRepoIds);
  }

  if (data?.status === "loading") {
    if (allowRetry) scheduleHubUsageSummaryRetry();
    return Boolean(hasSummary);
  }

  if (hasSummary) {
    clearHubUsageSummaryRetry();
    return true;
  }

  if (!effective && !data) {
    renderReposWithScroll(hubData.repos || [], hubChannelEntries, pinnedParentRepoIds);
  }
  return false;
}

interface LoadHubUsageOptions {
  silent?: boolean;
  allowRetry?: boolean;
}

async function loadHubUsage({ silent = false, allowRetry = true }: LoadHubUsageOptions = {}): Promise<void> {
  if (!silent && hubUsageRefresh) (hubUsageRefresh as HTMLButtonElement).disabled = true;
  try {
    const data = await api("/hub/usage") as HubUsageData;
    const cachedUsage = loadSessionCache<HubUsageData | null>(HUB_USAGE_CACHE_KEY, HUB_CACHE_TTL_MS);
    const shouldCache = handleHubUsagePayload(data, {
      cachedUsage,
      allowRetry,
    });
    if (shouldCache) {
      saveSessionCache(HUB_USAGE_CACHE_KEY, data);
    }
  } catch (err) {
    const cachedUsage = loadSessionCache<HubUsageData | null>(HUB_USAGE_CACHE_KEY, HUB_CACHE_TTL_MS);
    if (cachedUsage) {
      handleHubUsagePayload(cachedUsage, { cachedUsage, allowRetry: false });
    }
    if (!silent) {
      flash((err as Error).message || "Failed to load usage", "error");
    }
    clearHubUsageSummaryRetry();
  } finally {
    if (!silent && hubUsageRefresh) (hubUsageRefresh as HTMLButtonElement).disabled = false;
  }
}

async function loadUpdateTargetOptions(selectId: string | null): Promise<void> {
  const select = selectId ? (document.getElementById(selectId) as HTMLSelectElement | null) : null;
  if (!select) return;
  const isInitialized = select.dataset.updateTargetsInitialized === "1";
  let payload: UpdateTargetsResponse | null;
  try {
    payload = await api("/system/update/targets", { method: "GET" }) as UpdateTargetsResponse;
  } catch (_err) {
    return;
  }
  const { options, defaultTarget } = updateTargetOptionsFromResponse(payload);
  if (!options.length) return;

  const previous = normalizeUpdateTarget(select.value || "all");
  const hasPrevious = options.some((item) => item.value === previous);
  const fallback = options.some((item) => item.value === defaultTarget)
    ? defaultTarget
    : options[0].value;

  select.replaceChildren();
  options.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    select.appendChild(option);
  });
  if (isInitialized) {
    select.value = hasPrevious ? previous : fallback;
  } else {
    select.value = fallback;
    select.dataset.updateTargetsInitialized = "1";
  }
}

async function handleSystemUpdate(btnId: string, targetSelectId: string | null): Promise<void> {
  const btn = document.getElementById(btnId) as HTMLButtonElement | null;
  if (!btn) return;

  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Checking...";
  const updateTarget = getUpdateTarget(targetSelectId);
  const targetLabel = describeUpdateTarget(updateTarget);

  let check: UpdateCheckResponse | undefined;
  try {
    check = await api("/system/update/check") as UpdateCheckResponse;
  } catch (err) {
    check = { update_available: true, message: (err as Error).message || "Unable to check for updates." };
  }

  if (!check?.update_available) {
    flash(check?.message || "No update available.", "info");
    btn.disabled = false;
    btn.textContent = originalText;
    return;
  }

  const restartNotice = updateRestartNotice(updateTarget);
  const confirmed = await confirmModal(
    `${check?.message || "Update available."} Update Codex Autorunner (${targetLabel})? ${restartNotice}`
  );
  if (!confirmed) {
    btn.disabled = false;
    btn.textContent = originalText;
    return;
  }

  btn.textContent = "Updating...";

  try {
    let res = await api("/system/update", {
      method: "POST",
      body: { target: updateTarget },
    }) as UpdateResponse;
    if (res.requires_confirmation) {
      const forceConfirmed = await confirmModal(
        res.message || "Active sessions are still running. Update anyway?",
        { confirmText: "Update anyway", cancelText: "Cancel", danger: true }
      );
      if (!forceConfirmed) {
        btn.disabled = false;
        btn.textContent = originalText;
        return;
      }
      res = await api("/system/update", {
        method: "POST",
        body: { target: updateTarget, force: true },
      }) as UpdateResponse;
    }
    flash(res.message || `Update started (${targetLabel}).`, "success");
    if (!includesWebUpdateTarget(updateTarget)) {
      btn.disabled = false;
      btn.textContent = originalText;
      return;
    }
    document.body.style.pointerEvents = "none";
    setTimeout(() => {
      const url = new URL(window.location.href);
      url.searchParams.set("v", String(Date.now()));
      window.location.replace(url.toString());
    }, 8000);
  } catch (err) {
    flash((err as Error).message || "Update failed", "error");
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function initHubSettings(): void {
  const settingsBtns = Array.from(
    document.querySelectorAll<HTMLButtonElement>("#hub-settings, #pma-settings")
  );
  const modal = document.getElementById("hub-settings-modal");
  const closeBtn = document.getElementById("hub-settings-close");
  const updateBtn = document.getElementById("hub-update-btn") as HTMLButtonElement | null;
  const updateTarget = document.getElementById("hub-update-target") as HTMLSelectElement | null;
  void loadUpdateTargetOptions(updateTarget ? updateTarget.id : null);
  let closeModal: (() => void) | null = null;

  const hideModal = () => {
    if (closeModal) {
      const close = closeModal;
      closeModal = null;
      close();
    }
  };

  if (modal && settingsBtns.length > 0) {
    settingsBtns.forEach((settingsBtn) => {
      settingsBtn.addEventListener("click", () => {
        const triggerEl = document.activeElement;
        hideModal();
        closeModal = openModal(modal, {
          initialFocus: closeBtn || updateBtn || modal,
          returnFocusTo: triggerEl as HTMLElement | null,
          onRequestClose: hideModal,
        });
      });
    });
  }

  if (closeBtn && modal) {
    closeBtn.addEventListener("click", () => {
      hideModal();
    });
  }

  if (updateBtn) {
    updateBtn.addEventListener("click", () =>
      handleSystemUpdate("hub-update-btn", updateTarget ? updateTarget.id : null)
    );
  }
}

async function loadHubChannelDirectory({ silent = false }: { silent?: boolean } = {}): Promise<void> {
  try {
    const payload = (await api("/hub/chat/channels?limit=1000", {
      method: "GET",
    })) as HubChannelDirectoryResponse;
    hubChannelEntries = Array.isArray(payload.entries) ? payload.entries : [];
    renderReposWithScroll(hubData.repos || [], hubChannelEntries, pinnedParentRepoIds);
  } catch (err) {
    if (!silent) {
      flash((err as Error).message || "Failed to load channel directory", "error");
    }
  }
}

async function refreshHub(): Promise<void> {
  setButtonLoading(true);
  try {
    const data = await api("/hub/repos", { method: "GET" }) as HubData;
    applyHubData(data);
    markHubRefreshed();
    saveHubBootstrapCache(hubData);
    renderSummary(hubData.repos || [], hubData);
    renderReposWithScroll(hubData.repos || [], hubChannelEntries, pinnedParentRepoIds);
    renderAgentWorkspaces(hubData.agent_workspaces || [], hubChannelEntries);
    loadHubUsage({ silent: true }).catch(() => {});
    loadHubChannelDirectory({ silent: true }).catch(() => {});
  } catch (err) {
    flash((err as Error).message || "Hub request failed", "error");
  } finally {
    setButtonLoading(false);
  }
}

async function triggerHubScan(): Promise<void> {
  setButtonLoading(true);
  try {
    await startHubJob("/hub/jobs/scan", { startedMessage: "Hub scan queued" });
    await refreshHub();
  } catch (err) {
    flash((err as Error).message || "Hub scan failed", "error");
  } finally {
    setButtonLoading(false);
  }
}

function markHubRefreshed(): void {
  lastHubAutoRefreshAt = Date.now();
}

function hasActiveRuns(repos: HubRepo[]): boolean {
  return repos.some((repo) => repo.status === "running");
}

async function dynamicRefreshHub(): Promise<void> {
  const now = Date.now();
  const running = hasActiveRuns(hubData.repos || []);
  const minInterval = running ? HUB_REFRESH_ACTIVE_MS : HUB_REFRESH_IDLE_MS;
  if (now - lastHubAutoRefreshAt < minInterval) return;
  await silentRefreshHub();
}

async function silentRefreshHub(): Promise<void> {
  try {
    const data = await api("/hub/repos", { method: "GET" }) as HubData;
    applyHubData(data);
    markHubRefreshed();
    saveHubBootstrapCache(hubData);
    renderSummary(hubData.repos || [], hubData);
    renderReposWithScroll(hubData.repos || [], hubChannelEntries, pinnedParentRepoIds);
    renderAgentWorkspaces(hubData.agent_workspaces || [], hubChannelEntries);
    await Promise.allSettled([
      loadHubUsage({ silent: true, allowRetry: false }),
      loadHubChannelDirectory({ silent: true }),
    ]);
  } catch (err) {
    console.error("Auto-refresh hub failed:", err);
  }
}

async function loadHubVersion(): Promise<void> {
  try {
    const data = await api("/hub/version", { method: "GET" });
    const version = (data as { asset_version?: string }).asset_version || "";
    const formatted = version ? `v${version}` : "v–";
    if (hubVersionEl) hubVersionEl.textContent = formatted;
    if (pmaVersionEl) pmaVersionEl.textContent = formatted;
  } catch (_err) {
    if (hubVersionEl) hubVersionEl.textContent = "v–";
    if (pmaVersionEl) pmaVersionEl.textContent = "v–";
  }
}

async function checkUpdateStatus(): Promise<void> {
  try {
    const data = await api("/system/update/status", { method: "GET" });
    if (!data || !(data as { status?: string }).status) return;
    const stamp = (data as { at?: string | number }).at ? String((data as { at?: string | number }).at) : "";
    if (stamp && sessionStorage.getItem(UPDATE_STATUS_SEEN_KEY) === stamp) return;
    if ((data as { status?: string }).status === "rollback" || (data as { status?: string }).status === "error") {
      flash((data as { message?: string }).message || "Update failed; rollback attempted.", "error");
    }
    if (stamp) sessionStorage.setItem(UPDATE_STATUS_SEEN_KEY, stamp);
  } catch (_err) {
    // Ignore update status failures; UI still renders.
  }
}

function prefetchRepo(url: string): void {
  if (!url || prefetchedUrls.has(url)) return;
  prefetchedUrls.add(url);
  fetch(url, { method: "GET", headers: { "x-prefetch": "1" } }).catch(() => {});
}

function resolvePath(path: string): string {
  const base = (window as unknown as { __CAR_BASE_PREFIX?: string }).__CAR_BASE_PREFIX || "";
  if (!base || path.startsWith(base)) return path;
  return `${base}${path}`;
}

export function applyHubPanelState(openPanel: string): void {
  hubOpenPanel = openPanel;
  const reposOpen = openPanel === "repos";
  const agentsOpen = openPanel === "agents";
  hubShellEl?.setAttribute("data-hub-open-panel", openPanel);
  hubRepoPanelEl?.classList.toggle("hub-panel-expanded", reposOpen);
  hubRepoPanelEl?.classList.toggle("hub-panel-collapsed", !reposOpen);
  hubAgentPanelEl?.classList.toggle("hub-panel-expanded", agentsOpen);
  hubAgentPanelEl?.classList.toggle("hub-panel-collapsed", !agentsOpen);
  if (hubRepoPanelSummaryEl) {
    hubRepoPanelSummaryEl.setAttribute("aria-expanded", reposOpen ? "true" : "false");
  }
  if (hubRepoPanelStateEl) {
    hubRepoPanelStateEl.textContent = reposOpen ? "Expanded" : "Show panel";
  }
  if (hubAgentPanelSummaryEl) {
    hubAgentPanelSummaryEl.setAttribute("aria-expanded", agentsOpen ? "true" : "false");
  }
  if (hubAgentPanelStateEl) {
    hubAgentPanelStateEl.textContent = agentsOpen ? "Expanded" : "Show panel";
  }
}

export function toggleHubPanel(panel: string): void {
  if (hubOpenPanel === panel) return;
  saveHubOpenPanel(panel);
  applyHubPanelState(panel);
}

function initHubPanelControls(): void {
  applyHubPanelState(hubOpenPanel);
  hubRepoPanelSummaryEl?.addEventListener("click", () => {
    toggleHubPanel("repos");
  });
  hubAgentPanelSummaryEl?.addEventListener("click", () => {
    toggleHubPanel("agents");
  });
}

function initHubRepoListControls(): void {
  loadHubViewPrefs();
  if (hubFlowFilterEl) {
    hubFlowFilterEl.value = hubViewPrefs.flowFilter;
    hubFlowFilterEl.addEventListener("change", () => {
      hubViewPrefs.flowFilter = hubFlowFilterEl.value as typeof hubViewPrefs.flowFilter;
      saveHubViewPrefs();
      renderReposWithScroll(hubData.repos || [], hubChannelEntries, pinnedParentRepoIds);
    });
  }
  if (hubSortOrderEl) {
    hubSortOrderEl.value = hubViewPrefs.sortOrder;
    hubSortOrderEl.addEventListener("change", () => {
      hubViewPrefs.sortOrder = hubSortOrderEl.value as typeof hubViewPrefs.sortOrder;
      saveHubViewPrefs();
      renderReposWithScroll(hubData.repos || [], hubChannelEntries, pinnedParentRepoIds);
    });
  }
}

function attachHubHandlers(): void {
  initHubSettings();
  const refreshBtn = document.getElementById("hub-refresh") as HTMLButtonElement | null;
  const newRepoBtn = document.getElementById("hub-new-repo") as HTMLButtonElement | null;
  const newAgentBtn = document.getElementById("hub-new-agent") as HTMLButtonElement | null;
  const createCancelBtn = document.getElementById("create-repo-cancel") as HTMLButtonElement | null;
  const createSubmitBtn = document.getElementById("create-repo-submit") as HTMLButtonElement | null;
  const createRepoId = document.getElementById("create-repo-id") as HTMLInputElement | null;
  const createAgentCancelBtn = document.getElementById(
    "create-agent-workspace-cancel"
  ) as HTMLButtonElement | null;
  const createAgentSubmitBtn = document.getElementById(
    "create-agent-workspace-submit"
  ) as HTMLButtonElement | null;
  const createAgentId = document.getElementById(
    "create-agent-workspace-id"
  ) as HTMLInputElement | null;
  const createAgentRuntime = document.getElementById(
    "create-agent-workspace-runtime"
  ) as HTMLInputElement | null;
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => triggerHubScan());
  }
  if (hubUsageRefresh) {
    hubUsageRefresh.addEventListener("click", () => loadHubUsage());
  }
  const cleanupAllBtn = document.getElementById(
    "hub-cleanup-all"
  ) as HTMLButtonElement | null;
  if (cleanupAllBtn) {
    if (!hubCleanupAllClickBound) {
      hubCleanupAllClickBound = true;
      cleanupAllBtn.addEventListener("click", () => {
        void handleCleanupAll();
      });
    }
  } else {
    console.warn("hub-cleanup-all button not found in DOM");
  }
  if (hubRepoSearchInput) {
    hubRepoSearchInput.addEventListener("input", () => {
      renderReposWithScroll(hubData.repos || [], hubChannelEntries, pinnedParentRepoIds);
    });
  }

  if (newRepoBtn) {
    newRepoBtn.addEventListener("click", () => {
      toggleHubPanel("repos");
      showCreateRepoModal();
    });
  }
  if (newAgentBtn) {
    newAgentBtn.addEventListener("click", () => {
      toggleHubPanel("agents");
      showCreateAgentWorkspaceModal();
    });
  }
  if (createCancelBtn) {
    createCancelBtn.addEventListener("click", hideCreateRepoModal);
  }
  if (createSubmitBtn) {
    createSubmitBtn.addEventListener("click", handleCreateRepoSubmit);
  }
  if (createAgentCancelBtn) {
    createAgentCancelBtn.addEventListener("click", hideCreateAgentWorkspaceModal);
  }
  if (createAgentSubmitBtn) {
    createAgentSubmitBtn.addEventListener("click", handleCreateAgentWorkspaceSubmit);
  }

  if (createRepoId) {
    createRepoId.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleCreateRepoSubmit();
      }
    });
  }
  if (createAgentId) {
    createAgentId.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleCreateAgentWorkspaceSubmit();
      }
    });
  }
  if (createAgentRuntime) {
    createAgentRuntime.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleCreateAgentWorkspaceSubmit();
      }
    });
  }

  if (repoListEl) {
    repoListEl.addEventListener("click", (event) => {
        const target = event.target as HTMLElement;

        const btn = target instanceof HTMLElement && target.closest("button[data-action]") as HTMLElement | null;
        if (btn) {
          event.stopPropagation();
          const action = (btn as HTMLElement).dataset.action;
          const repoId = (btn as HTMLElement).dataset.repo;
          if (action && repoId) {
            handleRepoAction(repoId, action);
          }
          return;
        }

        const card = target instanceof HTMLElement && target.closest(".hub-repo-clickable") as HTMLElement | null;
        if (card && card.dataset.href) {
          window.location.href = card.dataset.href;
        }
      });

    repoListEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        const target = event.target;
        if (
          target instanceof HTMLElement &&
          target.classList.contains("hub-repo-clickable")
        ) {
          event.preventDefault();
          if (target.dataset.href) {
            window.location.href = target.dataset.href;
          }
        }
      }
    });

    repoListEl.addEventListener("mouseover", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const card = target.closest(".hub-repo-clickable") as HTMLElement | null;
      if (card && card.dataset.href) {
        prefetchRepo(card.dataset.href);
      }
    });

    repoListEl.addEventListener("pointerdown", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const card = target.closest(".hub-repo-clickable") as HTMLElement | null;
      if (card && card.dataset.href) {
        prefetchRepo(card.dataset.href);
      }
    });
  }

  if (agentWorkspaceListEl) {
    agentWorkspaceListEl.addEventListener("click", (event) => {
      const target = event.target as HTMLElement;
      const btn =
        target instanceof HTMLElement
          ? (target.closest("button[data-action]") as HTMLElement | null)
          : null;
      if (!btn) return;
      event.stopPropagation();
      const action = btn.dataset.action;
      const workspaceId = btn.dataset.agentWorkspace;
      if (action && workspaceId) {
        handleAgentWorkspaceAction(workspaceId, action);
      }
    });
  }
}

export function initInteractionHarness(): void {
  attachHubHandlers();
  initHubPanelControls();
}

export function initHub(): void {
  attachHubHandlers();
  initHubRepoListControls();
  initHubPanelControls();
  if (!repoListEl) return;
  initNotificationBell();
  const cachedHub = loadHubBootstrapCache();
  if (cachedHub) {
    applyHubData(cachedHub);
    renderSummary(hubData.repos || [], hubData);
    renderReposWithScroll(hubData.repos || [], hubChannelEntries, pinnedParentRepoIds);
    renderAgentWorkspaces(hubData.agent_workspaces || [], hubChannelEntries);
  }
  const cachedUsage = loadSessionCache<HubUsageData | null>(HUB_USAGE_CACHE_KEY, HUB_CACHE_TTL_MS);
  if (cachedUsage) {
    indexHubUsage(cachedUsage);
    renderHubUsageMeta(cachedUsage);
  }
  loadHubChannelDirectory({ silent: true }).catch(() => {});
  refreshHub();
  void Promise.allSettled([loadHubVersion(), checkUpdateStatus()]);

  registerAutoRefresh("hub-repos", {
    callback: async (ctx) => {
      void ctx;
      await dynamicRefreshHub();
    },
    tabId: null,
    interval: HUB_REFRESH_ACTIVE_MS,
    refreshOnActivation: true,
    immediate: false,
  });
}
