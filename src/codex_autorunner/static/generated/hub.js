// GENERATED FILE - do not edit directly. Source: static_src/
import { loadHubBootstrapCache, saveHubBootstrapCache } from "./hubCache.js?v=be399e9b80baaceac895399d521c7e33ba6116a6282d86fe16aaac8dd380e544";
import { renderRepos as _renderRepos, renderAgentWorkspaces as _renderAgentWorkspaces } from "./hubRepoCards.js?v=be399e9b80baaceac895399d521c7e33ba6116a6282d86fe16aaac8dd380e544";
import { applyHubPanelState, toggleHubPanel, initInteractionHarness, setHubChannelEntries as setHubChannelEntriesAction, getHubChannelEntries, getPinnedParentRepoIds } from "./hubActions.js?v=be399e9b80baaceac895399d521c7e33ba6116a6282d86fe16aaac8dd380e544";
import { attachHandlersAndControls, bootstrapHubData } from "./hubActions.js?v=be399e9b80baaceac895399d521c7e33ba6116a6282d86fe16aaac8dd380e544";
export { loadHubBootstrapCache, saveHubBootstrapCache } from "./hubCache.js?v=be399e9b80baaceac895399d521c7e33ba6116a6282d86fe16aaac8dd380e544";
export { applyHubPanelState, toggleHubPanel, initInteractionHarness } from "./hubActions.js?v=be399e9b80baaceac895399d521c7e33ba6116a6282d86fe16aaac8dd380e544";
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
