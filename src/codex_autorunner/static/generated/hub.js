// GENERATED FILE - do not edit directly. Source: static_src/
import { loadHubBootstrapCache, saveHubBootstrapCache } from "./hubCache.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { renderRepos as _renderRepos, renderAgentWorkspaces as _renderAgentWorkspaces } from "./hubRepoCards.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { applyHubPanelState, toggleHubPanel, initInteractionHarness, setHubChannelEntries as setHubChannelEntriesAction, getHubChannelEntries, getPinnedParentRepoIds } from "./hubActions.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { attachHandlersAndControls, bootstrapHubData } from "./hubActions.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
export { loadHubBootstrapCache, saveHubBootstrapCache } from "./hubCache.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
export { applyHubPanelState, toggleHubPanel, initInteractionHarness } from "./hubActions.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
export function initHub() {
    attachHandlersAndControls();
    bootstrapHubData();
}
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
