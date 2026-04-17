// GENERATED FILE - do not edit directly. Source: static_src/
import { loadHubBootstrapCache, saveHubBootstrapCache } from "./hubCache.js";
import { renderRepos as _renderRepos, renderAgentWorkspaces as _renderAgentWorkspaces } from "./hubRepoCards.js";
import { applyHubPanelState, toggleHubPanel, initInteractionHarness, setHubChannelEntries as setHubChannelEntriesAction, getHubChannelEntries, getPinnedParentRepoIds } from "./hubActions.js";
export { initHub } from "./hubActions.js";
export { loadHubBootstrapCache, saveHubBootstrapCache } from "./hubCache.js";
export { applyHubPanelState, toggleHubPanel, initInteractionHarness } from "./hubActions.js";
function renderRepos(repos) {
    _renderRepos(repos, getHubChannelEntries(), getPinnedParentRepoIds());
}
function renderAgentWorkspaces(workspaces) {
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
    setHubChannelEntries(entries) {
        setHubChannelEntriesAction(entries);
    },
};
