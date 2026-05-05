// GENERATED FILE - do not edit directly. Source: static_src/
import { loadHubBootstrapCache, saveHubBootstrapCache } from "./hubCache.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
import { renderRepos as _renderRepos, renderAgentWorkspaces as _renderAgentWorkspaces } from "./hubRepoCards.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
import { applyHubPanelState, toggleHubPanel, initInteractionHarness, setHubChannelEntries as setHubChannelEntriesAction, getHubChannelEntries, getPinnedParentRepoIds } from "./hubActions.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
import { attachHandlersAndControls, bootstrapHubData } from "./hubActions.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
export { loadHubBootstrapCache, saveHubBootstrapCache } from "./hubCache.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
export { applyHubPanelState, toggleHubPanel, initInteractionHarness } from "./hubActions.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
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
