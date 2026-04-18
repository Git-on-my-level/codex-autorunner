import type { HubChannelEntry, HubRepo, HubAgentWorkspace } from "./hubTypes.js";
import { loadHubBootstrapCache, saveHubBootstrapCache } from "./hubCache.js";
import { renderRepos as _renderRepos, renderAgentWorkspaces as _renderAgentWorkspaces } from "./hubRepoCards.js";
import { applyHubPanelState, toggleHubPanel, initInteractionHarness, setHubChannelEntries as setHubChannelEntriesAction, getHubChannelEntries, getPinnedParentRepoIds } from "./hubActions.js";
import { attachHandlersAndControls, bootstrapHubData } from "./hubActions.js";

export type { HubRepo, HubAgentWorkspace, HubData } from "./hubTypes.js";
export { loadHubBootstrapCache, saveHubBootstrapCache } from "./hubCache.js";
export { applyHubPanelState, toggleHubPanel, initInteractionHarness } from "./hubActions.js";

export function initHub(): void {
  attachHandlersAndControls();
  bootstrapHubData();
}

function renderRepos(repos: HubRepo[]): void {
  _renderRepos(repos, getHubChannelEntries(), getPinnedParentRepoIds());
}

function renderAgentWorkspaces(workspaces: HubAgentWorkspace[]): void {
  _renderAgentWorkspaces(workspaces, getHubChannelEntries());
}

export const __hubTest = {
  renderRepos,
  renderAgentWorkspaces,
  applyHubPanelState,
  toggleHubPanel,
  loadHubBootstrapCache,
  saveHubBootstrapCache,
  initInteractionHarness,
  setHubChannelEntries(entries: HubChannelEntry[]): void {
    setHubChannelEntriesAction(entries);
  },
};
